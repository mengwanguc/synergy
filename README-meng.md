# install docker

```
# Uninstall old versions
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do sudo apt-get remove $pkg; done

# Add Docker's official GPG key:
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add the repository to Apt sources:
echo \
  "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  "$(. /etc/os-release && echo "$VERSION_CODENAME")" stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

# install docker packages
sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

## Verify docker installation

```
sudo docker run hello-world
```

# install Nvidia Container Toolkit

```
# Configure the repository
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list \
  && \
    sudo apt-get update

# Install the NVIDIA Container Toolkit packages
sudo apt-get install -y nvidia-container-toolkit
```

## Configure Nvidia container runtime to be docker
```
# configure runtime
sudo nvidia-ctk runtime configure --runtime=docker

# restart docker
sudo systemctl restart docker
```

## Run a sample GPU workload
```
sudo docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi
```

<!-- # Install nvidia-docker2

```
sudo apt-get install nvidia-docker2
sudo pkill -SIGHUP dockerd
``` -->


# Install Synergy-CoorDL docker

## Option 1: Build by ourselves. However, this custom installation has some problem with DALI dependencies installation.
```
git clone https://github.com/mengwanguc/Synergy-CoorDL.git
cd Synergy-CoorDL
git submodule sync --recursive && git submodule update --init --recursive
git checkout iterator_chk
cd docker
CREATE_RUNNER="YES" sudo ./build.sh
```

## Option 2: use the compiled docker provided:
```
docker pull jayashreemohan/synergy_dali:latest
```

# Run the docker

```
sudo docker run --runtime=nvidia --ipc=host --mount src=/,target=/datadrive/,type=bind -it --rm --network=host --privileged jayashreemohan/synergy_dali:latest
```


# Run Synergy inside the docker

