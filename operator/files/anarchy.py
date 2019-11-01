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

api = flask.Flask('rest')
runner_token = os.environ.get('RUNNER_TOKEN', None)

operator_logger = logging.getLogger('operator')
operator_logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))
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
    operator_logger.debug("Completed init")

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
        anarchy_event = AnarchyEvent(resource)
        runner = anarchy_event.get_runner_name(runtime)

        if not runner or runner == 'successful':
            pass
        else:
            anarchy_events.append(anarchy_event)
            if runner != 'pending':
                # If runner is not "pending", then it is either the name of a
                # specific runner or "failed". Either way it is the subject's
                # current event.
                anarchy_subject = anarchy_event.get_subject(runtime)
                anarchy_subject.current_event = anarchy_event.name
                if runner != 'failed':
                    # If the event is recorded as executing on a specific runner
                    # then we need to track the runner in case it has been lost.
                    runtime.anarchy_runners[runner] = 0

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
            operator_logger.info('Wating for api init')
        if not event_load_complete:
            operator_logger.info('Wating for event init')
        if not governor_load_complete:
            operator_logger.info('Wating for governor init')
        time.sleep(1)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyapis')
def handle_api_event(event, **_):
    if event['type'] == 'DELETED':
        AnarchyAPI.unregister(event['object']['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyAPI.register(event['object'])

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchygovernors')
def handle_governor_event(event, **_):
    if event['type'] == 'DELETED':
        AnarchyGovernor.unregister(event['object']['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyGovernor.register(event['object'])

@kopf.on.create(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_create(body, **_):
    wait_for_init()
    subject = AnarchySubject(body)
    subject.handle_create(runtime)

@kopf.on.update(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_update(body, **_):
    wait_for_init()
    subject = AnarchySubject(body)
    subject.handle_update(runtime)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchysubjects')
def handle_subject_event(event, **_):
    wait_for_init()
    resource = event['object']
    anarchy_subject = AnarchySubject.cache_update(resource)
    if event['type'] in ['ADDED', 'MODIFIED', None]:
        if not anarchy_subject:
            anarchy_subject = AnarchySubject(resource)
        if anarchy_subject.is_pending_delete:
            anarchy_subject.handle_delete(runtime)

def cache_action(action):
    if action.has_started \
    or action.after_datetime > datetime.utcnow() + timedelta(minutes=20):
        return
    cache_key = (action.action, action.subject_name)
    action_cache[cache_key] = action

def cache_remove_action(action):
    cache_key = (action.action, action.subject_name)
    try:
        del action_cache[cache_key]
    except KeyError:
        pass

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
            operator_logger.exception("Error running action %s", action.name)

def start_actions_loop():
    while True:
        try:
            action_cache_lock.acquire()
            start_actions()
        except Exception as e:
            operator_logger.exception("Error in start_actions_loop!")
        finally:
            action_cache_lock.release()
            time.sleep(1)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyactions')
def handle_action_event(event, **_):
    wait_for_init()
    action = AnarchyAction(event['object'])
    if event['type'] == 'DELETED':
        cache_remove_action(action)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        cache_action(action)
    else:
        operator_logger.warn('Unknown event for AnarchyAction %s', event)

@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyevents')
def handle_event_event(event, **_):
    wait_for_init()
    anarchy_event = AnarchyEvent(event['object'])
    if event['type'] == 'DELETED':
        AnarchyEvent.unregister(anarchy_event, runtime)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyEvent.register(anarchy_event, runtime)

@kopf.on.event('', 'v1', 'pods', labels={runtime.runner_label: None})
def handle_ansible_runner_event(event, **_):
    wait_for_init()
    pod = event['object']
    pod_meta = pod['metadata']
    pod_name = pod_meta['name']
    if pod_meta.get('labels', {}).get('name', '') != 'anarchy-runner':
        operator_logger.debug('not an anarchy-runner pod')
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
            operator_logger.warn('Lost runner pod %s', runner_name)
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
            operator_logger.warn("Callback for AnarchyAction %s not found", anarchy_action_name)
            flask.abort(404)
            return
        else:
            raise

    anarchy_action = AnarchyAction(anarchy_action_resource)

    if not anarchy_action.check_callback_token(flask.request.headers.get('Authorization')):
        operator_logger.warn("Invalid callback token for AnarchyAction %s", anarchy_action_name)
        flask.abort(403)
        return

    anarchy_action.process_event(runtime, flask.request.json)
    return flask.jsonify({'status': 'ok'})

@api.route(
    '/event/<string:anarchy_action_name>',
    methods=['POST']
)
def action_callback(anarchy_action_name):
    operator_logger.info("Action callback for %s", anarchy_action_name)
    return __event_callback(anarchy_action_name, None)

@api.route(
    '/event/<string:anarchy_action_name>/<string:event_name>',
    methods=['POST']
)
def event_callback(anarchy_action_name, event_name):
    operator_logger.info("AnarchyAction %s received callback event %s", anarchy_action_name, event_name)
    return __event_callback(anarchy_action_name, event_name)

@api.route('/runner/<string:runner_queue_name>/<string:runner_name>', methods=['GET', 'POST'])
def runner(runner_queue_name, runner_name):
    authorization_header = flask.request.headers.get('Authorization', '')
    if authorization_header != 'Bearer ' + runner_token:
        flask.abort(400)
        return

    if flask.request.method == 'POST':
        operator_logger.debug('Received POST with %s', flask.request.json)
        try:
            run = flask.request.json['run']
            result = flask.request.json['result']
            anarchy_event_name = run['metadata']['name']
            anarchy_subject_name = run['spec']['subject']['metadata']['name']
        except KeyError:
            return flask.abort(400, flask.jsonify(
                {'success': False, 'error': 'Invalid run data'}
            ))

        anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
        if not anarchy_subject:
            return flask.abort(400, flask.jsonify(
                {'success': False, 'error': 'AnarchySubject not found'}
            ))

        anarchy_event = anarchy_subject.active_events.get(anarchy_event_name, None)
        if not anarchy_event:
            return flask.abort(400, flask.jsonify(
                {'success': False, 'error': 'AnarchyEvent not found'}
            ))

        anarchy_subject.run_queue_release()
        anarchy_event.post_result(result, runner_name, runtime)
        return flask.jsonify({'success':True})
    else:
        anarchy_subject = AnarchySubject.get_pending(runner_queue_name, runtime)
        if not anarchy_subject:
            return flask.jsonify(None)
        anarchy_event = anarchy_subject.get_event_to_run(runtime)
        anarchy_event.set_runner(runner_name, runtime)
        return flask.jsonify(anarchy_event.to_dict(runtime))

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'start-actions',
        target = start_actions_loop
    ).start()

    prometheus_client.start_http_server(8000)
    http_server = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
else:
    threading.Thread(name='main', target=main).start()
