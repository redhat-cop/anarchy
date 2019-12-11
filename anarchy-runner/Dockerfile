FROM registry.access.redhat.com/ubi8/ubi:latest

ENV HELM_VERSION=3.0.0 \
    OCP_VERSION=4.2.9 \
    PYTHON_VERSION=3.7 \
    PYTHON_RELEASE=3.7.5

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
 && curl https://www.python.org/ftp/python/${PYTHON_RELEASE}/Python-${PYTHON_RELEASE}.tgz \
      -o /usr/local/src/Python-${PYTHON_RELEASE}.tgz \
 && tar zxf /usr/local/src/Python-${PYTHON_RELEASE}.tgz -C /usr/local/src \
 && cd /usr/local/src/Python-${PYTHON_RELEASE} \
 && ./configure --enable-optimizations \
 && make altinstall \
 && cd /usr/local/bin \
 && rm /usr/local/src/Python-${PYTHON_RELEASE}.tgz \
 && ln -s idle${PYTHON_VERSION} idle3 \
 && ln -s python${PYTHON_VERSION} python3 \
 && ln -s pip${PYTHON_VERSION} pip3 \
 && ln -s pydoc${PYTHON_VERSION} pydoc3 \
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
