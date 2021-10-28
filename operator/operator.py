#!/usr/bin/env python

from datetime import datetime, timedelta, timezone

import flask
import gevent.pywsgi
import json
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
import urllib3
import yaml

from anarchyruntime import AnarchyRuntime
from anarchyrunner import AnarchyRunner
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction
from anarchyrun import AnarchyRun

api = flask.Flask('rest')

action_check_interval = int(os.environ.get('ACTION_CHECK_INTERVAL', 5))
cleanup_interval = int(os.environ.get('CLEANUP_INTERVAL', 300))
run_check_interval = int(os.environ.get('RUN_CHECK_INTERVAL', 5))
runner_check_interval = int(os.environ.get('RUNNER_CHECK_INTERVAL', 20))

operator_logger = logging.getLogger('operator')
operator_logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))
anarchy_runtime = AnarchyRuntime()

@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    # Disable scanning for crds and namespaces
    settings.scanning.disabled = True
    settings.persistence.finalizer = anarchy_runtime.operator_domain

    http_server = gevent.pywsgi.WSGIServer(('', 5000), api)
    threading.Thread(
        name = 'api',
        target = run_api,
        daemon = True,
    ).start()

    if anarchy_runtime.running_all_in_one:
        AnarchyRunner.start_all_in_one_runner(anarchy_runtime)
    else:
        AnarchyRunner.preload(anarchy_runtime)
        if not AnarchyRunner.get('default'):
            AnarchyRunner.create_default(anarchy_runtime)
        threading.Thread(
            name = 'watch_runners',
            target = AnarchyRunner.watch,
            args = [anarchy_runtime],
            daemon = True,
        ).start()
        threading.Thread(
            name = 'watch_runner_pods',
            target = AnarchyRunner.watch_pods,
            args = [anarchy_runtime],
            daemon = True,
        ).start()

    AnarchyGovernor.preload(anarchy_runtime)
    threading.Thread(
        name = 'watch_governors',
        target = AnarchyGovernor.watch,
        args = [anarchy_runtime],
        daemon = True,
    ).start()

if not anarchy_runtime.running_all_in_one:
    @kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners')
    @kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners')
    @kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners')
    def runner_event(logger, **kwargs):
        runner = AnarchyRunner.register(anarchy_runtime=anarchy_runtime, logger=logger, **kwargs)
        runner.manage(anarchy_runtime)

    @kopf.on.event('pods', labels={anarchy_runtime.runner_label: kopf.PRESENT})
    def runner_pod(event, logger, **kwargs):
        if 'object' not in event:
            return

        pod = anarchy_runtime.dict_to_k8s_object(event['object'], kubernetes.client.V1Pod)
        runner_name = pod.metadata.labels[anarchy_runtime.runner_label]
        runner = AnarchyRunner.get(runner_name)
        if runner:
            if event['type'] == 'DELETED':
                runner.unregister_pod(pod.metadata.name, anarchy_runtime)
            else:
                runner.register_pod(pod, anarchy_runtime)
            runner.manage(anarchy_runtime)
        else:
            logger.warning(
                'Did not find AnarchyRunner for runner Pod',
                extra = dict(
                    runner = dict(
                        api_version = anarchy_runtime.api_group_version,
                        kind = 'AnarchyRunner',
                        name = runner_name,
                        namespace = anarchy_runtime.operator_namespace,
                    )
                )
            )

#action_delete_timers = {}
#subject_action_start_timers = {}

