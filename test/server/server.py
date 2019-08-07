#!/usr/bin/env python3

import base64
import datetime
import flask
import gevent.pywsgi
import json
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
callbacks = {}

def init():
    global logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(os.environ.get('LOGGING_LEVEL', 'DEBUG'))
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(threadName)s - %(message)s')
    handler.setFormatter(formatter)
    logger = logging.getLogger()
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'DEBUG'))
    logger.addHandler(handler)

def schedule_callback(after, event, job_id, msg, token, url):
    callbacks[(job_id,event)] = {
        "after": after,
        "token": token,
        "url": url,
        "data": {
            "event": event,
            "job_id": job_id,
            "msg": msg,
        }
    }

def callback_loop():
    while True:
        completed = []
        for key, callback in callbacks.copy().items():
            if time.time() < callback['after']:
                continue
            resp = requests.post(
                callback['url'],
                json=callback['data'],
                headers={ "Authorization": "Bearer " + callback['token'] },
                verify=False
            )
            logger.info(
                "%s - %s: %s",
                callback['url'],
                resp.status_code,
                resp.text
            )
            del callbacks[key]
        time.sleep(1)

@api.route('/api/v2/job_templates/job-runner/launch/', methods=['POST'])
def launch():
    logger.info("Call to job job-runner launch")

    try:
        assert flask.request.json, \
            'no json data provided'
        assert 'extra_vars' in flask.request.json, \
            'extra_vars not provided'
        assert 'babylon_callback_url' in flask.request.json['extra_vars'], \
            'babylon_callback_url not provided in extra_vars'
        assert 'babylon_callback_token' in flask.request.json['extra_vars'], \
            'babylon_callback_token not provided in extra_vars'
        assert 'babylon_deployer_action' in flask.request.json['extra_vars'], \
            'babylon_deployer_action not provided in extra_vars'
    except Exception as e:
        logger.exception("Invalid parameters passed to job-runner launch: " + str(e))
        flask.abort(400)

    logger.info("Callback URL %s", flask.request.json['extra_vars']['babylon_callback_url'])

    job_id = random.randint(1,10000000)
    schedule_callback(
        after=time.time() + 1,
        event='started',
        job_id=job_id,
        msg='started ' + flask.request.json['extra_vars']['babylon_deployer_action'],
        token=flask.request.json['extra_vars']['babylon_callback_token'],
        url=flask.request.json['extra_vars']['babylon_callback_url']
    )
    schedule_callback(
        after=time.time() + 15,
        event='complete',
        job_id=job_id,
        msg='completed ' + flask.request.json['extra_vars']['babylon_deployer_action'],
        token=flask.request.json['extra_vars']['babylon_callback_token'],
        url=flask.request.json['extra_vars']['babylon_callback_url']
    )
    return flask.jsonify({
        "id": job_id,
        "job": job_id
    })

def main():
    """Main function."""

    init()
    logger.info("Starting test server")

    threading.Thread(
        name = 'callback',
        target = callback_loop
    ).start()

    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
