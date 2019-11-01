FROM registry.access.redhat.com/ubi8/ubi:latest

ENV OCP_VERSION=4.2.2 \
    PYTHON_VERSION=3.7 \
    PYTHON_RELEASE=3.7.5

USER 0:0

COPY files /operator

RUN yum install -y \
      gcc \
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
 && pip3 install -r /operator/requirements.txt --upgrade \
 && mkdir -p /operator/nss \
 && chmod a+rwx /operator/nss \
 && curl -s https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux-${OCP_VERSION}.tar.gz \
    | tar zxvf - -C /bin

USER 1000

CMD /operator/run.sh
