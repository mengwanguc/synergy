import copy
import json
from multiprocessing.pool import ThreadPool
import math
import logging
import numa
import os
import queue
import re
from shutil import which
import signal
import subprocess
import sys
import threading
import time
import traceback

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import helper

CUDA_MPS_PIPE_DIRECTORY = '/tmp/nvidia-mps'
CUDA_MPS_LOG_DIRECTORY = '/tmp/nvidia-log'
MAX_CPUS_PER_GPU = 8
LOG_FORMAT = '{name}:{levelname} [{asctime}] {message}'
ITERATOR_LOG_FORMAT = '[{asctime}] [{event}] [{status}] {message}'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

class JobLauncher:
    def __init__(self, machine_id, round_duration, gpu_ids, worker_rpc_client,
                 sched_addr, sched_port, run_dir, data_dir, checkpoint_dir,
                 use_mps=False):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT,
                                          style='{'))
        logger.addHandler(ch)
        self._logger = logger
        self._thread_pool = ThreadPool()
        self._machine_id = machine_id
        self._round_duration = round_duration
        self._worker_rpc_client = worker_rpc_client
        self._sched_addr = sched_addr
        self._sched_port = sched_port
        self._run_dir = run_dir
        self._data_dir = data_dir
        self._checkpoint_dir = checkpoint_dir
        self._gpu_ids = gpu_ids
        self._gpu_queue = queue.Queue(len(self._gpu_ids))
        self._job_assignments = {}
        self._commands = {}
        self._lock = threading.Lock()
        for gpu_id in self._gpu_ids:
            self._gpu_queue.put(gpu_id)
        self._configure_numa()
        self._use_mps = use_mps
        if self._use_mps:
            self._mps_initially_enabled = self._mps_status()
            if self._mps_initially_enabled:
                self._logger.info('CUDA MPS already running')
            else:
                mps_enabled = self._enable_mps()
                if not mps_enabled:
                    raise RuntimeError('Failed to enable CUDA MPS')

    def _configure_numa(self):
        self._numa_available = \
            numa.available() and which('numactl') is not None
        if not self._numa_available:
            return
        num_numa_nodes = numa.get_max_node() + 1
        self._numa_cpu_map = {}
        num_gpus = len(self._gpu_ids)

        # Calculate how many CPUs to allocate for each GPU. Ensure this number
        # is a power of 2.
        num_cpus = 0
        for i in range(num_numa_nodes):
            num_cpus += len(numa.node_to_cpus(i))
        num_cpus_per_gpu = min(MAX_CPUS_PER_GPU, max(num_cpus // num_gpus, 1))
        num_cpus_per_gpu = pow(2, round(math.log(num_cpus_per_gpu, 2)))

        # Find blocks of contiguous CPUs.
        contiguous_blocks = []
        for i in range(num_numa_nodes):
            cpus = sorted(numa.node_to_cpus(i))
            contiguous_block = [cpus[0]]
            for j in range(1, len(cpus)):
                if (cpus[j] - cpus[j-1] == 1 and
                    len(contiguous_block) < num_cpus_per_gpu):
                    contiguous_block.append(cpus[j])
                else:
                    contiguous_blocks.append((contiguous_block,
                                              len(contiguous_block)))
                    contiguous_block = [cpus[j]]
            if len(contiguous_block) > 0:
                contiguous_blocks.append((contiguous_block,
                                          len(contiguous_block)))
        contiguous_blocks.sort(key=lambda x: x[-1], reverse=True)

        # Assign CPUs to GPUs.
        block_idx = 0
        for i in range(num_gpus):
            self._numa_cpu_map[i] = []
            while len(self._numa_cpu_map[i]) < num_cpus_per_gpu:
                self._numa_cpu_map[i] += contiguous_blocks[block_idx][0]
                block_idx = (block_idx + 1) % len(contiguous_blocks)
            self._logger.info('GPU {gpu} assigned CPUs {cpus}'.format
                    (gpu=i, cpus=str(self._numa_cpu_map[i])))

    def _mps_status(self):
        """Returns True if MPS is running."""
        command = 'ps -ef | grep mps'
        output = subprocess.run(command, stdout=subprocess.PIPE, check=True,
                                shell=True).stdout.decode('utf-8').strip()
        running = False
        for line in output.split('\n'):
            if 'nvidia-cuda-mps-control -d' in line:
                running = True
                break
        return running

    def _enable_mps(self):
        # Set logging environment variables.
        os.environ['CUDA_MPS_PIPE_DIRECTORY'] = CUDA_MPS_PIPE_DIRECTORY
        os.environ['CUDA_MPS_LOG_DIRECTORY'] = CUDA_MPS_LOG_DIRECTORY

        # Start the daemon.
        command = 'nvidia-cuda-mps-control -d'

        try:
            output = \
                subprocess.run(command, stdout=subprocess.PIPE,
                               check=True,
                               shell=True).stdout.decode('utf-8').strip()
            self._logger.info('Successfully enabled CUDA MPS')
            return True
        except subprocess.CalledProcessError as e:
            self._logger.error('Unable to start CUDA MPS!')
            traceback.print_exc()
        return False

    def _shutdown_mps(self):
        command = 'echo quit | nvidia-cuda-mps-control'
        subprocess.run(command, shell=True)
        self._logger.info('Shut down CUDA MPS')

    def _construct_command(self, job_desc, gpus_per_server):
        checkpoint_dir = os.path.join(self._checkpoint_dir,
                                      'job_id=%d' % (job_desc.job_id))
        with self._lock:
            if not os.path.isdir(checkpoint_dir):
                os.mkdir(checkpoint_dir)

        #if job.needs_data_dir:
        #    command = job.command % (self._data_dir)
        #else:
        command = job_desc.command
        command = '%s %s' % (command, checkpoint_dir)

        #command = '%s --local_rank %d' % (command, gpu_id)
        #command = '%s %s %d' % (command, job.num_steps_arg, job.total_steps)
        #command = '%s --enable_synergy_iterator' % (command)

        prefix = None

        return prefix, command

    def _get_steps_and_execution_time(self, job_id, worker_id, round_id):
        checkpoint_dir = os.path.join(self._checkpoint_dir,
                                      'job_id=%d' % (job_id))
        synergy_dir = os.path.join(checkpoint_dir, '.synergy')
        round_dir = os.path.join(synergy_dir, 'round={0}'.format(round_id))
        log_file = os.path.join(round_dir, 'worker={0}.log'.format(worker_id))

        steps = 0
        execution_time = 0
        with open(log_file, 'r') as f:
            for line in f:
                match = re.match('\[(.*)\] \[(.*)\] \[(.*)\]\ ?(.*)', line)
                if match is None:
                    self._logger.error(
                        'Malformed log file: {0}'.format(line))
                    continue
                timestamp = match.group(1)
                event = match.group(2)
                status = match.group(3)
                message = match.group(4)

                if event == 'PROGRESS':
                    if status == 'STEPS':
                        steps = int(message)
                    elif status == 'DURATION':
                        execution_time = float(message)

        return steps, execution_time

    def _kill_job(self, pid):
        self._logger.debug('Killing process {0}'.format(pid))
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError as e:
            self._logger.debug('Could not find process {0}'.format(pid))
        except Exception as e:
            self._logger.error(
                'Could not kill process {0}: {1}'.format(pid, e))

    def _kill_jobs(self, job_id=None):
        with self._lock:
            if job_id is not None:
                self._logger.debug('Killing job {0}...'.format(job_id))
            else:
                self._logger.debug('Killing all jobs!')
            if job_id is not None:
                if (job_id not in self._commands or
                    len(self._commands[job_id]) == 0):
                    self._logger.warning(
                        'No commands found for job {0}'.format(job_id))
                    return
                round_id = min(self._commands[job_id].keys())
                pids = []
                for command in self._commands[job_id][round_id]:
                    pid = helper.get_pid_for_job(command)
                    if pid is not None:
                        pids.append(pid)
                self._logger.debug(
                    'PIDs for job {0} in round {1}: {2}'.format(
                        job_id, round_id, pids))
                for pid in pids:
                    self._kill_job(pid)
            else:
                for job_id in self._job_assignments:
                    if job_id not in self._commands:
                        continue
                    pids = []
                    for round_id in sorted(self._commands[job_id].keys()):
                        for command in self._commands[job_id][round_id]:
                            pid = helper.get_pid_for_job(command)
                            if pid is not None:
                                pids.append(pid)
                        self._logger.debug(
                            'PIDs for job {0} in round {1}: {2}'.format(
                                job_id, round_id, pids))
                        for pid in pids:
                            self._kill_job(pid)
            self._logger.debug('Finished killing job(s)')

    def _get_job_logger_and_fh(self, job_id, worker_id, round_id):
        checkpoint_dir = os.path.join(self._checkpoint_dir,
                                      'job_id=%d' % (job_id))
        synergy_dir = os.path.join(checkpoint_dir, '.synergy')
        round_dir = os.path.join(synergy_dir, 'round={0}'.format(round_id))
        log_file = os.path.join(round_dir, 'worker={0}.log'.format(worker_id))

        with self._lock:
            if not os.path.isdir(synergy_dir):
                os.mkdir(synergy_dir)
            if not os.path.isdir(round_dir):
                os.mkdir(round_dir)

        job_logger = \
            logging.getLogger('job={0}_worker={1}_round={2}'.format(
                job_id, worker_id, round_id))
        job_logger.propagate = False
        job_logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter(ITERATOR_LOG_FORMAT,
                                          datefmt=DATE_FORMAT,
                                          style='{'))
        fh.setLevel(logging.DEBUG)
        job_logger.addHandler(fh)

        return job_logger, fh


    def _get_iterator_log(self, job_id, worker_id, round_id):
        checkpoint_dir = os.path.join(self._checkpoint_dir,
                                      'job_id=%d' % (job_id))
        synergy_dir = os.path.join(checkpoint_dir, '.synergy')
        round_dir = os.path.join(synergy_dir, 'round={0}'.format(round_id))
        log_file = os.path.join(round_dir, 'worker={0}.log'.format(worker_id))

        if not os.path.exists(log_file):
            self._logger.error('Could not read SynergyIterator log file for '
                               'job {job_id} (worker {worker_id}, '
                               'round {round_id})!'.format(
                                   job_id=job_id, worker_id=worker_id,
                                   round_id=round_id))
            return ''

        with open(log_file, 'r') as f:
            return f.read().strip()

    def launch_job(self, job_desc, prefix, command, gpus_this_server, round_id):
        output = ''
        cwd = os.path.join(self._run_dir, "./")
        #cwd = os.path.join(self._run_dir, job.working_directory)
        job_id = job_desc.job_id

        # Log job dispatch.
        job_logger, fh = self._get_job_logger_and_fh(job_id, self._machine_id,
                                                     round_id)
        job_logger.info('', extra={'event': 'DISPATCHER', 'status': 'LAUNCH'})


        if prefix is not None:
            full_command = '{0} {1}'.format(prefix, command)
        else:
            full_command = command

        # Try to dispatch job.
        job_succeeded = False
        try:
            proc = subprocess.run(full_command,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  cwd=cwd,
                                  env=job_desc.env,
                                  shell=True)
            output = proc.stdout.decode('utf-8').strip()
            job_succeeded = True
        except subprocess.CalledProcessError as e:
            error_message = ('Job {job_id} (worker {worker_id}, '
                             'round {round_id}) failed!').format(
                                     job_id=job_id, worker_id=self._machine_id,
                                     round_id=round_id)
            self._logger.error(error_message)
            traceback.print_exc()
            if e.stdout is not None:
                self._logger.debug('Job {job_id} (worker {worker_id}, '
                                   'round {round_id}) '
                                   'stdout:\n{output}'.format(
                                       job_id=job_id, worker_id=self._machine_id,
                                       round_id=round_id, stdout=e.stdout))
            if e.stderr is not None:
                self._logger.debug('Job {job_id} (worker {worker_id}, '
                                   'round {round_id}) '
                                   'stderr:\n{output}'.format(
                                       job_id=job_id, worker_id=self._machine_id,
                                       round_id=round_id, stderr=e.stderr))
            self._kill_jobs(job_id=job_id)
        except Exception as e:
            self._logger.error('Dispatcher failed to launch job {job_id} '
                               '(worker {worker_id}, round {round_id})!'.format(
                                   job_id=job_id, worker_id=self._machine_id,
                                   round_id=round_id))
            traceback.print_exc()
            self._kill_jobs(job_id=job_id)

        # Log job completion and retrieve log output.
        job_logger.info('', extra={'event': 'DISPATCHER',
                                   'status': 'COMPLETE'})
        job_logger.removeHandler(fh)
        fh.close()
        iterator_log = self._get_iterator_log(job_id, self._machine_id, round_id)


        if not job_succeeded:
            return [job_id, 0, 0, iterator_log]

        try:
            completed_steps, execution_time = \
                self._get_steps_and_execution_time(job_id, self._machine_id,
                                                   round_id)
        except Exception as e:
            traceback.print_exc()
            self._logger.error('Could not get steps and execution time for '
                               'job {job_id} (worker {worker_id},  '
                               'round {round_id})!'.format(
                                   job_id=job_id, worker_id=self._machine_id,
                                   round_id=round_id))
            self._er.debug(
                'Job ID: {job_id}, '
                'Worker ID: {worker_id}, '
                'Round: {round_id}, '
                'Output:\n{output}'.format(
                    job_id=job_id, worker_id=self._machine_id, round_id=round_id,
                    output=output))
            return [job.job_id, 0, 0, iterator_log]

        self._logger.info(
            'Job ID: {job_id}, '
            'Worker ID: {worker_id}, '
            'Round: {round_id}, '
            'Num steps: {num_steps}, '
            'Execution time: {execution_time:.2f} seconds, '
            'Output:\n{output}'.format(
                job_id=job_id, worker_id=self._machine_id, round_id=round_id,
                num_steps=completed_steps,
                execution_time=execution_time,
                output=output))

        return [job_id, execution_time, completed_steps, iterator_log]

    def _dispatch_job_helper(self, job_desc, round_id):
        job_id = [job_desc.job_id]
        gpus_this_server = job_desc.env['SYNERGY_GPUS_ALLOCATED']
        gpus_this_server = gpus_this_server.split(',')
        gpus_this_server = [int(gpu) for gpu in gpus_this_server]

        cpus_this_server = job_desc.env['SYNERGY_CPUS_ALLOCATED']
        cpus_this_server = cpus_this_server.split(',')
        cpus_this_server = [int(cpu) for cpu in cpus_this_server]


        self._logger.info(
            'Requesting GPU for job {0} (GPUs {1}, CPUs {2} round {3})...'.format(
                job_id, gpus_this_server, cpus_this_server, round_id))

        self._logger.debug('Constructing commands for '
                           'job {0}, worker {1}...'.format(job_id, gpus_this_server))

        success = True
        try:
            prefix, command = \
                    self._construct_command(job_desc, gpus_this_server)
        except Exception as e:
            self._logger.error('Failed to construct command '
                                   'for job {0}!'.format(job.job_id))
            traceback.print_exc()
            success = False

        if success:
            # Launch the jobs.
            self._logger.info(
                    'Running job {job_id} (worker {worker_id}, '
                    'round {round_id})  '
                    'prefix: {prefix}, '
                    'command: "{command}"'.format(
                        job_id=job_id, worker_id=gpus_this_server,
                        round_id=round_id, 
                        prefix=('N/A' if prefix is None else prefix),
                        command=command))
            result = self._thread_pool.apply_async(
                        self.launch_job,
                        (job_desc, prefix, command, gpus_this_server, round_id,))
            job_result = result.get()
        else:
            job_result = [job_id, 0, 0, '']

        self._logger.info(job_result)

        # Cleanup and notify the scheduler.
        self._logger.debug('Job {0} has completed, '
                               'notifying scheduler...'.format(job_id))
        # self._worker_rpc_client.notify_scheduler(gpus_this_server,
        #                                         job_result)

    def dispatch_job(self, job_desc, round_id):
        #self._dispatch_jobs_helper(job_desc, round_id)
        result = self._thread_pool.apply_async(self._dispatch_job_helper,
                                      (job_desc, round_id, ))

        return result

    def reset(self):
        self._logger.debug('Resetting dispatcher...')
        self._kill_jobs()
        self._job_assignments = {}
        self._thread_pool = ThreadPool()
        self._gpu_queue = queue.Queue(len(self._gpu_ids))
        for gpu_id in self._gpu_ids:
            self._gpu_queue.put(gpu_id)
        self._logger.debug('Finished resetting dispatcher')

    def shutdown(self, shut_down_mps=True):
        self._logger.debug('Shutting down dispatcher...')
        self._kill_jobs()
        self._thread_pool.terminate()
        if self._use_mps and shut_down_mps and not self._mps_initially_enabled:
            self._shutdown_mps()
        self._logger.debug('Finished shutting down dispatcher')
