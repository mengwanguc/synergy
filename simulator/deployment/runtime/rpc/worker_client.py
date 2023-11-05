#!/usr/bin/env python3

import logging
import os
import sys
import time

import grpc

sys.path.append(os.path.join(os.path.dirname(__file__), '../rpc_stubs'))
import worker_to_scheduler_pb2 as w2s_pb2
import worker_to_scheduler_pb2_grpc as w2s_pb2_grpc

LOG_FORMAT = '{name}:{levelname} [{asctime}] {message}'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

class WorkerRpcClient:
    """Worker client for sending RPC requests to a scheduler server."""

    def __init__(self, worker_ip_addr, worker_port,
                 sched_ip_addr, sched_port):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT,
                                          style='{'))
        logger.addHandler(ch)
        self._logger = logger
        self._worker_ip_addr = worker_ip_addr
        self._worker_port = worker_port
        self._sched_loc = '{}:{}'.format(sched_ip_addr, sched_port)

    def register_worker(self, num_gpus):
        request = w2s_pb2.RegisterWorkerRequest(
            num_gpus=num_gpus,
            ip_addr=self._worker_ip_addr,
            port=self._worker_port)
        with grpc.insecure_channel(self._sched_loc) as channel:
            self._logger.info("Trying to register worker...")
            stub = w2s_pb2_grpc.WorkerToSchedulerStub(channel)
            response = stub.RegisterWorker(request)
            self._logger.info("Trying to register worker : Got response {}...".format(response.success))
            if response.success:
                self._logger.info("Successfully registered worker client to scheduler")
                return (response.success, response.round_duration, response.machine_id)
            else:
                self._logger.error('Failed to register worker!')
               
                return (response.success, None, None)

    
    def notify_scheduler(self, worker_id, job_descriptions):
        # Send a Done message.
        self._logger.info('hi')
        self._logger.debug('worker {} notifying scheduler for job {}'.format(
                        worker_id, job_descriptions))
        self._logger.debug('start request = w2s_pb2.DoneRequest()')
        request = w2s_pb2.DoneRequest()
        self._logger.debug('finished request = w2s_pb2.DoneRequest()')
        request.worker_id = worker_id
        for job_description in job_descriptions:
            request.job_id.append(job_description[0])
            request.execution_time.append(job_description[1])
            request.num_steps.append(job_description[2])
            request.iterator_log.append(job_description[3])
        with grpc.insecure_channel(self._sched_loc) as channel:
            self._logger.debug('stub = w2s_pb2_grpc.WorkerToSchedulerStub(channel)')
            stub = w2s_pb2_grpc.WorkerToSchedulerStub(channel)
            response = stub.Done(request)
            job_ids = \
              [job_description[0] for job_description in job_descriptions]
            if len(job_ids) == 1:
              self._logger.info('Notified scheduler that '
                                 'job {0} has completed'.format(job_ids[0]))
            else:
              self._logger.info('Notified scheduler that '
                                 'jobs {0} have completed'.format(job_ids))

 
