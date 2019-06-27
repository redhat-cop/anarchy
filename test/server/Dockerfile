FROM centos:7

USER 0

RUN yum install -y \
      centos-release-openshift-origin311 \
      epel-release \
      gcc \
      git \
      python \
      python-devel \
      python-setuptools \
    && \
    yum upgrade -y && \
    yum clean all && \
    useradd -u 1000 ansible

RUN easy_install pip && \
    pip install --ignore-installed \
      flask \
      gevent \
      kubernetes

COPY server.py /usr/local/bin/server.py

CMD /usr/local/bin/server.py