#def init():
#    """Initialization function before management loops."""
#    global init_complete
#    AnarchyGovernor.init(anarchy_runtime)
#    AnarchyRunner.init(anarchy_runtime)
#
#    init_complete = True
#    operator_logger.debug("Completed init")
#
#def init_default_runner():
#    """
#    Create default AnarchyRunner if it does not exist.
#    """
#    try:
#        runner = anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
#            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyrunners', 'default'
#        )
#    except kubernetes.client.rest.ApiException as e:
#        if e.status == 404:
#            operator_logger.info('Creating default AnarchyRunner')
#            runner = anarchy_runtime.custom_objects_api.create_namespaced_custom_object(
#                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyrunners',
#                AnarchyRunner.default_runner_definition(anarchy_runtime)
#            )
#        else:
#            raise
#
#@kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
#def handle_subject_create(logger, **kwargs):
#    try:
#        subject = AnarchySubject.register(logger=logger, anarchy_runtime=anarchy_runtime, **kwargs)
#        subject.handle_create(anarchy_runtime)
#    except AssertionError as e:
#        logger.warning(
#            'AnarchySubject is invalid',
#            extra = dict(
#                reason = str(e)
#            )
#        )
#
#@kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
#def handle_subject_resume(logger, **kwargs):
#    try:
#        subject = AnarchySubject.register(logger=logger, anarchy_runtime=anarchy_runtime, **kwargs)
#        subject.handle_resume(anarchy_runtime)
#    except AssertionError as e:
#        logger.warning(
#            'AnarchySubject is invalid',
#            extra = dict(
#                reason = str(e)
#            )
#        )
#
#@kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
#def handle_subject_update(logger, old, new, **kwargs):
#    try:
#        subject = AnarchySubject.register(logger=logger, anarchy_runtime=anarchy_runtime, **kwargs)
#        if old['spec'] != new['spec']:
#            subject.handle_spec_update(old, anarchy_runtime)
#    except AssertionError as e:
#        logger.warning(
#            'AnarchySubject is invalid',
#            extra = dict(
#                reason = str(e)
#            )
#        )
#
#@kopf.on.delete(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
#def handle_subject_delete(logger, **kwargs):
#    subject = AnarchySubject.register(logger=logger, anarchy_runtime=anarchy_runtime, **kwargs)
#    subject.handle_delete(anarchy_runtime)
#
#@kopf.on.event(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
#def handle_subject_event(event, logger, **_):
#    '''
#    Monitor for update events after delete has begun.
#    The Kopf framework stops tracking updates after the delete handler has succeeded.
#    '''
#    obj = event.get('object')
#    if obj and obj.get('apiVersion') == anarchy_runtime.api_group_version:
#        if event['type'] == 'DELETED':
#            AnarchySubject.unregister(
#                logger = logger,
#                name = obj,
#            )
#        elif event['type'] in ['ADDED', 'MODIFIED', None]:
#            subject = AnarchySubject.register(
#                logger = logger,
#                resource = obj,
#                anarchy_runtime = anarchy_runtime,
#            )
#            if subject.delete_started \
#            and anarchy_runtime.subject_label in self.metadata.get('finalizers', []):
#                # Manually interact with the kopf last-handled-configuration
#                # annotation that is usually managed by kopf.
#                last_handled_configuration = self.last_handled_configuration
#                if last_handled_configuration and self.spec != last_handled_configuration['spec']:
#                    self.handle_spec_update(last_handled_configuration, anarchy_runtime)
#                    # Update kopf last-handled-configuration annotation
#                    resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
#                        anarchy_runtime.operator_domain, anarchy_runtime.api_version, self.namespace, 'anarchysubjects', self.name,
#                        {
#                            'metadata': {
#                                'annotations': {
#                                    'kopf.zalando.org/last-handled-configuration': json.dumps({
#                                        'metadata': {
#                                            'annotations': {
#                                                k: v for k, v in self.metadata.get('annotations', {}).items()
#                                                if k != 'kopf.zalando.org/last-handled-configuration'
#                                            },
#                                            'labels': self.metadata.get('labels', {})
#                                        },
#                                        'spec': self.spec,
#                                    }),
#                                }
#                            }
#                        }
#                    )
#
#@kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
#@kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
#@kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
#def handle_action(body, logger, **_):
#    action = AnarchyAction(body, logger)
#
#    # Remove timer if set. It will be reset if appropriate.
#    action_delete_timers.pop(action.name, None)
#
#    governor = action.get_governor(anarchy_runtime)
#    if not governor:
#        raise kopf.TemporaryError(
#            'Cannot find AnarchyGovernor',
#            delay = 30,
#            extra = dict(
#                subject = action.governor_ref(anarchy_runtime)
#            )
#        )
#
#    subject = action.get_subject(anarchy_runtime)
#    if not subject:
#        raise kopf.TemporaryError(
#            'Cannot find AnarchySubject',
#            delay = 30,
#            extra = dict(
#                subject = action.subject_ref(anarchy_runtime)
#            )
#        )
#
#    # Initialize actions if not fully initalized with owner
#    if not action.has_owner:
#        action.set_owner(subject, anarchy_runtime)
#
#    if action.is_finished:
#        if subject.active_action_name == action.name:
#            next_action = subject.resolve_active_action(action)
#            if next_action:
#                next_action = AnarchyAction(next_action)
#                next_action.start()
#
#        finished_datetime = datetime.strptime(finished_timestamp, '%Y-%m-%dT%H:%M:%SZ')
#        remove_after_datetime = finished_datetime + governor.remove_finished_actions_after
#        remove_after_seconds = (remove_after_datetime - datetime.utcnow()).total_seconds()
#
#
#
#        if remove_after_seconds <= 0:
#            action.delete(anarchy_runtime)
#        else:
#            timer = threading.Timer(remove_after_seconds, action.delete, args=[anarchy_runtime])
#            timer.daemon = True
#            timer.start()
#            action_delete_timers[action.name] = timer
#    elif not action.has_run_scheduled:
#        start_after_seconds = (action.after_datetime - datetime.utcnow()).total_seconds()
#        subject = action.get_subject(anarchy_runtime)
#        if subject.active_action_name == action.name:
#            if start_after_seconds <= 0
#                action.start(anarchy_runtime)
#            else:
#                timer = threading.Timer(start_after_seconds, action.start, args=[anarchy_runtime])
#                timer.daemon = True
#                timer.start()
#                subject_action_start_timers[subject.name] = timer
#        elif subject.active_action_name:
#
#            # Already have an active action, add this action to pending
#            ...
#        elif start_after_seconds <= 0:
#            # Set this action as the active action for the subject and start
#            ...
#        else:
#            # Add this action to the subject's pending actions and set timer
#
#
#
#
#
#
#
#
#
#    subject = action.get_subject(anarchy_runtime)
#
#
#
#
#
#
#    elif not action.is_started:
#        start_after_seconds = (datetime.utcnow() - action.after_datetime).total_seconds()
#        subject = action.get_subject(anarchy_runtime)
#        if subject.active_action_name:
#            if action.name == subject.active_action_name:
#                # Action scheduled for continued execution
#                if start_after_seconds < 0:
#                    action.start()
#                else:
#                    action.
#            else:
#
#        else:
#
#
#
#
#            pass
#
#
#
#
#        # 
#        if start_after_seconds < 0:
#            action.start(anarchy_runtime)
#        else:
#
#
#
#
#
#
#@kopf.on.delete(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
#def handle_action_delete(body, logger, **_):
#    action = AnarchyAction(body)
#
#
#
#@api.route('/action/<string:anarchy_action_name>', methods=['POST'])
#def action_callback(anarchy_action_name):
#    operator_logger.info("Action callback for %s", anarchy_action_name)
#    return handle_action_callback(anarchy_action_name, None)
#@api.route('/action/<string:anarchy_action_name>/<string:callback_name>', methods=['POST'])
#def named_action_callback(anarchy_action_name, callback_name):
#    operator_logger.info("AnarchyAction %s received callback %s", anarchy_action_name, callback_name)
#    return handle_action_callback(anarchy_action_name, callback_name)
#
#def handle_action_callback(anarchy_action_name, callback_name):
#    if not flask.request.json:
#        flask.abort(400)
#    anarchy_action = AnarchyAction.get(anarchy_action_name, anarchy_runtime)
#    if not anarchy_action:
#        operator_logger.warning("AnarchyAction %s not found for callback", anarchy_action_name)
#        flask.abort(404)
#    if not anarchy_action.check_callback_token(flask.request.headers.get('Authorization', '')):
#        operator_logger.warning("Invalid callback token for AnarchyAction %s", anarchy_action_name)
#        flask.abort(403)
#    if anarchy_action.finished_timestamp:
#        operator_logger.warning("Invalid callback to finished AnarchyAction %s", anarchy_action_name)
#        flask.abort(400)
#    anarchy_action.process_callback(anarchy_runtime, callback_name, flask.request.json)
#    return flask.jsonify({'status': 'ok'})

