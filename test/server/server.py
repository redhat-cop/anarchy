#!/usr/bin/env python

import base64
import datetime
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import os
import random
import re
import requests
import socket
import sys
import string
import threading
import time
import yaml

api = flask.Flask('rest')

logging.basicConfig(
    format='%(levelname)s %(threadName)s - %(message)s',
)
logger = logging.getLogger('anarchy')
logger.setLevel(os.environ.get('LOGGING_LEVEL', 'DEBUG'))

jobs = {}

def add_deploy_job(job_template, callback_url, job_id):
    jobs[job_id] = {
        "id": job_id,
        "callback_url": callback_url,
        "callback_events": {
            "started": {
                "after": time.time() + 10,
                "data": {
                    "event": "started",
                    "msg": "deployment of {} {} complete".format(job_template, job_id)
                }
            },
            "complete": {
                "after": time.time() + 30,
                "data": {
                    "event": "complete",
                    "msg": "deployment of {} {} complete".format(job_template, job_id)
                }
            }
        }
    }

def add_destroy_job(job_template, callback_url, job_id):
    jobs[job_id] = {
        "id": job_id,
        "callback_url": callback_url,
        "callback_events": {
            "started": {
                "after": time.time() + 10,
                "data": {
                    "event": "started",
                    "msg": "destroy of {} {} complete".format(job_template, job_id)
                }
            },
            "complete": {
                "after": time.time() + 60,
                "data": {
                    "event": "complete",
                    "msg": "destroy of {} {} complete".format(job_template, job_id)
                }
            }
        }
    }

def callback_loop():
    while True:
        for job_id, job in jobs.copy().items():
            for event_name, event in job['callback_events'].copy().items():
                if time.time() > event['after']:
                    requests.post(job['callback_url'], json=event['data'], verify=False)
                    del job['callback_events'][event_name]
            if not job['callback_events']:
                del jobs[job_id]
        time.sleep(1)

@api.route('/api/v2/job_templates/<job_template>/launch/', methods=['POST'])
def event_callback(job_template):
    logger.info("Call to job template %s", job_template)
    if not flask.request.json:
        flask.abort(400)

    assert 'extra_vars' in flask.request.json, \
        'extra_vars'
    assert 'anarchy_callback_url' in flask.request.json['extra_vars'], \
        'anarchy_callback_url not provided in extra_vars'

    job_id = random.randint(1,10000000)
    if job_template.startswith('deploy'):
        add_deploy_job(
            job_template,
            flask.request.json['extra_vars']['anarchy_callback_url'],
            job_id
        )
    elif job_template.startswith('destroy'):
        add_destroy_job(
            job_template,
            flask.request.json['extra_vars']['anarchy_callback_url'],
            job_id
        )

    return flask.jsonify({
        "id": job_id,
        "job": job_id
    })

def main():
    """Main function."""

    threading.Thread(
        name = 'callback',
        target = callback_loop
    ).start()

    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
