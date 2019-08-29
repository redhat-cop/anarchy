#FROM registry.access.redhat.com/ubi8/python-37:latest
FROM docker.io/library/python:3.7

ARG OCP_VERSION=4.1.8

USER 0:0

COPY files /anarchy-runner

RUN apt-get update \
 && apt-get install -y libnss-wrapper \
 && pip install --ignore-installed \
      ansible \
      ansible-runner \
      jmespath \
      jsonpatch \
      kopf \
      kubernetes \
      openshift \
 && touch \
      /anarchy-runner/kubeconfig \
      /anarchy-runner/passwd \
      /anarchy-runner/group \
 && chmod ug=rw,o= \
      /anarchy-runner/kubeconfig \
      /anarchy-runner/passwd \
      /anarchy-runner/group \
 && mkdir -p \
      /anarchy-runner/.ansible \
      /anarchy-runner/ansible-runner/artifacts \
      /anarchy-runner/ansible-runner/env \
      /anarchy-runner/ansible-runner/project/tasks \
      /anarchy-runner/ansible-runner/project/vars \
 && chmod ug=rwx,o= \
      /anarchy-runner/.ansible \
      /anarchy-runner/ansible-runner/artifacts \
      /anarchy-runner/ansible-runner/env \
      /anarchy-runner/ansible-runner/project/tasks \
      /anarchy-runner/ansible-runner/project/vars \
 && curl -s https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux-${OCP_VERSION}.tar.gz \
  | tar zxvf - -C /bin

USER 1000

WORKDIR /anarchy-runner
CMD ["/anarchy-runner/run.sh"]