@api.route('/run', methods=['GET'])
def get_run():
    anarchy_runner, runner_pod, anarchy_run_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not anarchy_runner:
        flask.abort(400)

    logger = anarchy_runner.local_logger

    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )

    if anarchy_run_ref:
        logger.warning(
            'AnarchyRunner requesting new run when an AnarchyRun should still be running',
            extra = dict(pod=runner_pod_ref, run=anarchy_run_ref)
        )

    if runner_pod.metadata.deletion_timestamp:
        logger.debug(
            'Refusing to give work to AnarchyRunner Pod that is being deleted',
            extra = dict(pod=runner_pod_ref)
        )
        return flask.jsonify(None)

    if runner_pod.metadata.labels.get(anarchy_runtime.runner_terminating_label):
        logger.info(
            'Deleting AnarchyRunner Pod marked for termination',
            extra = dict(pod=runner_pod_ref)
        )
        anarchy_runtime.core_v1_api.delete_namespaced_pod(runner_pod.metadata.name, runner_pod.metadata.namespace)
        return flask.jsonify(None)

    while True:
        # Loop trying to get a pending anarchy run and claim in for this runner
        # until either nothing is pending or one is claimed.
        anarchy_run = AnarchyRun.get_pending(anarchy_runtime)
        if not anarchy_run:
            logger.debug(
                'No AnarchyRun pending for AnarchyRunner',
                extra = dict(pod=runner_pod_ref)
            )
            return flask.jsonify(None)
        if anarchy_run.set_runner(runner_pod.metadata.name, anarchy_runtime):
            # If successfully set_runner on anarchy run, then break from loop
            break

    anarchy_subject = anarchy_run.get_subject(anarchy_runtime)
    if not anarchy_subject:
        logger.warning(
            'AnarchyRun was pending, but cannot find AnarchySubject!',
            extra = dict(
                pod = runner_pod_ref,
                run = anarchy_run.reference,
                subject = anarchy_run.subject_reference,
            )
        )
        return flask.jsonify(None)

    anarchy_governor = anarchy_subject.get_governor(anarchy_runtime)
    if not anarchy_governor:
        logger.warning(
            'AnarchyRun was pending, but cannot find AnarchyGovernor!',
            extra = dict(
                governor = subject.governor_reference,
                pod = runner_pod_ref,
                run = anarchy_run.reference,
                subject = subject.reference,
            )
        )
        return flask.jsonify(None)

    anarchy_runner.set_pod_run_reference(
        anarchy_run = anarchy_run,
        anarchy_runtime = anarchy_runtime,
        runner_pod = runner_pod,
    )
    resp = anarchy_run.to_dict(anarchy_runtime)
    resp['subject'] = anarchy_subject.to_dict(anarchy_runtime)
    resp['governor'] = anarchy_governor.to_dict()
    return flask.jsonify(resp)

