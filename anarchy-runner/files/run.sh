#!/bin/sh

NSS_WRAPPER_PASSWD=/anarchy-runner/passwd
NSS_WRAPPER_GROUP=/anarchy-runner/group

cp /etc/passwd $NSS_WRAPPER_PASSWD
cp /etc/group $NSS_WRAPPER_GROUP
echo ansible:x:$(id -u):$(id -g):ansible:/anarchy-runner:/bin/bash >> $NSS_WRAPPER_PASSWD

export HOME=/anarchy-runner
export KUBECONFIG=/anarchy-runner/kubeconfig
export LD_PRELOAD=libnss_wrapper.so
export NSS_WRAPPER_PASSWD
export NSS_WRAPPER_GROUP
export OPERATOR_NAMESPACE="$(cat /run/secrets/kubernetes.io/serviceaccount/namespace)"

exec kopf run --standalone --namespace $OPERATOR_NAMESPACE /anarchy-runner/anarchy-runner.py
