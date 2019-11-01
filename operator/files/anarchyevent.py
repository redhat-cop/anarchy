from datetime import datetime, timedelta
import kubernetes
import logging
import random
import threading

from anarchysubject import AnarchySubject

operator_logger = logging.getLogger('operator')

class AnarchyEvent(object):

    @staticmethod
    def handle_lost_runner(runner_name, runtime):
        """Notified that a runner has been lost, reset AnarchyEvents to pending"""
        for resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyevents',
            label_selector = runtime.runner_label + '=' + runner_name
        ).get('items', []):
            anarchy_event = AnarchyEvent(resource)
            operator_logger.warn(
                'Resetting AnarchyEvent %s to failed after lost runner',
                anarchy_event.name
            )
            anarchy_event.get_subject(runtime).run_queue_release()
            anarchy_event.post_result({'status': 'lost'}, runner_name, runtime)

    @staticmethod
    def register(anarchy_event, runtime):
        runner = anarchy_event.metadata['labels'].get(runtime.runner_label, None)
        anarchy_subject_name = anarchy_event.subject_name
        anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)

        if not anarchy_subject:
            operator_logger.warn(
                'Unable to find AnarchySubject %s for AnarchyEvent %s',
                anarchy_subject_name, anarchy_event.name
            )
            return

        if not runner or runner == 'successful':
            return
        elif runner == 'failed':
            anarchy_subject.event_update(anarchy_event, runtime)
            if anarchy_event.failures < 10:
                retry_delay = timedelta(seconds=5 * 2**anarchy_event.failures)
            else:
                retry_delay = timedelta(hours = 1)
            if not anarchy_event.last_run:
                operator_logger.warn(
                    'Retrying AnarchyEvent %s (spec.lastRun not set)', anarchy_event.name
                )
                anarchy_subject.put_in_job_queue(runtime)
            elif anarchy_event.last_run < datetime.utcnow() - retry_delay:
                operator_logger.warn('Retrying AnarchyEvent %s', anarchy_event.name)
                anarchy_subject.put_in_job_queue(runtime)
        elif runner == 'lost':
            anarchy_subject.event_update(anarchy_event, runtime)
            anarchy_subject.put_in_job_queue(runtime)
        elif runner == 'pending':
            anarchy_subject.enqueue_event(anarchy_event, runtime)
        else:
            anarchy_subject.event_update(anarchy_event, runtime)

    @staticmethod
    def unregister(anarchy_event, runtime):
        '''Remove AnarchyEvent from cache in AnarchySubject'''
        anarchy_subject = AnarchySubject.cache.get(anarchy_event.subject_name, None)
        if anarchy_subject:
            anarchy_subject.remove_active_event(anarchy_event, runtime)

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
    def governor_name(self):
        return self.spec['governor']['metadata']['name']

    @property
    def last_run(self):
        if 'lastRun' in self.spec:
            datetime.strptime(self.spec['lastRun'], '%Y-%m-%dT%H:%M:%SZ')
        else:
            return None

    @property
    def log(self):
        return self.spec.get('log', [])

    @property
    def name(self):
        return self.metadata['name']

    @property
    def subject_name(self):
        return self.spec['subject']['metadata']['name']

    @property
    def uid(self):
        return self.metadata['uid']

    def get_runner_name(self, runtime):
        return self.metadata.get('labels', {}).get(runtime.runner_label, None)

    def get_subject(self, runtime):
        return AnarchySubject.get(self.subject_name, runtime)

    def post_result(self, result, runner_name, runtime):
        operator_logger.info('Update AnarchyEvent %s for %s run', self.name, result['status'])

        timestamp = datetime.utcnow().strftime('%FT%TZ')
        patch = {
            'metadata': {
                'labels': {runtime.operator_domain + '/runner': result['status']}
            },
            'spec': {
                'lastRun': timestamp,
                'log': self.log + [{
                    'result': result,
                    'runner': runner_name,
                    'timestamp': timestamp
                }]
            }
        }
        if result['status'] == 'failed':
            patch['spec']['failures'] = self.failures + 1

        try:
            runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchyevents', self.name, patch
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                operator_logger.warn('Unable to updated deleted AnarchyEvent %s', self.name)
            else:
                raise

        if result['status'] == 'successful':
            anarchy_subject = self.get_subject(runtime)
            if anarchy_subject:
                anarchy_subject.remove_active_event(self, runtime)

    def set_runner(self, runner_value, runtime):
        operator_logger.debug('Set runner for AnarchyEvent %s to %s', self.name, runner_value)
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace,
            'anarchyevents', self.name,
            {
                'metadata': {
                    'labels': { runtime.runner_label: runner_value }
                }
            }
        )

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.operator_domain + '/v1',
            kind = 'AnarchyEvent',
            metadata=self.metadata,
            spec=self.spec,
            status=self.status
        )
