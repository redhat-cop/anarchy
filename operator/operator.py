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
import subprocess
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
cleanup_interval = int(os.environ.get('CLEANUP_INTERVAL', 300))
runner_check_interval = int(os.environ.get('RUNNER_CHECK_INTERVAL', 60))

operator_logger = logging.getLogger('operator')
operator_logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))
runtime = AnarchyRuntime()
init_complete = False

def init():
    """Initialization function before management loops."""
    global init_complete
    AnarchyGovernor.init(runtime)
    AnarchyRunner.init(runtime)

    init_complete = True
    operator_logger.debug("Completed init")

def init_default_runner():
    """
    Create default AnarchyRunner if it does not exist.
    """
    try:
        runner = runtime.custom_objects_api.get_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyrunners', 'default'
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status == 404:
            operator_logger.info('Creating default AnarchyRunner')
            runner = runtime.custom_objects_api.create_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyrunners',
                AnarchyRunner.default_runner_definition(runtime)
            )
        else:
            raise

def start_runner_process():
    '''
    Start anarchy-runner process for running in all-in-one pod.
    '''
    operator_logger.info('Starting all-in-one runner')
    default_runner = AnarchyRunner.get('default')
    if not default_runner:
        default_runner = AnarchyRunner.register(AnarchyRunner.default_runner_definition(runtime))
    env = os.environ.copy()
    env['ANARCHY_COMPONENT'] = 'runner'
    env['ANARCHY_URL'] = 'http://{}:5000'.format(runtime.anarchy_service_name)
    env['RUNNER_NAME'] = 'default'
    env['RUNNER_TOKEN'] = default_runner.runner_token
    subprocess.Popen(['/opt/app-root/src/.s2i/bin/run'], env=env)

@kopf.on.create(runtime.operator_domain, runtime.api_version, 'anarchysubjects')
def handle_subject_create(body, **_):
    try:
        subject = AnarchySubject(body)
        subject.handle_create(runtime)
    except AssertionError as e:
        operator_logger.warning('AnarchySubject %s invalid: %s', body['metadata']['name'], e)

@kopf.on.update(runtime.operator_domain, runtime.api_version, 'anarchysubjects')
def handle_subject_update(body, old, new, **_):
    try:
        subject = AnarchySubject(body)
        if old['spec'] != new['spec']:
            subject.handle_spec_update(runtime)
    except AssertionError as e:
        operator_logger.warning('AnarchySubject %s invalid: %s', body['metadata']['name'], e)

@kopf.timer(runtime.operator_domain, runtime.api_version, 'anarchysubjects', idle=60, interval=60)
def handle_subject_timer(body, **_):
    subject = AnarchySubject(body)
    subject.set_active_run_to_pending_from_status(runtime)

@kopf.on.event(runtime.operator_domain, runtime.api_version, 'anarchysubjects')
def handle_subject_event(event, logger, **_):
    '''
    Anarchy uses on.event instead of on.delete because Anarchy needs custom
    for removing finalizers. The finalizer will be removed immediately if the
    AnarchyGovernor does not have a delete subject event handler. If there is
    a delete subject event handler then it is up to the governor logic to remove
    the finalizer.
    '''
    obj = event.get('object')
    if obj and obj.get('apiVersion') == runtime.api_group_version:
        if event['type'] in ['ADDED', 'MODIFIED', None]:
            subject = AnarchySubject(obj)
            if subject.is_pending_delete:
                subject.handle_delete(runtime)

@kopf.on.create(runtime.operator_domain, runtime.api_version, 'anarchyactions', labels={runtime.run_label: kopf.ABSENT})
@kopf.on.resume(runtime.operator_domain, runtime.api_version, 'anarchyactions', labels={runtime.run_label: kopf.ABSENT})
@kopf.on.update(runtime.operator_domain, runtime.api_version, 'anarchyactions', labels={runtime.run_label: kopf.ABSENT})
def handle_action_activity(body, logger, **_):
    action = AnarchyAction(body)
    if not action.has_owner:
        action.set_owner(runtime)
    elif not action.has_started:
        if action.after_datetime <= datetime.utcnow():
            action.start(runtime)
        else:
            AnarchyAction.cache_put(action)

