#!/usr/bin/env python

from datetime import datetime, timedelta, timezone
import flask
import gevent.pywsgi
import kopf
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import sys
import threading
import time
import yaml

from anarchyruntime import AnarchyRuntime
from anarchyapi import AnarchyAPI
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction

action_pod_keep_seconds = int(os.environ.get('ACTION_POD_KEEP_SECONDS', 600))
flask_api = flask.Flask('rest')
runtime = AnarchyRuntime()

# Variables initialized during init()
api_load_complete = False
governor_load_complete = False

# Action running globals
action_cache_lock = threading.Lock()
action_cache = {}

def init():
    """Initialization function before management loops."""
    init_apis()
    init_governors()
    runtime.logger.debug("Completed init")

def init_apis():
    """Get initial list of anarchy apis"""
    global api_load_complete
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyapis'
    ).get('items', []):
        AnarchyAPI.register(resource)
    api_load_complete = True

def init_governors():
    """Get initial list of anarchy governors"""
    global governor_load_complete
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchygovernors'
    ).get('items', []):
        AnarchyGovernor.register(resource)
    governor_load_complete = True

def wait_for_init():
    while not api_load_complete or not governor_load_complete:
        if not api_load_complete:
            runtime.logger.warn('Wating for api init')
        if not governor_load_complete:
            runtime.logger.warn('Wating for governor init')
        time.sleep(1)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyapis')
def handle_api_event(event, logger, **_):
    if event['type'] == 'DELETED':
        AnarchyAPI.unregister(event['object']['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyAPI.register(event['object'])

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchygovernors')
def handle_governor_event(event, logger, **_):
    if event['type'] == 'DELETED':
        AnarchyGovernor.unregister(event['object']['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyGovernor.register(event['object'])

@kopf.on.create(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_create(body, logger, **_):
    wait_for_init()
    subject = AnarchySubject(body)
    subject.handle_create(runtime, logger)

@kopf.on.update(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_update(body, logger, **_):
    wait_for_init()
    subject = AnarchySubject(body)
    subject.handle_update(runtime, logger)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_event(event, logger, **_):
    wait_for_init()
    if event['type'] in ['ADDED', 'MODIFIED', None]:
        subject = AnarchySubject(event['object'])
        if subject.is_pending_delete:
            subject.handle_delete(runtime, logger)

def cache_action(action):
    if action.has_started \
    or action.after_datetime > datetime.utcnow() + timedelta(minutes=20):
        return
    cache_key = (action.action, action.subject_name)
    action_cache_lock.acquire()
    action_cache[cache_key] = action
    action_cache_lock.release()

def cache_remove_action(action):
    cache_key = (action.action, action.subject_name)
    action_cache_lock.acquire()
    if cache_key in action_cache:
        del action_cache[cache_key]
    action_cache_lock.release()

def start_actions():
    due_actions = {}
    for key, action in action_cache.items():
        if action.after_datetime > datetime.utcnow():
            continue
        due_actions[key] = action
    for key, action in due_actions.items():
        del action_cache[key]
        try:
            action.start(runtime)
        except Exception as e:
            runtime.logger.exception("Error running action %s", action.name)

def start_actions_loop():
    while True:
        try:
            action_cache_lock.acquire()
            start_actions()
        except Exception as e:
            runtime.logger.exception("Error in start_actions_loop!")
        finally:
            action_cache_lock.release()
            time.sleep(1)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyactions')
def handle_action_event(event, logger, **_):
    wait_for_init()
    action = AnarchyAction(event['object'])
    if event['type'] == 'DELETED':
        cache_remove_action(action)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        cache_action(action)
    else:
        logger.warning(event)

@kopf.on.event('', 'v1', 'pods', labels={runtime.operator_domain + '/event-name': None})
def handle_pod_event(event, logger, **_):
    if event['type'] in ['ADDED', 'MODIFIED', None]:
        pod = event['object']
        pod_meta = pod['metadata']
        pod_creation_timestamp = pod_meta['creationTimestamp']
        pod_name = pod_meta['name']
        pod_phase = pod['status']['phase']

        delete_older_than = (
            datetime.utcnow() - timedelta(0, action_pod_keep_seconds)
        ).strftime('%FT%TZ')

        # Delete pods that have completed, are older than the threshold
        # and are not already marked for deletion.
        if pod_phase in ('Succeeded', 'Failed') \
        and pod_creation_timestamp < delete_older_than \
        and not 'deletetionTimestamp' in pod_meta:
            try:
                runtime.core_v1_api.delete_namespaced_pod(pod_name, pod_namespace)
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise

def __event_callback(action_namespace, action_name, event_name):
    if not flask.request.json:
        flask.abort(400)
        return

    action_resource = runtime.custom_objects_api.get_namespaced_custom_object(
        runtime.operator_domain, 'v1', action_namespace, 'anarchyactions', action_name
    )
    action = AnarchyAction(action_resource)

    if not action.check_callback_token(flask.request.headers.get('Authorization')):
        runtime.logger.warning("Invalid callback token for %s/%s", action_namespace, action_name)
        flask.abort(403)
        return

    action.process_event(runtime, flask.request.json)
    return flask.jsonify({'status': 'ok'})

@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>',
    methods=['POST']
)
def action_callback(action_namespace, action_name):
    runtime.logger.info("Action callback for %s/%s", action_namespace, action_name)
    return __event_callback(action_namespace, action_name, None)

@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>/<string:event_name>',
    methods=['POST']
)
def event_callback(action_namespace, action_name, event_name):
    runtime.logger.info("Event %s called for %s/%s", event_name, action_namespace, action_name)
    return __event_callback(action_namespace, action_name, event_name)

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'start-actions',
        target = start_actions_loop
    ).start()

    prometheus_client.start_http_server(8000)
    http_server = gevent.pywsgi.WSGIServer(('', 5000), flask_api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
else:
    threading.Thread(name='main', target=main).start()
