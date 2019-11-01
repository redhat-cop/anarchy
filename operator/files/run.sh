#!/bin/sh

NSS_WRAPPER_PASSWD=/operator/nss/passwd
NSS_WRAPPER_GROUP=/operator/nss/group

cp /etc/passwd $NSS_WRAPPER_PASSWD
cp /etc/group $NSS_WRAPPER_GROUP
echo operator:x:$(id -u):$(id -g):operator:/operator:/bin/sh >> $NSS_WRAPPER_PASSWD

export LD_PRELOAD=libnss_wrapper.so
export NSS_WRAPPER_PASSWD
export NSS_WRAPPER_GROUP
export OPERATOR_NAMESPACE="$(cat /run/secrets/kubernetes.io/serviceaccount/namespace)"

exec kopf run --standalone --namespace $OPERATOR_NAMESPACE /operator/anarchy.py