#@api.route('/run/<string:run_name>', methods=['POST'])
#def post_run(run_name):
#    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
#    if not anarchy_runner:
#        flask.abort(400)
#
#    if not anarchy_runtime.running_all_in_one:
#        run_value = runner_pod.metadata.labels.get(anarchy_runtime.run_label)
#        anarchy_runtime.core_v1_api.patch_namespaced_pod(
#            runner_pod.metadata.name, runner_pod.metadata.namespace,
#            { 'metadata': { 'labels': { anarchy_runtime.run_label: '', anarchy_runtime.subject_label: '' } } }
#        )
#
#        if run_value != run_name:
#            operator_logger.warning(
#                'AnarchyRunner %s Pod %s attempted to post run for %s but run label indicates %s',
#                anarchy_runner.name, runner_pod.metadata.name, run_name, run_value
#            )
#            flask.abort(400)
#
#    # When an AnarchyRun is handling a delete completion it is normal for the
#    # AnarchyRun and AnarchySubject to be deleted before the post is received.
#    anarchy_run = AnarchyRun.get_from_api(run_name, anarchy_runtime)
#    if not anarchy_run:
#        operator_logger.info(
#            'AnarchyRunner %s pod %s posted result on deleted run %s',
#            anarchy_runner.name, runner_pod.metadata.name, run_name
#        )
#        return flask.jsonify({'success': True, 'msg': 'AnarchyRun not found'})
#
#    anarchy_subject = anarchy_run.get_subject(anarchy_runtime)
#    if not anarchy_subject:
#        operator_logger.warning(
#            'AnarchyRun %s post to deleted AnarchySubject %s!',
#            anarchy_run.name, anarchy_run.subject_name
#        )
#        return flask.jsonify({'success': True, 'msg': 'AnarchySubject not found'})
#
#    try:
#        result = flask.request.json['result']
#    except KeyError:
#        flask.abort(400, flask.jsonify(
#            {'success': False, 'error': 'Invalid run data'}
#        ))
#
#    anarchy_run.post_result(result, runner_pod.metadata.name, anarchy_runtime)
#    if run_name == anarchy_subject.active_run_name:
#        if result['status'] == 'successful':
#            anarchy_subject.remove_active_run_from_status(anarchy_run, anarchy_runtime)
#            anarchy_subject.set_active_run_to_pending(anarchy_runtime)
#            if anarchy_run.action_name:
#                anarchy_action = AnarchyAction.get(anarchy_run.action_name, anarchy_runtime)
#                anarchy_governor = anarchy_subject.get_governor(anarchy_runtime)
#                action_config = anarchy_governor.action_config(anarchy_action.action)
#                if result.get('continue'):
#                    anarchy_action.schedule_continuation(result['continue'], anarchy_runtime)
#                elif action_config.finish_on_successful_run:
#                    if anarchy_action.name == anarchy_subject.active_action_name:
#                        anarchy_subject.remove_active_action(anarchy_action, anarchy_runtime)
#                    anarchy_action.set_finished('successful', anarchy_runtime)
#        else:
#            anarchy_subject.set_run_failure_in_status(anarchy_run, anarchy_runtime)
#    else:
#        operator_logger.warning(
#            'AnarchyRun %s post to AnarchySubject %s, but was not the active run!',
#            anarchy_run.name, anarchy_run.subject_name
#        )
#
#    return flask.jsonify({'success':True})
#
#@api.route('/run/subject/<string:subject_name>', methods=['PATCH','DELETE'])
#def patch_or_delete_subject(subject_name):
#    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
#    if not anarchy_runner:
#        flask.abort(400)
#
#    if not anarchy_runtime.running_all_in_one \
#    and subject_name != runner_pod.metadata.labels.get(anarchy_runtime.subject_label):
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s cannot update AnarchySubject %s!',
#            anarchy_runner.name, runner_pod.metadata.name, subject_name
#        )
#        flask.abort(400)
#
#    anarchy_subject = AnarchySubject.get(subject_name, anarchy_runtime)
#    if not anarchy_subject:
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s attempted %s on deleted AnarchySubject %s!',
#            anarchy_runner.name, runner_pod.metadata.name, flask.request.method, subject_name
#        )
#        flask.abort(400)
#
#    if flask.request.method == 'PATCH':
#        if not 'patch' in flask.request.json:
#            operator_logger.warning('No patch in AnarchySubject %s post', subject_name)
#            flask.abort(400)
#        result = anarchy_subject.patch(flask.request.json['patch'], anarchy_runtime)
#    elif flask.request.method == 'DELETE':
#        result = anarchy_subject.delete(flask.request.json.get('remove_finalizers', False), anarchy_runtime)
#
#    return flask.jsonify({'success': True, 'result': result})
#
#@api.route('/run/subject/<string:subject_name>/actions', methods=['POST'])
#def run_subject_action_post(subject_name):
#    subject_ref = dict(
#        apiVersion = anarchy_runtime.api_group_version,
#        kind = 'AnarchySubject',
#        name = subject_name,
#        namespace = anarchy_runtime.operator_namespace,
#    )
#
#    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
#    if not anarchy_runner:
#        flask.abort(400)
#
#    pod_ref = dict(
#        apiVersion = 'v1',
#        kind = 'Pod',
#        name = runner_pod.metadata.name,
#        namespace = runner_pod.metadata.namespace,
#    )
#
#    if not anarchy_runtime.running_all_in_one \
#    and subject_name != runner_pod.metadata.labels.get(anarchy_runtime.subject_label):
#        anarchy_runner.logger.warning(
#            'AnarchyRunner pod denied access to update AnarchySubject!',
#            extra = dict(
#                pod = pod_ref,
#                subject = subject_ref,
#            )
#        )
#        flask.abort(400)
#
#    anarchy_subject = AnarchySubject.get(subject_name, anarchy_runtime)
#    if not anarchy_subject:
#        anarchy_runner.logger.warning(
#            'AnarchyRunner pod attempted to create action on deleted AnarchySubject!',
#            extra = dict(
#                pod = pod_ref,
#                subject = subject_ref,
#            )
#        )
#        flask.abort(400)
#
#    anarchy_governor = anarchy_subject.get_governor(anarchy_runtime)
#    if not anarchy_governor:
#        anarchy_runner.logger.warning(
#            'AnarchyRunner pod cannot post action to AnarchySubject, unable to find AnarchyGovernor!',
#            extra = dict(
#                governor = dict(
#                    apiVersion = anarchy_runtime.api_group_version,
#                    kind = 'AnarchyGovernor',
#                    name = anarchy_subject.governor_name,
#                    namespace = anasrchy_subject.namespace_name,
#                ),
#                pod = pod_ref,
#                subject = subject_ref,
#            )
#        )
#        flask.abort(400)
#
#    action_name = flask.request.json.get('action', None)
#    after_timestamp = flask.request.json.get('after', None)
#    cancel_actions = flask.request.json.get('cancel', None)
#
#    if not action_name and not cancel_actions:
#        anarchy_runner.warning(
#            'No action or cancel given for scheduling action',
#            extra = dict(
#                pod = pod_ref,
#                subject = subject_ref,
#            )
#        )
#        flask.abort(400)
#
#    if after_timestamp and not re.match(r'\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ', after_timestamp):
#        anarchy_runner.logger.warning(
#            'Invalid datetime format given for action after value',
#            extra = dict(
#                after = after_timestamp,
#                pod = pod_ref,
#                subject = subject_ref,
#            )
#        )
#        flask.abort(400)
#
#    if action_name not in cancel_actions:
#        cancel_actions.append(action_name)
#
#    for action_resource in anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
#        anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions',
#        label_selector='{}/subject={}'.format(anarchy_runtime.operator_domain, anarchy_subject.name)
#    ).get('items', []):
#        if action_resource['spec']['action'] in cancel_actions \
#        and 'status' not in action_resource:
#            anarchy_runtime.custom_objects_api.delete_namespaced_custom_object(
#                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', action_resource['metadata']['name']
#            )
#
#    if action_name:
#        result = anarchy_runtime.custom_objects_api.create_namespaced_custom_object(
#            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions',
#            {
#                "apiVersion": anarchy_runtime.api_group_version,
#                "kind": "AnarchyAction",
#                "metadata": {
#                    "generateName": "%s-%s-" % (anarchy_subject.name, action_name),
#                    "labels": {
#                        anarchy_runtime.action_label: action_name,
#                        anarchy_runtime.subject_label: anarchy_subject.name,
#                        anarchy_runtime.governor_label: anarchy_governor.name
#                    },
#                    "ownerReferences": [{
#                        "apiVersion": anarchy_runtime.api_group_version,
#                        "controller": True,
#                        "kind": "AnarchySubject",
#                        "name": anarchy_subject.name,
#                        "uid": anarchy_subject.uid
#                    }]
#                },
#                "spec": {
#                    "action": action_name,
#                    "after": after_timestamp,
#                    "callbackToken": uuid.uuid4().hex,
#                    "governorRef": anarchy_governor.ref,
#                    "subjectRef": {
#                        "apiVersion": anarchy_runtime.api_group_version,
#                        "kind": "AnarchySubject",
#                        "name": anarchy_subject.name,
#                        "namespace":  anarchy_runtime.operator_namespace,
#                        "uid": anarchy_subject.uid
#                    }
#                }
#            }
#        )
#    else:
#        result = None
#
#    return flask.jsonify({'success': True, 'result': result})
#
#@api.route('/run/subject/<string:subject_name>/actions/<string:action_name>', methods=['PATCH'])
#def run_subject_action_patch(subject_name, action_name):
#    """
#    Callback from runner to update AnarchyAction associated with AnarchySubject assigned to runner.
#
#    The only function of this method currently is to pass JSON, `{"successful": true}` or
#    `{"failed": true}` to mark the action as finished.
#    """
#    anarchy_runner, runner_pod = check_runner_auth(flask.request.headers.get('Authorization', ''))
#    if not anarchy_runner:
#        flask.abort(400)
#
#    if not anarchy_runtime.running_all_in_one \
#    and subject_name != runner_pod.metadata.labels.get(anarchy_runtime.subject_label):
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s cannot update actions for AnarchySubject %s!',
#            anarchy_runner.name, runner_pod.metadata.name, subject_name
#        )
#        flask.abort(400)
#
#    anarchy_action = AnarchyAction.get(action_name, anarchy_runtime)
#    if not anarchy_action:
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s attempted to update action on deleted AnarchyAction %s!',
#            anarchy_runner.name, runner_pod.metadata.name, action_name
#        )
#        flask.abort(400)
#
#    anarchy_subject = AnarchySubject.get(subject_name, anarchy_runtime)
#    if not anarchy_subject:
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s attempted to update action on deleted AnarchySubject %s!',
#            anarchy_runner.name, runner_pod.metadata.name, subject_name
#        )
#        flask.abort(400)
#
#    anarchy_governor = anarchy_subject.get_governor(anarchy_runtime)
#    if not anarchy_governor:
#        operator_logger.warning(
#            'AnarchyRunner %s Pod %s cannot post action to AnarchySubject %s, unable to find AnarchyGovernor %s!',
#            anarchy_runner.name, runner_pod, subject_name, anarchy_subject.governor_name
#        )
#        flask.abort(400)
#
#    finished_state = None
#    if flask.request.json.get('successful', False):
#        finished_state = 'successful'
#    elif flask.request.json.get('failed', False):
#        finished_state = 'failed'
#
#    if finished_state != None:
#        anarchy_subject.remove_active_action(anarchy_action, anarchy_runtime)
#        anarchy_action.set_finished(finished_state, anarchy_runtime)
#
#    return flask.jsonify({'success': True, 'result': anarchy_action.to_dict(anarchy_runtime)})
#
#
#def check_runner_auth(auth_header):
#    """
#    Verify bearer token sent by anarchy runner in API call.
#    """
#    match = re.match(r'Bearer ([^:]+):([^:]+):(.*)', auth_header)
#    if not match:
#        return None, None
#    runner_name = match.group(1)
#    pod_name = match.group(2)
#    runner_token = match.group(3)
#
#    anarchy_runner = AnarchyRunner.get(runner_name)
#    if not anarchy_runner:
#        operator_logger.warning('Failed auth for unknown AnarchyRunner %s %s', runner_name, pod_name)
#        return None, None
#
#    runner_pod = anarchy_runner.pods.get(pod_name)
#    if not runner_pod:
#        operator_logger.warning('Failed auth for AnarchyRunner %s %s, unknown pod', runner_name, pod_name)
#        return None, None
#
#    pod_runner_token = None
#    if anarchy_runtime.running_all_in_one:
#        pod_runner_token = anarchy_runner.runner_token
#    else:
#        for env_var in runner_pod.spec.containers[0].env:
#            if env_var.name == 'RUNNER_TOKEN':
#                pod_runner_token = env_var.value
#                break
#
#        if not pod_runner_token:
#            operator_logger.warning('Failed auth for AnarchyRunner %s %s, cannot find RUNNER_TOKEN', runner_name, pod_name)
#            return None, None
#
#    if pod_runner_token == runner_token:
#        return anarchy_runner, runner_pod
#
#    operator_logger.warning('Invalid auth token for AnarchyRunner %s %s', runner_name, runner_pod)
#    return None, None

