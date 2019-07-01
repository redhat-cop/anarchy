FROM registry.access.redhat.com/ubi8/python-36:latest

USER 0

RUN pip install --ignore-installed \
      flask \
      gevent \
      kubernetes

COPY server.py /usr/local/bin/server.py

USER 1000

CMD /usr/local/bin/server.py
