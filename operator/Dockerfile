FROM registry.redhat.io/ubi8/python-36:latest

USER 0

RUN pip install --ignore-installed \
      ansible \
      flask \
      gevent \
      jmespath \
      kubernetes \
      ndg-httpsclient \
      prometheus_client \
      pyasn1 \
      pyOpenSSL \
    && \
    mkdir /anarchy

COPY *.py /anarchy/

USER 1000

CMD /anarchy/anarchy.py
