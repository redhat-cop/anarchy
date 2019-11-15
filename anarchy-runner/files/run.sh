#!/bin/sh

export HOME=/anarchy-runner
export ANSIBLE_CONFIG=$HOME/ansible-runner/ansible.cfg
export KUBECONFIG=$HOME/kubeconfig
export LD_PRELOAD=libnss_wrapper.so
export NSS_WRAPPER_PASSWD=$HOME/passwd
export NSS_WRAPPER_GROUP=$HOME/group
export OPERATOR_NAMESPACE="$(cat /run/secrets/kubernetes.io/serviceaccount/namespace)"

cp /etc/passwd $NSS_WRAPPER_PASSWD
cp /etc/group $NSS_WRAPPER_GROUP
echo ansible:x:$(id -u):$(id -g):ansible:$HOME:/bin/bash >> $NSS_WRAPPER_PASSWD

exec python3 /anarchy-runner/anarchy-runner.py
