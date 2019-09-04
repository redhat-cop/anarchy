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
from anarchyevent import AnarchyEvent

flask_api = flask.Flask('rest')
runtime = AnarchyRuntime()

# Variables initialized during init()
api_load_complete = False
event_load_complete = False
governor_load_complete = False

# Action running globals
action_cache_lock = threading.Lock()
action_cache = {}

def init():
    """Initialization function before management loops."""
    init_apis()
    init_governors()
    init_events()
    runtime.logger.debug("Completed init")

def init_apis():
    """Get initial list of anarchy apis"""
    global api_load_complete
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyapis'
    ).get('items', []):
        AnarchyAPI.register(resource)
    api_load_complete = True

def init_events():
    """Load events that require processing"""
    global event_load_complete
    anarchy_events = []
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyevents',
        label_selector = runtime.runner_label
    ).get('items', []):
        runner = resource['metadata']['labels'].get(runtime.runner_label, None)
        if runner \
        and runner not in ('failed' 'pending') \
        and not runner.startswith('failed-') \
        and not runner.startswith('success-'):
            runtime.anarchy_runners[runner] = 0
        anarchy_events.append(AnarchyEvent(resource))

    anarchy_events.sort(key=lambda x: x.creation_timestamp)
    for anarchy_event in anarchy_events:
        AnarchyEvent.register(anarchy_event, runtime)

    for pod in runtime.core_v1_api.list_namespaced_pod(
        runtime.operator_namespace,
        label_selector = 'name=anarchy-runner'
    ).items:
        runner = pod.metadata.name
        if pod.status.phase == 'Running':
            runtime.register_runner(runner)

    check_for_lost_runners()
    event_load_complete = True

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
        if not event_load_complete:
            runtime.logger.warn('Wating for event init')
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
    resource = event['object']
    anarchy_subject = AnarchySubject.cache_update(resource)
    if event['type'] in ['ADDED', 'MODIFIED', None]:
        if not anarchy_subject:
            anarchy_subject = AnarchySubject(resource)
        if anarchy_subject.is_pending_delete:
            anarchy_subject.handle_delete(runtime, logger)

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

def anarchy_events_loop():
    while True:
        anarchy_subject = AnarchySubject.get_pending(runtime)
        runtime.logger.info('Running pending event for %s', anarchy_subject.name)
        anarchy_event = anarchy_subject.get_event_to_run(runtime)
        if anarchy_event:
            anarchy_runner = runtime.get_available_runner()
            runtime.logger.info('Assigned runner %s to AnarchyEvent %s', anarchy_runner, anarchy_event.name)
            anarchy_event.set_runner(anarchy_runner, runtime)

def start_anarchy_events_loop():
    while True:
        try:
            anarchy_events_loop()
        except Exception as e:
            runtime.logger.exception("Error in anarchy_events_loop!")
            time.sleep(10)

def start_actions():
    due_actions = {}
    for key, action in action_cache.items():
        if action.after_datetime > datetime.utcnow():
            continue
        due_actions[key] = action
    for key, action in due_actions.items():
        try:
            if action.start(runtime):
                del action_cache[key]
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

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyevents')
def handle_event_event(event, logger, **_):
    wait_for_init()
    anarchy_event = AnarchyEvent(event['object'])
    if event['type'] == 'DELETED':
        AnarchyEvent.unregister(anarchy_event, runtime)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyEvent.register(anarchy_event, runtime)

@kopf.on.event('', 'v1', 'pods')
def handle_ansible_runner_event(event, logger, **_):
    wait_for_init()
    pod = event['object']
    pod_meta = pod['metadata']
    pod_name = pod_meta['name']
    if pod_meta.get('labels', {}).get('name', '') != 'anarchy-runner':
        logger.debug('not an anarchy-runner pod')
        return
    if event['type'] == 'DELETED':
        AnarchyEvent.handle_lost_runner(pod_name, runtime)
        runtime.remove_runner(pod_name)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        pod_status = pod['status']
        if pod['status']['phase'] == 'Running':
            runtime.register_runner(pod_name)
        else:
            AnarchyEvent.handle_lost_runner(pod_name, runtime)
            runtime.remove_runner(pod_name)

    # Check for lost runner pods every 15 minutes
    if runtime.last_lost_runner_check < time.time() - 900:
        check_for_lost_runners()

def check_for_lost_runners():
    runtime.last_lost_runner_check = time.time()
    lost_runner_pods = []
    for runner_name, last_running_time in runtime.anarchy_runners.items():
        if last_running_time < time.time() - 900:
            runtime.logger.warning('Lost runner pod %s', runner_name)
            AnarchyEvent.handle_lost_runner(runner_name, runtime)
            lost_runner_pods.append(runner_name)
    for runner_name in lost_runner_pods:
        del runtime.anarchy_runners[runner_name]

def __event_callback(anarchy_action_name, event_name):
    if not flask.request.json:
        flask.abort(400)
        return

    try:
        anarchy_action_resource = runtime.custom_objects_api.get_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace,
            'anarchyactions', anarchy_action_name
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            runtime.logger.warning("Callback for AnarchyAction %s not found", anarchy_action_name)
            flask.abort(404)
            return
        else:
            raise

    anarchy_action = AnarchyAction(anarchy_action_resource)

    if not anarchy_action.check_callback_token(flask.request.headers.get('Authorization')):
        runtime.logger.warning("Invalid callback token for AnarchyAction %s", anarchy_action_name)
        flask.abort(403)
        return

    anarchy_action.process_event(runtime, flask.request.json)
    return flask.jsonify({'status': 'ok'})

@flask_api.route(
    '/event/<string:anarchy_action_name>',
    methods=['POST']
)
def action_callback(anarchy_action_name):
    runtime.logger.info("Action callback for %s", anarchy_action_name)
    return __event_callback(anarchy_action_name, None)

@flask_api.route(
    '/event/<string:anarchy_action_name>/<string:event_name>',
    methods=['POST']
)
def event_callback(anarchy_action_name, event_name):
    runtime.logger.info("AnarchyAction %s received callback event %s", anarchy_action_name, event_name)
    return __event_callback(anarchy_action_name, event_name)

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'anarchy-events',
        target = start_anarchy_events_loop
    ).start()

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