@kopf.on.event(runtime.operator_domain, runtime.api_version, 'anarchyactions', labels={runtime.run_label: kopf.ABSENT})
def handle_action_event(event, logger, **_):
    obj = event.get('object')
    if obj and obj.get('apiVersion') == runtime.api_group_version:
        if event['type'] == 'DELETED':
            AnarchyAction.cache_remove(obj['metadata']['name'])

@kopf.on.event(runtime.operator_domain, runtime.api_version, 'anarchyruns', labels={runtime.finished_label: kopf.ABSENT})
def handle_run_event(event, logger, **_):
    obj = event.get('object')
    if obj and obj.get('apiVersion') == runtime.api_group_version:
        if event['type'] in ['ADDED', 'MODIFIED', None]:
            AnarchyRun.register(obj)
        elif event['type'] == 'DELETED':
            AnarchyRun.unregister(obj['metadata']['name'])

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

    run_value = runner_pod.metadata.labels.get(runtime.run_label)
    if run_value:
        operator_logger.warning(
            'AnarchyRunner %s Pod %s requesting run, but appears it should still be running %s',
            anarchy_runner.name, runner_pod.metadata.name, run_value
        )

    if runner_pod.metadata.deletion_timestamp:
        operator_logger.debug(
            'Refusing to give work to AnarchyRunner %s Pod %s, pod is being deleted',
            anarchy_runner.name, runner_pod.metadata.name
        )
        return flask.jsonify(None)

    if runner_pod.metadata.labels.get(runtime.runner_terminating_label):
        operator_logger.info(
            'Deleting AnarchyRunner %s Pod %s, marked for termination',
            anarchy_runner.name, runner_pod.metadata.name
        )
        runtime.core_v1_api.delete_namespaced_pod(runner_pod.metadata.name, runner_pod.metadata.namespace)
        return flask.jsonify(None)

    while True:
        # Loop trying to get a pending anarchy run and claim in for this runner
        # until either nothing is pending or one is claimed.
        anarchy_run = AnarchyRun.get_pending(runtime)
        if not anarchy_run:
            operator_logger.debug(
                'No AnarchyRun pending for AnarchyRunner %s Pod %s',
                anarchy_runner.name, runner_pod.metadata.name
            )
            return flask.jsonify(None)
        if anarchy_run.set_runner(anarchy_runner.name + '.' + runner_pod.metadata.name, runtime):
            # If successfully set_runner on anarchy run, then break from loop
            break

    anarchy_subject = anarchy_run.get_subject(runtime)
    if not anarchy_subject:
        operator_logger.warning(
            'AnarchyRun %s was pending, but cannot find subject %s!',
            anarchy_run.name, anarchy_run.subject_name
        )
        return flask.jsonify(None)

    anarchy_governor = anarchy_subject.get_governor(runtime)
    if not anarchy_governor:
        operator_logger.warning(
            'AnarchySubject %s was pending, but cannot find governor %s!',
            anarchy_subject.name, anarchy_subject.governor_name
        )
        return flask.jsonify(None)

    runtime.core_v1_api.patch_namespaced_pod(
        runner_pod.metadata.name, runner_pod.metadata.namespace,
        { 'metadata': { 'labels': { runtime.run_label: anarchy_run.name, runtime.subject_label: anarchy_subject.name } } }
    )
    resp = anarchy_run.to_dict(runtime)
    resp['subject'] = anarchy_subject.to_dict(runtime)
    resp['governor'] = anarchy_governor.to_dict(runtime)
    return flask.jsonify(resp)

