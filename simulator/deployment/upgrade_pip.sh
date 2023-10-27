#!/bin/sh

pip install --upgrade protobuf

# alias python=python3
pip install --upgrade pip
# python -m pip install --upgrade setuptools
pip install --upgrade setuptools
pip install --no-cache-dir  grpcio 
pip install --no-cache-dir  grpcio-tools 
pip install --no-cache-dir  python-numa
# sudo apt install python3.8-dev
# python -m pip install psutil 
pip install psutil 

sudo apt-get install libnuma-dev  # this is required for "pip install numa"