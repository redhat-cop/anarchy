FROM registry.access.redhat.com/ubi8/python-38:latest

ENV HELM_VERSION=3.2.1 \
    OCP_VERSION=4.3.19

USER 0:0

COPY files /anarchy-runner

RUN yum install -y \
      gcc \
      git \
      openssl-devel \
      bzip2-devel \
      libffi-devel \
      make \
      nss_wrapper \
 && yum clean all \
 && pip3 install -r /anarchy-runner/requirements.txt --upgrade \
 && touch \
      /anarchy-runner/ansible-runner/project/main.yml \
      /anarchy-runner/kubeconfig \
      /anarchy-runner/passwd \
      /anarchy-runner/group \
 && chmod ug=rw,o= \
      /anarchy-runner/ansible-runner/project/main.yml \
      /anarchy-runner/kubeconfig \
      /anarchy-runner/passwd \
      /anarchy-runner/group \
 && mkdir -p \
      /anarchy-runner/.ansible \
      /anarchy-runner/ansible-runner/artifacts \
      /anarchy-runner/ansible-runner/env \
 && chmod ug=rwx,o= \
      /anarchy-runner/.ansible \
      /anarchy-runner/ansible-runner/artifacts \
      /anarchy-runner/ansible-runner/env \
 && curl -s https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz \
    | tar zxvf - -C /bin linux-amd64/helm --strip-components=1 \
 && curl -s https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux-${OCP_VERSION}.tar.gz \
    | tar zxvf - -C /bin

USER 1000

WORKDIR /anarchy-runner
CMD ["/anarchy-runner/run.sh"]
