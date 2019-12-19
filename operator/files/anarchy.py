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
import re
import sys
import threading
import time
import uuid
import yaml

from anarchyruntime import AnarchyRuntime
from anarchyrunner import AnarchyRunner
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction
from anarchyrun import AnarchyRun

api = flask.Flask('rest')
runner_check_interval = 60
runner_token = os.environ.get('RUNNER_TOKEN', None)

action_cache_lock = threading.Lock()
action_cache = {}

operator_logger = logging.getLogger('operator')
operator_logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))
runtime = AnarchyRuntime()
init_complete = False

def init():
    """Initialization function before management loops."""
    global init_complete
    init_runners()
    init_governors()
    init_runs()
    init_complete = True
    operator_logger.debug("Completed init")

def init_runners():
    """Get initial list of AnarchyRunners"""
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyrunners'
    ).get('items', []):
        runner = AnarchyRunner.register(resource)
    AnarchyRunner.refresh_all_runner_pods(runtime)

def init_governors():
    """Get initial list of anarchy governors"""
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchygovernors'
    ).get('items', []):
        AnarchyGovernor.register(resource)

def init_runs():
    """
    Load AnarchyRuns that require processing

    The runner label on AnarchyRuns indicates whether it was currently being
    processed by the last instance of the operator on shut-down. We collect
    these jobs and register them with their AnarchySubject.
    """
    anarchy_runs = []
    for resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyruns',
        label_selector = runtime.runner_label
    ).get('items', []):
        anarchy_run = AnarchyRun(resource)
        runner_value = anarchy_run.get_runner_label_value(runtime)

        # If runner value is "successful" then it does not need to be processed.
        # If runner value is "pending" then it will be handled for processing
        # by the kopf watcher on anarchyruns. There should only be one AnarchyRun
        # per subject that is not 'successful' or 'pending'.
        if runner_value and runner_value not in ('successful', 'pending'):
            anarchy_subject = anarchy_run.get_subject(runtime)
            anarchy_subject.current_anarchy_run = anarchy_run.name
            anarchy_subject.anarchy_runs[anarchy_run.name] = anarchy_run
            if '.' in runner_value:
                runner_name, pod_name = runner_value.split('.')
                if runner_name in AnarchyRunner.runners \
                and pod_name in AnarchyRunner.runners[runner_name].runner_pods:
                    AnarchyRunner.runners[runner_name].runner_pods[pod_name] = anarchy_run
                else:
                    anarchy_run.handle_lost_runner(pod_name, runtime)

def wait_for_init():
    while not init_complete:
        operator_logger.info('Waiting for initialization to complete')
        time.sleep(1)