@api.route('/run/<string:run_name>', methods=['POST'])
def post_run(run_name):
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)

    run_value = runner_pod.metadata.labels.get(runtime.run_label)
    runtime.core_v1_api.patch_namespaced_pod(
        runner_pod.metadata.name, runner_pod.metadata.namespace,
        { 'metadata': { 'labels': { runtime.run_label: '', runtime.subject_label: '' } } }
    )

    if run_value != run_name:
        operator_logger.warning(
            'AnarchyRunner %s Pod %s attempted to post run for %s but run label indicates %s',
            anarchy_runner.name, runner_pod.metadata.name, run_name, run_value
        )
        flask.abort(400)

    # When an AnarchyRun is handling a delete completion it is normal for the
    # AnarchyRun and AnarchySubject to be deleted before the post is received.
    anarchy_run = AnarchyRun.get_from_api(run_name, runtime)
    if not anarchy_run:
        operator_logger.info(
            'AnarchyRunner %s pod %s posted result on deleted run %s',
            anarchy_runner.name, runner_pod.metadata.name, run_name
        )
        return flask.jsonify({'success': True, 'msg': 'AnarchyRun not found'})

    anarchy_subject = anarchy_run.get_subject(runtime)
    if not anarchy_subject:
        operator_logger.warning(
            'AnarchyRun %s post to deleted Anarchysubject %s!',
            anarchy_run.name, anarchy_run.subject_name
        )
        return flask.jsonify({'success': True, 'msg': 'AnarchySubject not found'})

    try:
        result = flask.request.json['result']
    except KeyError:
        return flask.abort(400, flask.jsonify(
            {'success': False, 'error': 'Invalid run data'}
        ))

    anarchy_run.post_result(result, runner_pod.metadata.name, runtime)
    if result['status'] == 'successful' \
    and run_name == anarchy_subject.active_run_name:
        anarchy_subject.remove_active_run_from_status(anarchy_run, runtime)
        anarchy_subject.set_active_run_to_pending(runtime)

    return flask.jsonify({'success':True})

