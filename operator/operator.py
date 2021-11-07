#!/usr/bin/env python

import asyncio
import flask
import gevent.pywsgi
import json
import kopf
import kubernetes
import logging
import math
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
from datetime import datetime

api = flask.Flask('rest')

action_check_interval = int(os.environ.get('ACTION_CHECK_INTERVAL', 3))
cleanup_interval = int(os.environ.get('CLEANUP_INTERVAL', 60))
run_check_interval = int(os.environ.get('RUN_CHECK_INTERVAL', 3))
runner_check_interval = int(os.environ.get('RUNNER_CHECK_INTERVAL', 20))

operator_logger = logging.getLogger('operator')
operator_logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))
anarchy_runtime = AnarchyRuntime()

class InfiniteRelativeBackoff:
    def __init__(self, n=2, maximum=60):
        self.n = n
        self.maximum = maximum

    def __iter__(self):
        prev_t = []
        max_age = self.maximum * math.ceil(math.log(self.maximum) / math.log(self.n))
        while True:
            t = time.monotonic()
            prev_t = [p for p in prev_t if t - p < max_age]
            delay = self.n ** len(prev_t)
            prev_t.append(t)
            yield delay if delay < self.maximum else self.maximum

@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    # Never give up from network errors
    settings.networking.error_backoffs = InfiniteRelativeBackoff()

    # Use operator domain as finalizer
    settings.persistence.finalizer = anarchy_runtime.operator_domain

    # Store progress in status. Some objects may be too large to store status in metadata annotations
    settings.persistence.progress_storage = kopf.StatusProgressStorage(field='status.kopf.progress')

    # Only create events for warnings and errors
    settings.posting.level = logging.WARNING

    # Disable scanning for crds and namespaces
    settings.scanning.disabled = True

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

    AnarchySubject.preload(anarchy_runtime)
    threading.Thread(
        name = 'watch_subjects',
        target = AnarchySubject.watch,
        args = [anarchy_runtime],
        daemon = True
    )

    threading.Thread(
        name = 'api',
        target = run_api,
        daemon = True,
    ).start()


if not anarchy_runtime.running_all_in_one:
    @kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners', id='anarchyrunner_create')
    @kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners', id='anarchyrunner_resume')
    @kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyrunners', id='anarchyrunner_update')
    def runner_event(**kwargs):
        runner = AnarchyRunner.register(anarchy_runtime=anarchy_runtime, **kwargs)
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


@kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
def subject_create(**kwargs):
    subject = AnarchySubject.register(anarchy_runtime=anarchy_runtime, **kwargs)
    subject.initialize_metadata(anarchy_runtime)
    subject.process_subject_event_handlers(anarchy_runtime, 'create')


@kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
def subject_resume(**kwargs):
    subject = AnarchySubject.register(anarchy_runtime=anarchy_runtime, **kwargs)
    subject.initialize_metadata(anarchy_runtime)


@kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
def subject_update(new, old, **kwargs):
    subject = AnarchySubject.register(anarchy_runtime=anarchy_runtime, **kwargs)
    if old['spec'] != new['spec']:
        # Updates are checked againts sha256 annotition to allow Anarchy to
        # skip processing when this annotation is updated along with the spec
        if subject.check_spec_changed(anarchy_runtime=anarchy_runtime):
            subject.process_subject_event_handlers(
                anarchy_runtime=anarchy_runtime,
                event_name='update',
                old=old,
            )


