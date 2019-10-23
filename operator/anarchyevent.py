from datetime import datetime, timedelta
import logging
import random
import threading

logger = logging.getLogger('anarchy')

from anarchysubject import AnarchySubject

class AnarchyEvent(object):

    @staticmethod
    def handle_lost_runner(runner_name, runtime):
        """Notified that a runner has been lost, reset AnarchyEvents to pending"""
        for resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyevents',
            label_selector = runtime.runner_label + '=' + runner_name
        ).get('items', []):
            anarchy_event_name = resource['metadata']['name']
            anarchy_subject_name = resource['spec']['subject']['metadata']['name']
            anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
            if anarchy_subject:
                anarchy_subject.has_job_queued_or_running = False
            runtime.logger.warning(
                'Resetting AnarchyEvent %s to pending after lost runner',
                anarchy_event_name
            )
            runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchyevents', anarchy_event_name,
                { 'metadata': { 'labels': { runtime.runner_label: 'pending' } } }
            )

    @staticmethod
    def register(anarchy_event, runtime):
        runner = anarchy_event.metadata['labels'].get(runtime.runner_label, None)
        if not runner:
            return

        anarchy_subject_name = anarchy_event.subject_name
        dequeue_event_from_subject = True
        enqueue_event_to_subject = False
        event_is_running = False
        if runner.startswith('failed-'):
            runtime.logger.warning('Failure on AnarchyEvent %s', anarchy_event.name)
            anarchy_event.set_runner('failed', runtime)
            anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
            if anarchy_subject:
                anarchy_subject.has_job_queued_or_running = False
            runtime.runner_finished(runner[7:])
        elif runner.startswith('success-'):
            runtime.logger.info('Success on AnarchyEvent %s', anarchy_event.name)
            anarchy_event.set_runner(None, runtime)
            anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
            if anarchy_subject:
                anarchy_subject.has_job_queued_or_running = False
                anarchy_subject.remove_pending_event(anarchy_event, runtime)
            runtime.runner_finished(runner[8:])
        elif runner == 'failed':
            if anarchy_event.failures < 10:
                retry_delay = timedelta(seconds=5 * 2**anarchy_event.failures)
            else:
                retry_delay = timedelta(hours = 1)
            if not anarchy_event.last_run:
                runtime.logger.warning('Retrying AnarchyEvent %s (spec.lastRun not set)', anarchy_event.name)
                anarchy_event.set_runner('pending', runtime)
            elif anarchy_event.last_run < datetime.utcnow() - retry_delay:
                runtime.logger.warning('Retrying AnarchyEvent %s', anarchy_event.name)
                anarchy_event.set_runner('pending', runtime)
        elif runner == 'pending':
            enqueue_event_to_subject = True
        elif runner:
            event_is_running = True
            enqueue_event_to_subject = True

        if enqueue_event_to_subject:
            anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
            if anarchy_subject:
                anarchy_subject.enqueue_event(anarchy_event, event_is_running, runtime)
            else:
                runtime.logger.warning(
                    'Unable to get AnarchySubject %s for pending AnarchyEvent %s',
                    anarchy_subject_name, anarchy_event.name
                )

    @staticmethod
    def unregister(anarchy_event, runtime):
        anarchy_subject_name = anarchy_event.spec['subject']['metadata']['name']
        anarchy_subject = AnarchySubject.cache.get(anarchy_subject_name, None)
        if anarchy_subject:
            anarchy_subject.remove_pending_event(anarchy_event, runtime)
        runner = anarchy_event.metadata['labels'].get(runtime.runner_label, None)
        if runner:
            runtime.runner_finished(runner)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', {})
        self.sanity_check()

    def sanity_check(self):
        # FIXME
        pass

    @property
    def creation_timestamp(self):
        return self.metadata.get('creationTimestamp')

    @property
    def failures(self):
        return self.spec.get('failures', 0)

    @property
    def last_run(self):
        if 'lastRun' in self.spec:
            datetime.strptime(self.spec['lastRun'], '%Y-%m-%dT%H:%M:%SZ')
        else:
            return None

    @property
    def governor_name(self):
        return self.spec['governor']['metadata']['name']

    @property
    def name(self):
        return self.metadata['name']

    @property
    def subject_name(self):
        return self.spec['subject']['metadata']['name']

    @property
    def uid(self):
        return self.metadata['uid']

    def set_runner(self, runner_value, runtime):
        runtime.logger.debug('Set runner for AnarchyEvent %s to %s', self.name, runner_value)
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace,
            'anarchyevents', self.name,
            {
                'metadata': {
                    'labels': { runtime.runner_label: runner_value }
                }
            }
        )
