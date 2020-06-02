from datetime import datetime, timedelta
import kubernetes
import logging
import threading

from anarchyrunner import AnarchyRunner
from anarchysubject import AnarchySubject

operator_logger = logging.getLogger('operator')

class AnarchyRun(object):

    @staticmethod
    def register(anarchy_run, runtime):
        runner_value = anarchy_run.metadata['labels'].get(runtime.runner_label, None)
        anarchy_subject_name = anarchy_run.subject_name
        anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)

        if not anarchy_subject:
            operator_logger.warn(
                'Unable to find AnarchySubject %s for AnarchyRun %s',
                anarchy_subject_name, anarchy_run.name
            )
            return

        if not runner_value or runner_value == 'successful':
            return
        elif runner_value == 'failed':
            anarchy_subject.anarchy_run_update(anarchy_run, runtime)
            last_attempt = anarchy_run.run_post_datetime
            if last_attempt:
                if anarchy_run.failures > 9:
                    retry_delay = timedelta(hours=1)
                else:
                    retry_delay = timedelta(seconds=5 * 2**anarchy_run.failures)
                next_attempt = last_attempt + retry_delay
            else:
                next_attempt = datetime.utcnow()
            anarchy_subject.retry_after = next_attempt
        elif runner_value == 'lost':
            anarchy_subject.anarchy_run_update(anarchy_run, runtime)
            anarchy_subject.put_in_job_queue(runtime)
        elif runner_value == 'pending':
            anarchy_subject.enqueue_anarchy_run(anarchy_run, runtime)
        else:
            anarchy_subject.anarchy_run_update(anarchy_run, runtime)

    @staticmethod
    def unregister(anarchy_run, runtime):
        '''Remove AnarchyRun from cache in AnarchySubject'''
        anarchy_subject = AnarchySubject.cache.get(anarchy_run.subject_name, None)
        if anarchy_subject:
            anarchy_subject.remove_anarchy_run(anarchy_run, runtime)

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
        return self.spec['governor']['name']

    @property
    def run_post_datetime(self):
        if 'runPostTimestamp' in self.spec:
            return datetime.strptime(self.spec['runPostTimestamp'], '%Y-%m-%dT%H:%M:%SZ')
        else:
            return None

    @property
    def run_post_timestamp(self):
        return self.spec.get('runPostTimestamp')

    @property
    def name(self):
        return self.metadata['name']

    @property
    def subject_name(self):
        return self.spec['subject']['name']

    @property
    def uid(self):
        return self.metadata['uid']

    def get_runner_label_value(self, runtime):
        return self.metadata.get('labels', {}).get(runtime.runner_label, None)

    def get_subject(self, runtime):
        return AnarchySubject.get(self.subject_name, runtime)

    def handle_lost_runner(self, runner_pod_name, runtime):
        """Notified that a runner has been lost, reset AnarchyRun to pending"""
        operator_logger.warn(
            'Resetting AnarchyRun %s to lost', self.name
        )
        self.get_subject(runtime).run_queue_release()
        self.post_result({'status': 'lost'}, runner_pod_name, runtime)

    def post_result(self, result, runner_pod_name, runtime):
        operator_logger.info('Update AnarchyRun %s for %s run', self.name, result['status'])

        patch = [{
            'op': 'add',
            'path': '/metadata/labels/{0}~1runner'.format(runtime.operator_domain),
            'value': result['status']
        },{
            'op': 'add',
            'path': '/spec/result',
            'value': result
        },{
            'op': 'add',
            'path': '/spec/runner',
            'value': runner_pod_name
        },{
            'op': 'add',
            'path': '/spec/runPostTimestamp',
            'value': datetime.utcnow().strftime('%FT%TZ')
        }]

        if result['status'] == 'failed':
            patch.append({
                'op': 'add',
                'path': '/spec/failures',
                'value': self.failures + 1
            })

        try:
            runtime.custom_objects_api.api_client.call_api(
                '/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}',
                'PATCH',
                { # path params
                    'group': runtime.operator_domain,
                    'version': 'v1',
                    'plural': 'anarchyruns',
                    'namespace': runtime.operator_namespace,
                    'name': self.name
                },
                [], # query params
                { # header params
                    'Accept': 'application/json',
                    'Content-Type': 'application/json-patch+json',
                },
                body=patch,
                response_type='object',
                auth_settings=['BearerToken'],
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                operator_logger.warn('Unable to updated deleted AnarchyRun %s', self.name)
            else:
                raise

        if result['status'] == 'successful':
            anarchy_subject = self.get_subject(runtime)
            if anarchy_subject:
                anarchy_subject.remove_anarchy_run(self, runtime)

    def set_runner(self, runner_value, runtime):
        operator_logger.debug('Set runner for AnarchyRun %s to %s', self.name, runner_value)
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace,
            'anarchyruns', self.name,
            {
                'metadata': {
                    'labels': { runtime.runner_label: runner_value }
                }
            }
        )

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.operator_domain + '/v1',
            kind = 'AnarchyRun',
            metadata=self.metadata,
            spec=self.spec,
            status=self.status
        )