@kopf.on.delete(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
def subject_delete(**kwargs):
    subject = AnarchySubject.register(anarchy_runtime=anarchy_runtime, **kwargs)
    event_handled = subject.process_subject_event_handlers(anarchy_runtime, 'delete')
    if event_handled:
        subject.record_delete_started(anarchy_runtime)
    if not event_handled:
        # Remove custom subject event handlers to allow delete to complete
        subject.remove_finalizers(anarchy_runtime)


@kopf.on.event(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects')
def subject_event(event, logger, **_):
    '''
    Monitor for update events after delete has begun.
    The Kopf framework stops tracking updates after the delete handler has succeeded.
    '''
    if 'object' not in event:
        return

    obj = event.get('object')
    if obj.get('apiVersion') != anarchy_runtime.api_group_version:
        return

    if event['type'] == 'DELETED':
        AnarchySubject.unregister(obj['metadata']['name'])
    elif event['type'] in ['ADDED', 'MODIFIED', None]:
        subject = AnarchySubject.register(
            logger = logger,
            resource_object = obj,
            anarchy_runtime = anarchy_runtime,
        )
        if subject.delete_started \
        and anarchy_runtime.subject_label in subject.metadata.get('finalizers', []):
            # Manually interact with the kopf last-handled-configuration
            # annotation that is usually managed by kopf.
            last_handled_configuration = subject.last_handled_configuration
            if last_handled_configuration and subject.spec != last_handled_configuration['spec']:
                # Updates are checked againts sha256 annotition to allow Anarchy to
                # skip processing when this annotation is updated along with the spec
                if subject.check_spec_changed(anarchy_runtime=anarchy_runtime):
                    subject.process_subject_event_handlers(
                        anarchy_runtime=anarchy_runtime,
                        event_name='update',
                        old=last_handled_configuration,
                    )

                # Update kopf last-handled-configuration annotation
                resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    subject.namespace, 'anarchysubjects', subject.name,
                    {
                        'metadata': {
                            'annotations': {
                                'kopf.zalando.org/last-handled-configuration': json.dumps({
                                    'metadata': {
                                        'annotations': {
                                            k: v for k, v in subject.metadata.get('annotations', {}).items()
                                            if k != 'kopf.zalando.org/last-handled-configuration'
                                        },
                                        'labels': subject.metadata.get('labels', {})
                                    },
                                    'spec': subject.spec,
                                }),
                            }
                        }
                    }
                )
                subject.__init__(resource_object)


@kopf.daemon(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchysubjects', cancellation_timeout=1)
async def subject_daemon(stopped, **kwargs):
    subject = AnarchySubject.register(anarchy_runtime=anarchy_runtime, **kwargs)
    try:
        while not stopped:
            if subject.active_action_name:
                action_name = subject.active_action_name
                action = AnarchyAction.get_from_cache(action_name)
                if not action:
                    action = AnarchyAction.get_from_api(
                        anarchy_runtime = anarchy_runtime,
                        name = action_name,
                    )
                if not action:
                    subject.logger.warning(
                        "Active AnarchyAction not found!",
                        extra = dict(action=subject.active_action_ref),
                    )
                    subject.remove_action_from_status(
                        action_name = action_name,
                        anarchy_runtime = anarchy_runtime,
                    )
            for action_ref in subject.pending_actions:
                action_name = action_ref['name']
                action = AnarchyAction.get_from_cache(action_name)
                if not action:
                    action = AnarchyAction.get_from_api(
                        anarchy_runtime = anarchy_runtime,
                        name = action_name,
                    )
                if not action:
                    subject.logger.warning(
                        "Pending AnarchyAction not found!",
                        extra = dict(action=action_ref),
                    )
                    subject.remove_action_from_status(
                        action_name = action_name,
                        anarchy_runtime = anarchy_runtime,
                    )
            for run_ref in subject.active_runs:
                run_name = run_ref['name']
                run = AnarchyRun.get_from_cache(run_name)
                if not run:
                    run = AnarchyRun.get_from_api(
                        anarchy_runtime = anarchy_runtime,
                        name = run_name,
                    )
                if not run:
                    subject.logger.warning(
                        "Active AnarchyRun not found!",
                        extra = dict(run=run_ref),
                    )
                    subject.remove_run_from_status(
                        anarchy_runtime = anarchy_runtime,
                        run_name = run_name,
                    )
            await asyncio.sleep(cleanup_interval)
    except asyncio.CancelledError:
        pass
    except asyncio.CancelledError:
        pass


@kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
def action_create(**kwargs):
    action = AnarchyAction.register(anarchy_runtime=anarchy_runtime, **kwargs)
    action.set_owner_references(
        anarchy_runtime = anarchy_runtime,
    )
    subject = action.get_subject()
    subject.add_action_to_status(
        action = action,
        anarchy_runtime = anarchy_runtime,
    )


@kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
def action_resume(**kwargs):
    action = AnarchyAction.register(anarchy_runtime=anarchy_runtime, **kwargs)
    subject = action.get_subject()
    if not subject:
        raise kopf.TemporaryError(
            "Unable to find AnarchySubject for AnarchyAction",
            delay = 5
        )
    if action.is_finished:
        return
    subject.add_action_to_status(
        action = action,
        anarchy_runtime = anarchy_runtime,
    )


@kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
def action_update(new, old, **kwargs):
    action = AnarchyAction.register(anarchy_runtime=anarchy_runtime, **kwargs)
    subject = action.get_subject()
    if not subject:
        raise kopf.TemporaryError(
            "Unable to find AnarchySubject for AnarchyAction",
            delay = 5
        )

    if action.is_finished:
        # Action just finished, remove from AnarchySubject status
        if not old['metadata'].get('labels', {}).get(anarchy_runtime.finished_label):
            subject.remove_action_from_status(
                action_name = action.name,
                anarchy_runtime = anarchy_runtime,
            )
    else:
        # Action was rescheduled, update schedule in AnarchySubject status
        if new['spec'].get('after') != old['spec'].get('after'):
            subject.reschedule_action_in_status(action=action, anarchy_runtime=anarchy_runtime)


@kopf.on.delete(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions')
def action_delete(logger, name, spec, **kwargs):
    AnarchyAction.unregister(name)
    subject_name = spec.get('subjectRef', {}).get('name')

    if subject_name:
        subject = AnarchySubject.get(subject_name)
        if subject:
            subject.remove_action_from_status(
                action_name = name,
                anarchy_runtime = anarchy_runtime,
            )


@kopf.daemon(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyactions', cancellation_timeout=1)
async def action_daemon(stopped, **kwargs):
    action = AnarchyAction.register(anarchy_runtime=anarchy_runtime, **kwargs)
    try:
        while not stopped:
            if not action.has_run_scheduled:
                if action.after_datetime <= datetime.utcnow():
                    subject = action.get_subject()
                    if not subject:
                        raise kopf.TemporaryError(
                            "Unable to find AnarchySubject for AnarchyAction",
                            delay = 10
                        )
                    if subject.active_action_name:
                        if action.name == subject.active_action_name:
                            action.start(anarchy_runtime = anarchy_runtime)
                    else:
                        if action.name == subject.next_pending_action_name:
                            action.start(anarchy_runtime = anarchy_runtime)
                await asyncio.sleep(action_check_interval)
            elif action.is_finished:
                governor = action.get_governor()
                if governor:
                    delete_datetime = action.finished_datetime + governor.remove_finished_actions_after
                    if delete_datetime <= datetime.utcnow():
                        action.delete(anarchy_runtime)
                else:
                    action.delete(anarchy_runtime)
                await asyncio.sleep(cleanup_interval)
            else:
                await asyncio.sleep(action_check_interval)
    except asyncio.CancelledError:
        pass


@kopf.on.create(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyruns', id="run_create")
@kopf.on.resume(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyruns', id="run_resume")
def run_create_or_resume(**kwargs):
    run = AnarchyRun.register(anarchy_runtime=anarchy_runtime, **kwargs)

    # Do not do anything else with run if it is finished already
    if anarchy_runtime.finished_label in run.labels:
        return

    subject = run.get_subject()
    if not subject:
        raise kopf.TemporaryError(
            "Unable to find AnarchySubject for AnarchyRun",
            delay = 5
        )

    if run.action_name:
        action = run.add_to_action_status(anarchy_runtime=anarchy_runtime)
        if not action:
            raise kopf.TemporaryError(
                "Unable to find AnarchyAction for AnarchyRun!",
                delay = 5
            )

    subject.add_run_to_status(anarchy_runtime=anarchy_runtime, run=run)

@kopf.on.update(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyruns')
def run_update(new, old, **kwargs):
    run = AnarchyRun.register(anarchy_runtime=anarchy_runtime, **kwargs)

    new_labels = new['metadata'].get('labels', {})
    old_labels = old['metadata'].get('labels', {})
    new_runner_label_value = new_labels.get(anarchy_runtime.runner_label)
    old_runner_label_value = old_labels.get(anarchy_runtime.runner_label)

    # Only do something if runner value changes
    if new_runner_label_value == old_runner_label_value:
        return

    governor = run.get_governor()
    if not governor:
        raise kopf.TemporaryError(
            f"AnarchyRun posted result but cannot find AnarchyGovernor {run.governor_name}!",
            delay = 5
        )

    subject = run.get_subject()
    if not subject:
        raise kopf.TemporaryError(
            f"AnarchyRun posted result but cannot find AnarchySubject {run.subject_name}!",
            delay = 5
        )

    if run.result_status == 'successful':
        subject.remove_run_from_status(
            anarchy_runtime = anarchy_runtime,
            run_name = run.name,
        )

        if run.action_name:
            action = AnarchyAction.get_from_cache(run.action_name)
            if not action:
                action = AnarchyAction.get_from_api(
                    anarchy_runtime = anarchy_runtime,
                    name = run.action_name, 
                )
            if not action:
                raise kopf.TemporaryError(
                    f"AnarchyRun posted result but cannot find AnarchyAction {run.action_name}!",
                    delay = 5
                )

            action_config = governor.action_config(action.action)
            if not action_config:
                raise kopf.TemporaryError(
                    "AnarchyRun posted result but cannot action config for AnarchyAction in AnarchyGovernor!",
                    delay = 5
                )

            continue_action_after = run.continue_action_after
            if continue_action_after:
                run.logger.info(
                    "Scheduling continuation for AnarchyAction",
                    extra = {
                        "action": action.reference,
                        "after": continue_action_after,
                    },
                )
                action.schedule_continuation(
                    after = continue_action_after,
                    anarchy_runtime = anarchy_runtime,
                )
            elif action_config.finish_on_successful_run:
                run.logger.info(
                    "Marking AnarchyAction as finished after successful AnarchyRun",
                    extra = dict(action=action.reference),
                )
                action.set_finished(
                    anarchy_runtime = anarchy_runtime,
                    state = 'successful',
                )
    else:
        extra = dict(
            retryAfter = run.retry_after,
            runner = run.runner_reference,
            runnerPod = run.runner_pod_reference,
            status = run.result_status,
            statusMessage = run.result_status_message,
            subject = run.subject_reference,
        )
        if run.action_reference:
            extra['action'] = run.action_reference
        run.logger.warning(
            "AnarchyRun failed, will retry",
            extra = extra
        )
        subject.set_run_failure_in_status(
            anarchy_runtime = anarchy_runtime,
            run = run,
        )


@kopf.on.delete(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyruns')
def run_delete(logger, name, spec, status, **_):
    AnarchyRun.unregister(name)

    subject_name = spec.get('subject', {}).get('name')
    if subject_name:
        subject = AnarchySubject.get(subject_name)
        if subject:
            subject.remove_run_from_status(
                anarchy_runtime = anarchy_runtime,
                run_name = name,
            )

    runner_ref = status.get('runner')
    runner_pod = status.get('runnerPod')
    if runner_ref and runner_pod:
        runner = AnarchyRunner.get(runner_ref['name'])
        runner.clear_pod_run_reference(
            anarchy_runtime = anarchy_runtime,
            run_name = name,
            runner_pod_name = runner_pod['name'],
        )


@kopf.daemon(anarchy_runtime.operator_domain, anarchy_runtime.api_version, 'anarchyruns', cancellation_timeout=1)
async def run_daemon(stopped, **kwargs):
    run = AnarchyRun.register(anarchy_runtime=anarchy_runtime, **kwargs)
    try:
        while not stopped:
            subject = run.get_subject()
            if not subject:
                run.logger.info(
                    'AnarchySubject found deleted in AnarchyRun daemon',
                    extra = dict(subject=run.subject_reference)
                )
                # The AnarchyRun should auto-delete following the AnarhySubject
                run.delete(anarchy_runtime)
                return

            governor = run.get_governor()
            if not governor:
                run.logger.warning(
                    'Cannot find AnarchyGovernor for AnarchyRun!',
                    extra = dict(governor=run.governor_reference)
                )
                return

            # Delete if finished for longer than remove successful runs after configuration
            if anarchy_runtime.finished_label in run.labels:
                if governor:
                    delete_datetime = run.run_post_datetime + governor.remove_successful_runs_after
                    if delete_datetime <= datetime.utcnow():
                        run.delete(anarchy_runtime)
                else:
                    run.delete(anarchy_runtime)
                await asyncio.sleep(cleanup_interval)
            else:
                run.manage(anarchy_runtime=anarchy_runtime)
                await asyncio.sleep(run_check_interval)
    except asyncio.CancelledError:
        pass


@api.route('/action/<string:action_name>', methods=['POST'])
def action_callback(action_name):
    return handle_action_callback(action_name, None)
@api.route('/action/<string:action_name>/<string:callback_name>', methods=['POST'])
def named_action_callback(action_name, callback_name):
    return handle_action_callback(action_name, callback_name)

def handle_action_callback(action_name, callback_name):
    if not flask.request.json:
        flask.abort(400)

    action_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchyAction',
        name = action_name,
        namespace = anarchy_runtime.operator_namespace,
    )

    action = AnarchyAction.get_from_api(
        anarchy_runtime = anarchy_runtime,
        name = action_name,
    )
    if not action:
        operator_logger.warning(
            "AnarchyAction not found for callback",
            extra=dict(action=action_ref, callback=callback_name)
        )
        flask.abort(404)

    if not action.check_callback_token(flask.request.headers.get('Authorization', '')):
        action.logger.warning(
            "Invalid callback token for AnarchyAction",
            extra=dict(callback=callback_name)
        )
        flask.abort(403)
    if action.finished_timestamp:
        action.logger.warning(
            "Invalid callback to finished AnarchyAction",
            extra=dict(callback=callback_name)
        )
        flask.abort(400)

    action.process_callback(anarchy_runtime, callback_name, flask.request.json)
    return flask.jsonify({'status': 'ok'})


@api.route('/run', methods=['GET'])
def get_run():
    runner, runner_pod, run_ref, subject_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not runner:
        flask.abort(400)

    logger = runner.local_logger

    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )

    if run_ref:
        logger.warning(
            'AnarchyRunner requesting new run when an AnarchyRun should still be running',
            extra = dict(pod=runner_pod_ref, run=run_ref, subject=subject_ref)
        )
        runner.clear_pod_run_reference(
            anarchy_runtime = anarchy_runtime,
            runner_pod_name = runner_pod.metadata.name,
        )

        lost_run = AnarchyRun.get_from_api(
            anarchy_runtime = anarchy_runtime,
            name = run_ref['name'],
        )
        if lost_run \
        and lost_run.runner_pod_name == runner_pod.metadata.name:
            lost_run.handle_lost_runner(anarchy_runtime)

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
        run = AnarchyRun.get_pending(anarchy_runtime)
        if not run:
            logger.debug(
                'No AnarchyRun pending for AnarchyRunner',
                extra = dict(pod=runner_pod_ref)
            )
            return flask.jsonify(None)
        if run.set_runner_pod(
            anarchy_runtime = anarchy_runtime,
            runner = runner,
            runner_pod = runner_pod,
        ):
            # If successfully set runner on anarchy run, then break from loop
            break

    subject = run.get_subject()
    if not subject:
        logger.warning(
            'AnarchyRun was pending, but cannot find AnarchySubject!',
            extra = dict(
                pod = runner_pod_ref,
                run = run.reference,
                subject = run.subject_reference,
            )
        )
        return flask.jsonify(None)

    governor = subject.get_governor()
    if not governor:
        logger.warning(
            'AnarchyRun was pending, but cannot find AnarchyGovernor!',
            extra = dict(
                governor = subject.governor_reference,
                pod = runner_pod_ref,
                run = run.reference,
                subject = subject.reference,
            )
        )
        return flask.jsonify(None)

    runner.set_pod_run_reference(
        anarchy_runtime = anarchy_runtime,
        run = run,
        runner_pod = runner_pod,
    )
    resp = run.to_dict()
    resp['governor'] = governor.to_dict()
    resp['subject'] = subject.to_dict()
    return flask.jsonify(resp)


@api.route('/run/<string:run_name>', methods=['POST'])
def post_run(run_name):
    runner, runner_pod, run_ref, subject_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not runner:
        flask.abort(400)

    logger = runner.local_logger

    post_run_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchyRun',
        name = run_name,
        namespace = anarchy_runtime.operator_namespace,
    )
    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )

    runner.clear_pod_run_reference(
        runner_pod_name = runner_pod.metadata.name,
        anarchy_runtime = anarchy_runtime,
    )

    # When an AnarchyRun is handling a delete completion it is normal for the
    # AnarchyRun and AnarchySubject to be deleted before the post is received.
    run = AnarchyRun.get_from_api(
        anarchy_runtime = anarchy_runtime,
        name = run_name,
    )
    if not run:
        logger.info(
            'AnarchyRunner Pod posted result on deleted AnarchyRun',
            extra = dict(pod=runner_pod_ref, run=post_run_ref)
        )
        return flask.jsonify({'success': True, 'msg': 'AnarchyRun not found'})

    anarchy_subject = run.get_subject()
    if not anarchy_subject:
        logger.info(
            'AnarchyRunner Pod posted result on deleted AnarchySubject',
            extra = dict(pod=runner_pod_ref, run=run.reference, subject=run.subject_reference)
        )
        return flask.jsonify({'success': True, 'msg': 'AnarchySubject not found'})

    if not run_ref or run_name != run_ref['name']:
        logger.error(
            'AnarchyRunner Pod attempted to post run that it was not assigned!',
            extra = dict(attempted=post_run_ref, expected=run_ref, pod=runner_pod_ref, subject=subject_ref)
        )
        flask.abort(400)

    try:
        result = flask.request.json['result']
    except KeyError:
        logger.error(
            'AnarchyRunner Pod posted invalid run data!',
            extra = dict(pod=runner_pod_ref, run=run.reference, subject=subject.reference)
        )
        flask.abort(400, flask.jsonify(
            {'success': False, 'error': 'Invalid run data'}
        ))

    run.post_result(
        result = result,
        anarchy_runtime = anarchy_runtime,
    )
    return flask.jsonify({'success':True})


@api.route('/run/subject/<string:subject_name>', methods=['PATCH','DELETE'])
def patch_or_delete_subject(subject_name):
    runner, runner_pod, run_ref, subject_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not runner:
        flask.abort(400)

    logger = runner.local_logger

    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )
    target_subject_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchySubject',
        name = subject_name,
        namespace = anarchy_runtime.operator_namespace,
    )

    if not subject_ref or subject_name != subject_ref['name']:
        logger.error(
            'AnarchyRunner Pod cannot update AnarchySubject not associated with its AnarchyRun!',
            extra = dict(
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
                target = target_subject_ref,
            )
        )
        flask.abort(400)

    anarchy_subject = AnarchySubject.get(subject_name)
    if not anarchy_subject:
        operator_logger.warning(
            f"AnarchyRunner Pod attempted {flask.request.method} of a deleted AnarchySubject!",
            extra = dict(
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
            )
        )
        flask.abort(400)

    if flask.request.method == 'PATCH':
        if not 'patch' in flask.request.json:
            operator_logger.warning('No patch in AnarchySubject %s post', subject_name)
            flask.abort(400)
        result = anarchy_subject.patch(flask.request.json['patch'], anarchy_runtime)
    elif flask.request.method == 'DELETE':
        result = anarchy_subject.delete(
            anarchy_runtime = anarchy_runtime,
            remove_finalizers = flask.request.json.get('remove_finalizers', False),
        )

    return flask.jsonify({'success': True, 'result': result})


@api.route('/run/subject/<string:subject_name>/actions', methods=['POST'])
def run_subject_action_post(subject_name):
    runner, runner_pod, run_ref, subject_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not runner:
        flask.abort(400)

    logger = runner.local_logger

    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )
    target_subject_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchySubject',
        name = subject_name,
        namespace = anarchy_runtime.operator_namespace,
    )

    if not subject_ref or subject_name != subject_ref['name']:
        logger.error(
            'AnarchyRunner Pod cannot update create AnarchyAction for AnarchySubject not associated with its AnarchyRun!',
            extra = dict(
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
                target = target_subject_ref,
            )
        )
        flask.abort(400)

    subject = AnarchySubject.get(subject_name)
    if not subject:
        logger.warning(
            'AnarchyRunner pod attempted to create AnarchyAction on deleted AnarchySubject!',
            extra = dict(
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
            )
        )
        flask.abort(400)

    governor = subject.get_governor()
    if not governor:
        logger.error(
            'AnarchyRunner Pod cannot create AnarchyAction, unable to find AnarchyGovernor!',
            extra = dict(
                governor = subject.governor_reference,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject.reference,
            )
        )
        flask.abort(400)

    action_name = flask.request.json.get('action', None)
    after_timestamp = flask.request.json.get('after', None)
    cancel_actions = flask.request.json.get('cancel', None)

    if not action_name and not cancel_actions:
        logger.warning(
            'No action or cancel given for scheduling action',
            extra = dict(
                governor = governor.reference,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject.reference,
            )
        )
        flask.abort(400)

    if after_timestamp and not re.match(r'\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ', after_timestamp):
        runner.logger.warning(
            'Invalid datetime format given for action after value',
            extra = dict(
                after = after_timestamp,
                governor = governor.reference,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject.reference,
            )
        )
        flask.abort(400)

    # Scheduling an action always implies canceling any existing instances of the same action
    if action_name not in cancel_actions:
        cancel_actions.append(action_name)

    for action_resource in anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
        anarchy_runtime.operator_namespace, 'anarchyactions',
        label_selector=f"{anarchy_runtime.subject_label}={subject.name}"
    ).get('items', []):
        if action_resource['spec']['action'] in cancel_actions \
        and 'status' not in action_resource:
            anarchy_runtime.custom_objects_api.delete_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', action_resource['metadata']['name']
            )

    if action_name:
        result = anarchy_runtime.custom_objects_api.create_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchyactions',
            {
                "apiVersion": anarchy_runtime.api_group_version,
                "kind": "AnarchyAction",
                "metadata": {
                    "generateName": f"{subject.name}-{action_name}-",
                    "labels": {
                        anarchy_runtime.action_label: action_name,
                        anarchy_runtime.subject_label: subject.name,
                        anarchy_runtime.governor_label: governor.name
                    },
                    "ownerReferences": [subject.owner_reference]
                },
                "spec": {
                    "action": action_name,
                    "after": after_timestamp,
                    "callbackToken": uuid.uuid4().hex,
                    "governorRef": governor.reference,
                    "subjectRef": subject.reference,
                }
            }
        )
    else:
        result = None

    return flask.jsonify({'success': True, 'result': result})