def run_api():
    http_server = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

#def watch_runners():
#    '''
#    Watch AnarchyRunners to keep definition in sync.
#    '''
#    while True:
#        try:
#            AnarchyRunner.watch(anarchy_runtime)
#        except kubernetes.client.rest.ApiException as e:
#            if e.status == 410:
#                # Ignore 410 Gone, simply reset watch
#                pass
#            else:
#                operator_logger.exception("ApiException in AnarchyRunner watch")
#                time.sleep(5)
#                ProtocolError
#        except urllib3.exceptions.ProtocolError as e:
#            operator_logger.warning(f"ProtocolError in AnarchyRunner watch: {e}")
#        except Exception as e:
#            operator_logger.exception("Exception in AnarchyRunner watch")
#            time.sleep(5)
#
#def watch_peering():
#    '''
#    Watch for KopfPeering and set anarchy_runtime active flag
#    '''
#    while True:
#        try:
#            anarchy_runtime.watch_peering()
#        except kubernetes.client.rest.ApiException as e:
#            if e.status == 410:
#                # Ignore 410 Gone, simply reset watch
#                pass
#            else:
#                operator_logger.exception("ApiException in KopfPeering watch")
#                time.sleep(5)
#        except urllib3.exceptions.ProtocolError as e:
#            operator_logger.warning(f"ProtocolError in KopfPeering watch: {e}")
#        except Exception as e:
#            operator_logger.exception("Exception in KopfPeering watch")
#            time.sleep(5)
#
#def cleanup_loop():
#    last_cleanup = 0
#    last_run_check = 0
#    last_runner_check = 0
#    while True:
#        with anarchy_runtime.is_active_condition:
#            while not anarchy_runtime.is_active:
#                anarchy_runtime.is_active_condition.wait()
#
#        while anarchy_runtime.is_active:
#            if runner_check_interval < time.time() - last_runner_check:
#                try:
#                    AnarchyRunner.manage_runners(anarchy_runtime)
#                    last_runner_check = time.time()
#                except:
#                    operator_logger.exception('Error in AnarchyRunner.manage_runners!')
#
#            if cleanup_interval < time.time() - last_cleanup:
#                try:
#                    AnarchyGovernor.cleanup(anarchy_runtime)
#                    last_cleanup = time.time()
#                except:
#                    operator_logger.exception('Error in AnarchyGovernor.cleanup!')
#
#            if run_check_interval < time.time() - last_run_check:
#                try:
#                    AnarchyRun.manage_active_runs(anarchy_runtime)
#                    last_run_check = time.time()
#                except:
#                    operator_logger.exception('Error in AnarchyRun.manage_active_runs!')
#
#            time.sleep(5)
#
#def main_loop():
#    while True:
#        with anarchy_runtime.is_active_condition:
#            while not anarchy_runtime.is_active:
#                anarchy_runtime.is_active_condition.wait()
#
#        if anarchy_runtime.running_all_in_one:
#            start_runner_process()
#        elif not AnarchyRunner.get('default'):
#            init_default_runner()
#
#def main():
#    """Main function."""
#    init()
#
#    threading.Thread(
#        daemon = True,
#        name = 'api',
#        target = run_api
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'watch_governors',
#        target = watch_governors
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'watch_runners',
#        target = watch_runners
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'watch_runner_pods',
#        target = watch_runner_pods
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'watch_peering',
#        target = watch_peering
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'cleanup',
#        target = cleanup_loop
#    ).start()
#
#    threading.Thread(
#        daemon = True,
#        name = 'main_loop',
#        target = main_loop
#    ).start()
#
#    prometheus_client.start_http_server(8000)
#
#if __name__ == '__main__':
#    main()
#else:
#    # Main function call when running in Kopf
#    threading.Thread(
#        daemon = True,
#        name = 'main',
#        target = main
#    ).start()