@api.route('/run/subject/<string:subject_name>', methods=['PATCH','DELETE'])
def patch_or_delete_subject(subject_name):
    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
    if not anarchy_runner:
        flask.abort(400)
        return

    if subject_name != runner_pod.metadata.labels.get(runtime.subject_label):
        operator_logger.warning(
            'AnarchyRunner %s Pod %s cannot update AnarchySubject %s!',
            anarchy_runner.name, runner_pod.metadata.name, subject_name
        )
        flask.abort(400)
        return

    anarchy_subject = AnarchySubject.get(subject_name, runtime)
    if not anarchy_subject:
        operator_logger.warning(
            'AnarchyRunner %s Pod %s attempted %s on deleted AnarchySubject %s!',
            anarchy_runner.name, runner_pod.metadata.name, flask.request.method, subject_name
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

    if subject_name != runner_pod.metadata.labels.get(runtime.subject_label):
        operator_logger.warning(
            'AnarchyRunner %s Pod %s cannot update AnarchySubject %s!',
            anarchy_runner.name, runner_pod.metadata.name, subject_name
        )
        flask.abort(400)
        return

    anarchy_subject = AnarchySubject.get(subject_name, runtime)
    if not anarchy_subject:
        operator_logger.warning(
            'AnarchyRunner %s Pod %s attempted to create action on deleted AnarchySubject %s!',
            anarchy_runner.name, runner_pod.metadata.name, subject_name
        )
        flask.abort(400)
        return

    anarchy_governor = anarchy_subject.get_governor(runtime)
    if not anarchy_governor:
        operator_logger.warning(
            'AnarchyRunner %s Pod %s cannot post action to AnarchySubject %s, unable to find AnarchyGovernor %s!',
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
        runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions',
        label_selector='{}/subject={}'.format(runtime.operator_domain, anarchy_subject.name)
    ).get('items', []):
        if action_resource['spec']['action'] in cancel_actions \
        and 'status' not in action_resource:
            runtime.custom_objects_api.delete_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions', action_resource['metadata']['name']
            )

    if action_name:
        result = runtime.custom_objects_api.create_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions',
            {
                "apiVersion": runtime.api_group_version,
                "kind": "AnarchyAction",
                "metadata": {
                    "generateName": "%s-%s-" % (anarchy_subject.name, action_name),
                    "labels": {
                        runtime.action_label: action_name,
                        runtime.subject_label: anarchy_subject.name,
                        runtime.governor_label: anarchy_governor.name
                    },
                    "ownerReferences": [{
                        "apiVersion": runtime.api_group_version,
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
                        "apiVersion": runtime.api_group_version,
                        "kind": "AnarchyGovernor",
                        "name": anarchy_governor.name,
                        "namespace":  runtime.operator_namespace,
                        "uid": anarchy_governor.uid
                    },
                    "subjectRef": {
                        "apiVersion": runtime.api_group_version,
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
    pod_name = match.group(2)
    runner_token = match.group(3)

    anarchy_runner = AnarchyRunner.get(runner_name)
    if not anarchy_runner:
        operator_logger.warning('Failed auth for unknown AnarchyRunner %s %s', runner_name, pod_name)
        return None, None

    runner_pod = anarchy_runner.pods.get(pod_name)
    if not runner_pod:
        operator_logger.warning('Failed auth for AnarchyRunner %s %s, unknown pod', runner_name, pod_name)
        return None, None

    pod_runner_token = None
    for env_var in runner_pod.spec.containers[0].env:
        if env_var.name == 'RUNNER_TOKEN':
            pod_runner_token = env_var.value
            break

    if not pod_runner_token:
        operator_logger.warning('Failed auth for AnarchyRunner %s %s, cannot find RUNNER_TOKEN', runner_name, pod_name)
        return None, None

    if pod_runner_token == runner_token:
        return anarchy_runner, runner_pod

    operator_logger.warning('Invalid auth token for AnarchyRunner %s %s', runner_name, runner_pod)
    return None, None


def watch_governors():
    '''
    Watch AnarchyGovernors to keep definition in sync.
    '''
    while True:
        try:
            AnarchyGovernor.watch(runtime)
        except Exception as e:
            operator_logger.exception("Error in AnarchyGovernor watch")
            time.sleep(5)

def watch_runners():
    '''
    Watch AnarchyRunners to keep definition in sync.
    '''
    while True:
        try:
            AnarchyRunner.watch(runtime)
        except Exception as e:
            operator_logger.exception("Error in AnarchyRunner watch")
            time.sleep(5)

def watch_runner_pods():
    '''
    Watch AnarchyRunners to keep definition in sync.
    '''
    while True:
        try:
            AnarchyRunner.watch_pods(runtime)
        except Exception as e:
            operator_logger.exception("Error in AnarchyRunner watch_pods")
            time.sleep(5)

def watch_peering():
    '''
    Watch for KopfPeering and set runtime active flag
    '''
    while True:
        try:
            runtime.watch_peering()
        except Exception as e:
            operator_logger.exception("Error in KopfPeering watch")
            time.sleep(5)

def main_loop():
    last_cleanup = 0
    last_runner_check = 0
    while True:
        with runtime.is_active_condition:
            while not runtime.is_active:
                runtime.is_active_condition.wait()

        if runtime.running_all_in_one:
            start_runner_process()
        elif not AnarchyRunner.get('default'):
            init_default_runner()

        while runtime.is_active:
            AnarchyAction.start_actions(runtime)
            AnarchyRun.manage_active_runs(runtime)

            if cleanup_interval < time.time() - last_cleanup:
                try:
                    AnarchyGovernor.cleanup(runtime)
                    last_cleanup = time.time()
                except:
                    operator_logger.exception('Error in AnarchyGovernor.cleanup!')

            if runner_check_interval < time.time() - last_runner_check:
                try:
                    AnarchyRunner.manage_runners(runtime)
                    last_runner_check = time.time()
                except:
                    operator_logger.exception('Error in AnarchyRunner.managing_runners!')

            time.sleep(1)

def main():
    """Main function."""
    init()

    threading.Thread(
        name = 'watch_governors',
        target = watch_governors
    ).start()

    threading.Thread(
        name = 'watch_runners',
        target = watch_runners
    ).start()

    threading.Thread(
        name = 'watch_runner_pods',
        target = watch_runner_pods
    ).start()

    threading.Thread(
        name = 'watch_peering',
        target = watch_peering
    ).start()

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
