FROM registry.access.redhat.com/ubi8/python-36:latest

ARG OCP_VERSION=4.1.8

USER 0

RUN yum install -y nss_wrapper \
 && pip install --ignore-installed \
      ansible \
      jmespath \
      kubernetes \
 && mkdir /ansible \
 && mkdir /ansible/bin \
 && mkdir /ansible/dynamic \
 && chmod a+rwx /ansible/dynamic \
 && curl -s https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OCP_VERSION}/openshift-client-linux-${OCP_VERSION}.tar.gz \
  | tar zxvf - -C /bin

COPY hosts ansible.cfg playbook.yaml run.sh /ansible/

USER 1000

WORKDIR /ansible
CMD ["/ansible/run.sh"]