@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyrunners')
def handle_runner_event(event, **_):
    if event['type'] == 'DELETED':
        AnarchyRunner.unregister(event['object']['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        runner = AnarchyRunner.register(event['object'])
        runner.manage_runner_deployment(runtime)
        runner.refresh_runner_pods(runtime)


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


# FIXME - Switch to using kopf.on.delete?
@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyactions')
def handle_action_event(event, **_):
    wait_for_init()
    try:
        action_cache_lock.acquire()
        action = AnarchyAction(event['object'])
        if event['type'] == 'DELETED':
            AnarchyAction.cache_remove(action)
        elif event['type'] in ['ADDED', 'MODIFIED', None]:
            if not action.has_started:
                AnarchyAction.cache_put(action)
        else:
            operator_logger.warning('Unknown event for AnarchyAction %s', event)
    finally:
        action_cache_lock.release()


@kopf.on.event(runtime.operator_domain, 'v1', 'anarchyruns')
def handle_run_event(event, **_):
    wait_for_init()
    anarchy_event = AnarchyRun(event['object'])
    if event['type'] == 'DELETED':
        AnarchyRun.unregister(anarchy_event, runtime)
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        AnarchyRun.register(anarchy_event, runtime)


@api.route('/action/<string:anarchy_action_name>', methods=['POST'])
def action_callback(anarchy_action_name):
    operator_logger.info("Action callback for %s", anarchy_action_name)
    return handle_action_callback(anarchy_action_name, None)
@api.route('/action/<string:anarchy_action_name>/<string:callback_name>', methods=['POST'])
def named_action_callback(anarchy_action_name, callback_name):
    operator_logger.info("AnarchyAction %s received callback %s", anarchy_action_name, callback_name)
    return handle_action_callback(anarchy_action_name, callback_name)

def handle_action_callback(anarchy_action_name, callback_name):
    if not flask.request.json:
        flask.abort(400)
        return
    anarchy_action = AnarchyAction.get(anarchy_action_name, runtime)
    if not anarchy_action:
        operator_logger.warning("AnarchyAction %s not found for callback", anarchy_action_name)
        flask.abort(404)
        return
    if not anarchy_action.check_callback_token(flask.request.headers.get('Authorization', '')):
        operator_logger.warning("Invalid callback token for AnarchyAction %s", anarchy_action_name)
        flask.abort(403)
        return
    anarchy_action.process_callback(runtime, callback_name, flask.request.json)
    return flask.jsonify({'status': 'ok'})


@api.route('/run', methods=['GET'])
def get_run():
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)
        return

    anarchy_run = anarchy_runner.runner_pods.get(runner_pod, None)
    if anarchy_run:
        operator_logger.warning(
            'AnarchyRunner %s pod %s get run, but already had run %s!',
            anarchy_runner.name, runner_pod, anarchy_run.name
        )
        anarchy_run.handle_lost_runner(runner_pod, runtime)

    anarchy_subject = AnarchySubject.get_pending(anarchy_runner.name, runtime)
    if not anarchy_subject:
        return flask.jsonify(None)

    anarchy_run = anarchy_subject.get_anarchy_run(runtime)
    if not anarchy_run:
        return flask.jsonify(None)

    anarchy_runner.runner_pods[runner_pod] = anarchy_run
    anarchy_run.set_runner(anarchy_runner.name + '.' + runner_pod, runtime)
    return flask.jsonify(anarchy_run.to_dict(runtime))

@api.route('/run/<string:run_name>', methods=['POST'])
def post_run(run_name):
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)
        return

    anarchy_run = anarchy_runner.runner_pods.get(runner_pod, None)
    anarchy_runner.runner_pods[runner_pod] = None
    if not anarchy_run or anarchy_run.name != run_name:
        operator_logger.warning(
            'AnarchyRunner %s pod %s posted unexpected run %s!',
            anarchy_runner.name, runner_pod, run_name
        )
        flask.abort(400)
        return

    try:
        result = flask.request.json['result']
    except KeyError:
        return flask.abort(400, flask.jsonify(
            {'success': False, 'error': 'Invalid run data'}
        ))

    anarchy_subject = anarchy_run.get_subject(runtime)
    if not anarchy_subject:
        return flask.abort(404, flask.jsonify(
            {'success': False, 'error': 'AnarchySubject not found'}
        ))

    anarchy_subject.run_queue_release()
    anarchy_run.post_result(result, runner_pod, runtime)
    return flask.jsonify({'success':True})

@api.route('/run/subject/<string:subject_name>', methods=['PATCH','DELETE'])
def patch_or_delete_subject(subject_name):
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)
        return

    anarchy_run = anarchy_runner.runner_pods.get(runner_pod, None)
    if not anarchy_run:
        operator_logger.warning(
            'AnarchyRunner %s pod %s cannot update AnarchySubject %s without AnarchyRun!',
            anarchy_runner.name, runner_pod, subject_name
        )
        flask.abort(400)
        return

    anarchy_subject = anarchy_run.get_subject(runtime)
    if not anarchy_subject or anarchy_subject.name != subject_name:
        operator_logger.warning(
            'AnarchyRunner %s pod %s cannot update AnarchySubject %s!',
            anarchy_runner.name, runner_pod, subject_name
        )
        flask.abort(400)
        return

    if flask.request.method == 'PATCH':
        if not 'patch' in flask.request.json:
            operator_logger.warning('No patch in AnarchySubject %s post', subject_name)
            flask.abort(400)
            return
        result = anarchy_subject.patch(flask.request.json['patch'], runtime)
    elif flask.request.method == 'DELETE':
        result = anarchy_subject.delete(flask.request.json.get('remove_finalizers', False), runtime)

    return flask.jsonify({'success': True, 'result': result})

