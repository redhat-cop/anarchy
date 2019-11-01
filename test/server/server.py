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
domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')

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
        extra_vars = flask.request.json.get('extra_vars', None)
        assert extra_vars, 'extra_vars not provided'
        job_vars = extra_vars.get('job_vars', None)
        assert job_vars, 'job_vars not provided in extra_vars'
        job_meta = job_vars.get('__meta__', None)
        assert job_meta, '__meta__ not provided in extra_vars.job_vars'
        callback = job_meta.get('callback', None)
        assert callback, 'callback not provided in extra_vars.job_vars.__meta__'
        callback_token = callback.get('token', None)
        callback_url = callback.get('url', None)
        assert callback_token, 'callback_token not provided in extra_vars.job_vars.__meta__.callback'
        assert callback_url, 'callback_url not provided in extra_vars.job_vars.__meta__.callback'
        deployer = job_meta.get('deployer', None)
        assert deployer, 'deployer not provided in extra_vars.job_vars.__meta__'
        deployer_entry_point = deployer.get('entry_point', None)
        assert deployer_entry_point, 'entry_point not provided in extra_vars.job_vars.__meta__.deployer'
        tower = job_meta.get('tower', None)
        assert tower, 'tower not provided in extra_vars.job_vars.__meta__'
        tower_action = tower.get('action', None)
        assert tower_action, 'action not provided in extra_vars.job_vars.__meta__.tower'
        aws_access_key_id = job_vars.get('aws_access_key_id', None)
        assert aws_access_key_id, 'aws_access_key_id not provided in extra_vars.job_vars'
        assert aws_access_key_id == 'th3k3y', 'aws_access_key_id not correct'
        aws_secret_access_key = job_vars.get('aws_secret_access_key', None)
        assert aws_secret_access_key, 'aws_secret_access_key not provided in extra_vars.job_vars'
        assert aws_secret_access_key == 'th34cc355', 'aws_secret_access_key not correct'
    except Exception as e:
        logger.exception("Invalid parameters passed to job-runner launch: " + str(e))
        flask.abort(400)

    logger.info("Callback URL %s", callback_url)

    job_id = random.randint(1,10000000)
    schedule_callback(
        after=time.time() + 1,
        event='started',
        job_id=job_id,
        msg='started ' + tower_action,
        token=callback_token,
        url=callback_url
    )
    schedule_callback(
        after=time.time() + 10,
        event='complete',
        job_id=job_id,
        msg='completed ' + tower_action,
        token=callback_token,
        url=callback_url
    )
    return flask.jsonify({
        "id": job_id,
        "job": job_id
    })

@api.route('/runner/<string:runner_queue_name>/<string:runner_name>', methods=['GET', 'POST'])
def runner_get(runner_queue_name, runner_name):
    if flask.request.method == 'POST':
        logger.info("RUNNER POST %s %s %s", runner_queue_name, runner_name, flask.request.json)
        return flask.jsonify(None)

    logger.info("RUNNER GET")
    n = random.randint(0,2)
    if n == 0:
        return flask.jsonify(None)
    else:
        if n == 1:
            tasks = []
        else:
            tasks = [{
                'name': 'test failure',
                'fail': {
                    'msg': 'test failure'
                }
            }]
        return flask.jsonify({
            'apiVersion': domain + '/v1',
            'kind': 'AnarchyEvent',
            'metadata': {
                'name': 'test'
            },
            'spec': {
                'action': {
                    'metadata': {
                        'name': 'test-action'
                    }
                },
                'event': {
                    'name': 'test',
                    'data': {
                    },
                    'tasks': tasks
                },
                'governor': {
                    'metadata': {
                        'name': 'test-governor'
                    }
                },
                'subject': {
                    'metadata': {
                        'name': 'test-subject'
                    }
                }
            }
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
