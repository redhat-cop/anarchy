#!/bin/sh

NSS_WRAPPER_PASSWD=/ansible/dynamic/passwd
NSS_WRAPPER_GROUP=/ansible/dynamic/group

cp /etc/passwd $NSS_WRAPPER_PASSWD
cp /etc/group $NSS_WRAPPER_GROUP
echo ansible:x:$(id -u):$(id -g):ansible:/ansible/dynamic:/bin/bash >> $NSS_WRAPPER_PASSWD

export KUBECONFIG=/ansible/dynamic/kubeconfig
export ANSIBLE_LIBRARY=/ansible/dynamic/modules:/ansible/modules
export LD_PRELOAD=libnss_wrapper.so
export NSS_WRAPPER_PASSWD
export NSS_WRAPPER_GROUP

echo "${VARS}" > /ansible/dynamic/vars.yaml
echo "${TASKS}" > /ansible/dynamic/tasks.yaml

PATH="/ansible/bin:${PATH}"

exec ansible-playbook playbook.yaml