@api.route('/run/subject/<string:subject_name>/actions', methods=['POST'])
def post_subject_action(subject_name):
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)
        return

    anarchy_run = anarchy_runner.runner_pods.get(runner_pod, None)
    if not anarchy_run:
        operator_logger.warning(
            'AnarchyRunner %s pod %s cannot update AnarchySubject %s without AnarchyRun!',
            anarchy_runner.name, runner_pod, subject_name
        )
        flask.abort(400)
        return

    anarchy_subject = anarchy_run.get_subject(runtime)
    if not anarchy_subject or anarchy_subject.name != subject_name:
        operator_logger.warning(
            'AnarchyRunner %s pod %s cannot update AnarchySubject %s!',
            anarchy_runner.name, runner_pod, subject_name
        )
        flask.abort(400)
        return

    anarchy_governor = anarchy_subject.get_governor(runtime)
    if not anarchy_governor:
        operator_logger.warning(
            'AnarchyRunner %s pod %s cannot update AnarchySubject %s, unable to find AnarchyGovernor %s!',
            anarchy_runner.name, runner_pod, subject_name, anarchy_subject.governor_name
        )
        flask.abort(400)
        return

    action_name = flask.request.json.get('action', None)
    after_timestamp = flask.request.json.get('after', None)
    cancel_actions = flask.request.json.get('cancel', None)
    if not action_name or cancel_actions:
        operator_logger.warning('No action or cancel given for scheduling action')
        flask.abort(400)
        return
    if after_timestamp and not re.match(r'\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ', after_timestamp):
        operator_logger.warning('Invalide datetime format "%s" given for action after value', after_timestamp)
        flask.abort(400)
        return

    if action_name not in cancel_actions:
        cancel_actions.append(action_name)

    for action_resource in runtime.custom_objects_api.list_namespaced_custom_object(
        runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions',
        label_selector='{}/subject={}'.format(runtime.operator_domain, anarchy_subject.name)
    ).get('items', []):
        if action_resource['spec']['action'] in cancel_actions \
        and 'status' not in action_resource:
            runtime.custom_objects_api.delete_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions',
                action_resource['metadata']['name'], kubernetes.client.V1DeleteOptions()
            )

    if action_name:
        result = runtime.custom_objects_api.create_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions',
            {
                "apiVersion": runtime.operator_domain + "/v1",
                "kind": "AnarchyAction",
                "metadata": {
                    "generateName": "%s-%s-" % (anarchy_subject.name, action_name),
                    "labels": {
                        runtime.operator_domain + '/action': action_name,
                        runtime.operator_domain + "/subject": anarchy_subject.name,
                        runtime.operator_domain + "/governor": anarchy_governor.name
                    },
                    "ownerReferences": [{
                        "apiVersion": runtime.operator_domain + "/v1",
                        "controller": True,
                        "kind": "AnarchySubject",
                        "name": anarchy_subject.name,
                        "uid": anarchy_subject.uid
                    }]
                },
                "spec": {
                    "action": action_name,
                    "after": after_timestamp,
                    "callbackToken": uuid.uuid4().hex,
                    "governorRef": {
                        "apiVersion": runtime.operator_domain + "/v1",
                        "kind": "AnarchyGovernor",
                        "name": anarchy_governor.name,
                        "namespace":  runtime.operator_namespace,
                        "uid": anarchy_governor.uid
                    },
                    "subjectRef": {
                        "apiVersion": runtime.operator_domain + "/v1",
                        "kind": "AnarchySubject",
                        "name": anarchy_subject.name,
                        "namespace":  runtime.operator_namespace,
                        "uid": anarchy_subject.uid
                    }
                }
            }
        )
    else:
        result = None

    return flask.jsonify({'success': True, 'result': result})

def check_runner_auth(auth_header):
    match = re.match(r'Bearer ([^:]+):([^:]+):(.*)', auth_header)
    if not match:
        return None, None
    runner_name = match.group(1)
    runner_pod = match.group(2)
    runner_token = match.group(3)

    anarchy_runner = AnarchyRunner.get(runner_name)
    if not anarchy_runner:
        operator_logger.warning('Failed auth for unknown AnarchyRunner %s %s', runner_name, runner_pod)
        return None, None
    elif anarchy_runner.runner_token != runner_token:
        operator_logger.warning('Invalid auth token for AnarchyRunner %s %s', runner_name, runner_pod)
        return None, None

    return anarchy_runner, runner_pod

def main_loop():
    last_runner_check = 0
    while True:
        try:
            action_cache_lock.acquire()
            AnarchyAction.start_actions(runtime)
        except Exception as e:
            operator_logger.exception("Error in start_actions!")
        finally:
            action_cache_lock.release()

        try:
            AnarchySubject.retry_failures(runtime)
        except Exception as e:
            operator_logger.exception("Error in retry_failures!")

        if runner_check_interval < time.time() - last_runner_check:
            try:
                AnarchyRunner.refresh_all_runner_pods(runtime)
                last_runner_check = time.time()
            except:
                operator_logger.exception('Error checking runner pods in main loop')

        time.sleep(1)

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'main',
        target = main_loop
    ).start()

    prometheus_client.start_http_server(8000)
    http_server = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
else:
    threading.Thread(name='main', target=main).start()