@api.route('/run/subject/<string:subject_name>/actions/<string:action_name>', methods=['PATCH'])
def run_subject_action_patch(subject_name, action_name):
    """
    Callback from runner to update AnarchyAction associated with AnarchySubject assigned to runner.

    The only function of this method currently is to pass JSON, `{"successful": true}` or
    `{"failed": true}` to mark the action as finished.
    """
    runner, runner_pod, run_ref, subject_ref = AnarchyRunner.check_runner_auth(
        flask.request.headers.get('Authorization', ''), anarchy_runtime
    )
    if not runner:
        flask.abort(400)

    logger = runner.local_logger

    action_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchyAction',
        name = action_name,
        namespace = anarchy_runtime.operator_namespace,
    )
    runner_pod_ref = dict(
        apiVersion = runner_pod.api_version,
        kind = runner_pod.kind,
        name = runner_pod.metadata.name,
        namespace = runner_pod.metadata.namespace,
        uid = runner_pod.metadata.uid,
    )
    target_subject_ref = dict(
        apiVersion = anarchy_runtime.api_group_version,
        kind = 'AnarchySubject',
        name = subject_name,
        namespace = anarchy_runtime.operator_namespace,
    )

    if not subject_ref or subject_name != subject_ref['name']:
        logger.error(
            'AnarchyRunner Pod cannot update AnarchyActions for AnarchySubject!',
            extra = dict(
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
                target = target_subject_ref,
            )
        )
        flask.abort(400)

    action = AnarchyAction.get_from_api(
        anarchy_runtime = anarchy_runtime,
        name = action_name,
    )
    if not action:
        logger.warning(
            'AnarchyRunner Pod attempted to update deleted AnarchyAction!',
            extra = dict(
                action = action_ref,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
            )
        )
        flask.abort(400)

    subject = AnarchySubject.get(subject_name)
    if not subject:
        logger.warning(
            'AnarchyRunner Pod attempted to update AnarchyAction for deleted AnarchySubject!',
            extra = dict(
                action = action.reference,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject_ref,
            )
        )
        flask.abort(400)

    governor = subject.get_governor()
    if not governor:
        logger.warning(
            'AnarchyRunner Pod cannot update AnarchyAction, unable to find AnarchyGovernor!',
            extra = dict(
                action = action.reference,
                governor = subject.governor_refernce,
                pod = runner_pod_ref,
                run = run_ref,
                subject = subject.reference,
            )
        )
        flask.abort(400)

    finished_state = None
    if flask.request.json.get('successful', False):
        finished_state = 'successful'
    elif flask.request.json.get('failed', False):
        finished_state = 'failed'

    if finished_state != None:
        action.set_finished(
            anarchy_runtime = anarchy_runtime,
            state = finished_state,
        )

    return flask.jsonify({'success': True, 'result': action.to_dict()})

def run_api():
    http_server = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

def watch_active_change():
    while True:
        with anarchy_runtime.is_active_condition:
            if not anarchy_runtime.is_active:
                AnarchyRun.cancel_timers()
            anarchy_runtime.is_active_condition.wait()
