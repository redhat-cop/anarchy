from datetime import datetime, timedelta
import copy
import kubernetes
import logging
import threading

from anarchyrunner import AnarchyRunner
from anarchysubject import AnarchySubject

operator_logger = logging.getLogger('operator')

class AnarchyRun(object):
    pending_count = 0

    @staticmethod
    def get_from_api(name, runtime):
        '''
        Get AnarchyRun from api by name.
        '''
        operator_logger.debug('Getting AnarchyRun %s', name)
        resource = AnarchyRun.get_resource_from_api(name, runtime)
        if resource:
            return AnarchyRun(resource)
        else:
            return None

    @staticmethod
    def get_pending(runtime):
        '''
        Get pending AnarchyRun from api, if one exists.
        '''
        items = runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns',
            label_selector='{}=pending'.format(runtime.runner_label), limit=1
        ).get('items', [])
        if items:
            return AnarchyRun(items[0])
        else:
            return None

    @staticmethod
    def get_resource_from_api(name, runtime):
        '''
        Get raw AnarchyRun resource from api by name, if one exists.
        '''
        try:
            return runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def manage_active_runs(runtime):
        '''
        Manage AnarchyRuns, retrying failures and detecting lost runners.
        '''
        for resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns',
            label_selector='!{}'.format(runtime.finished_label)
        ).get('items', []):
            run = AnarchyRun(resource)
            run.manage(runtime)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', {})
        self.sanity_check()

    def sanity_check(self):
        # FIXME
        pass

    @property
    def action_name(self):
        action = self.spec.get('action')
        if action:
            return action['name']

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
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def result_status(self):
        return self.spec.get('result', {}).get('status')

    @property
    def result_status_message(self):
        return self.spec.get('result', {}).get('statusMessage')

    @property
    def retry_after(self):
        return self.spec.get('retryAfter')

    @property
    def retry_after_datetime(self):
        return datetime.strptime(
            self.spec['retryAfter'], '%Y-%m-%dT%H:%M:%SZ'
        ) if 'retryAfter' in self.spec else datetime.utcnow()

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
        operator_logger.warning(
            'Handling AnarchyRun %s lost runner %s', self.name, runner_pod_name
        )
        self.post_result({'status': 'lost'}, runner_pod_name, runtime)

    def manage(self, runtime):
        runner_label_value = self.get_runner_label_value(runtime)
        if runner_label_value == 'pending':
            pass
        elif runner_label_value == 'queued':
            pass
        elif runner_label_value == 'failed':
            if self.retry_after_datetime < datetime.utcnow():
                self.set_to_pending(runtime)
        elif '.' in runner_label_value: # Running, assigned to a runner pod
            runner_name, runner_pod_name = runner_label_value.split('.')
            runner = AnarchyRunner.get(runner_name)
            if runner:
                if runner.pods.get(runner_pod_name):
                    pass # FIXME - Timeout?
                else:
                    self.handle_lost_runner(runtime.runner_label, runtime)
            else:
                operator_logger.warning(
                    'Unable to find AnarchyRunner %s for AnarchyRun %s', runner_name, self.name
                )

    def post_result(self, result, runner_pod_name, runtime):
        operator_logger.info('Update AnarchyRun %s for %s run', self.name, result['status'])

        patch = [{
            'op': 'add',
            'path': '/metadata/labels/' + runtime.runner_label.replace('/', '~1'),
            'value': 'pending' if result['status'] == 'lost' else result['status']
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

        if result['status'] == 'successful':
            patch.append({
                'op': 'add',
                'path': '/metadata/labels/' + runtime.finished_label.replace('/', '~1'),
                'value': 'true',
            })

        elif result['status'] == 'failed':
            if self.failures > 8:
                retry_delay = timedelta(minutes=30)
            else:
                retry_delay = timedelta(seconds=5 * 2**self.failures)
            patch.append({
                'op': 'add',
                'path': '/spec/failures',
                'value': self.failures + 1
            })
            patch.append({
                'op': 'add',
                'path': '/spec/retryAfter',
                'value': (datetime.utcnow() + retry_delay).strftime('%FT%TZ')
            })

        try:
            data = runtime.custom_objects_api.api_client.call_api(
                '/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}',
                'PATCH',
                { # path params
                    'group': runtime.operator_domain,
                    'version': runtime.api_version,
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
            self.refresh_from_resource(data[0])
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                operator_logger.warning('Unable to updated deleted AnarchyRun %s', self.name)
            else:
                raise

    def set_runner(self, runner_value, runtime):
        resource_def = copy.deepcopy(self.to_dict(runtime))
        resource_def['metadata']['labels'][runtime.runner_label] = runner_value
        try:
            resource = runtime.custom_objects_api.replace_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, self.namespace, 'anarchyruns', self.name, resource_def
            )
            self.refresh_from_resource(resource)
            operator_logger.info('Set runner for AnarchyRun %s to %s', self.name, runner_value)
        except kubernetes.client.rest.ApiException as e:
            if e.status == 409:
                # Failed to set runner due to conflict
                return False
            else:
                raise
        return True

    def ref(self, runtime):
        return dict(
            apiVersion = runtime.api_group_version,
            kind = 'AnarchyRun',
            name = self.name,
            namespace = self.namespace,
            uid = self.uid
        )

    def refresh_from_resource(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']

    def set_to_pending(self, runtime):
        resource = runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, self.namespace, 'anarchyruns', self.name,
            {'metadata': {'labels': { runtime.runner_label: 'pending' } } }
        )
        self.refresh_from_resource(resource)

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.api_group_version,
            kind = 'AnarchyRun',
            metadata=self.metadata,
            spec=self.spec,
            status=self.status
        )
