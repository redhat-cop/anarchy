import copy
import kopf
import kubernetes
import threading

from anarchyrunner import AnarchyRunner
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction
from datetime import datetime, timedelta

class AnarchyRun(object):
    """
    AnarchyRun class
    """

    register_lock = threading.Lock()
    runs = {}

    @staticmethod
    def get_from_api(name, anarchy_runtime):
        '''
        Get AnarchyRun from api by name.
        '''
        resource_object = AnarchyRun.get_resource_from_api(name, anarchy_runtime)
        if resource_object:
            return AnarchyRun(resource_object=resource_object)

    @staticmethod
    def get_from_cache(name):
        return AnarchyRun.runs.get(name)

    @staticmethod
    def get_pending(anarchy_runtime):
        '''
        Get pending AnarchyRun from api, if one exists.
        '''
        items = anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchyruns',
            label_selector='{}=pending'.format(anarchy_runtime.runner_label), limit=1
        ).get('items', [])
        if items:
            return AnarchyRun(resource_object=items[0])
        else:
            return None

    @staticmethod
    def get_resource_from_api(name, anarchy_runtime):
        '''
        Get raw AnarchyRun resource from api by name, if one exists.
        '''
        try:
            return anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyruns', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def register(
        anarchy_runtime=None,
        annotations=None,
        labels=None,
        logger=None,
        meta=None,
        name=None,
        namespace=None,
        resource_object=None,
        spec=None,
        status=None,
        uid=None,
        **_
    ):
        with AnarchyRun.register_lock:
            if resource_object:
                name = resource_object['metadata']['name']
            else:
                resource_object = dict(
                    apiVersion = anarchy_runtime.api_group_version,
                    kind = 'AnarchyRun',
                    metadata = dict(
                        annotations = dict(annotations) if annotations else None,
                        creationTimestamp = meta["creationTimestamp"],
                        deletionTimestamp = meta.get("deletionTimestamp"),
                        labels = dict(labels) if labels else None,
                        name = name,
                        namespace = namespace,
                        resourceVersion = meta["resourceVersion"],
                        uid = uid,
                    ),
                    spec = dict(spec),
                    status = dict(status) if status else {},
                )
            run = AnarchyRun.runs.get(name)
            if run:
                run.__init__(logger=logger, resource_object=resource_object)
                run.local_logger.debug("Refreshed AnarchyRun")
            else:
                run = AnarchyRun(resource_object=resource_object, logger=logger)
                AnarchyRun.runs[run.name] = run
                run.local_logger.info("Registered AnarchyRun")
            return run

    @staticmethod
    def unregister(name):
        with AnarchyRun.register_lock:
            if name in AnarchyRun.runs:
                run = AnarchyRun.runs.pop(name)
                run.local_logger.info("Unregistered AnarchyRun")
                return run

    def __init__(self, resource_object, logger=None):
        self.api_version = resource_object['apiVersion']
        self.kind = resource_object['kind']
        self.metadata = resource_object['metadata']
        self.spec = resource_object['spec']
        self.status = resource_object.get('status', {})

        self.local_logger = kopf.LocalObjectLogger(
            body = resource_object,
            settings = kopf.OperatorSettings(),
        )
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = self.local_logger

    @property
    def action_name(self):
        action = self.spec.get('action')
        if action:
            return action['name']

    @property
    def action_namespace(self):
        action = self.spec.get('action')
        if action:
            return action['namespace']

    @property
    def action_reference(self):
        action = self.spec.get('action')
        if action:
            return dict(
                apiVersion = action['apiVersion'],
                kind = action['kind'],
                name = action['name'],
                namespace = action['namespace'],
                uid = action['uid'],
            )

    @property
    def continue_action_after(self):
        timestamp = self.continue_action_after_timestamp
        if timestamp:
            return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')

    @property
    def continue_action_after_timestamp(self):
        return self.status.get('result', {}).get('continueAction', {}).get('after')

    @property
    def continue_action_vars(self):
        return self.status.get('result', {}).get('continueAction', {}).get('vars', {})

    @property
    def creation_timestamp(self):
        return self.metadata.get('creationTimestamp')

    @property
    def failures(self):
        return self.status.get('failures', 0)

    @property
    def governor_name(self):
        return self.spec['governor']['name']

    @property
    def governor_reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = 'AnarchyGovernor',
            name = self.governor_name,
            namespace = self.namespace,
        )

    @property
    def labels(self):
        return self.metadata.get('labels', {})

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            name = self.name,
            namespace = self.namespace,
            uid = self.uid,
        )

    @property
    def result_status(self):
        return self.status.get('result', {}).get('status')

    @property
    def result_status_message(self):
        return self.status.get('result', {}).get('statusMessage')

    @property
    def retry_after(self):
        return self.status.get('retryAfter')

    @property
    def retry_after_datetime(self):
        retry_after = self.retry_after
        if retry_after:
            return datetime.strptime(retry_after, '%Y-%m-%dT%H:%M:%SZ')
        else:
            return datetime.utcnow()

    @property
    def run_post_datetime(self):
        timestamp = self.run_post_timestamp
        if timestamp:
            return datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')

    @property
    def run_post_timestamp(self):
        return self.status.get('runPostTimestamp')

    @property
    def runner_name(self):
        ref = self.runner_reference
        if ref:
            return ref['name']

    @property
    def runner_reference(self):
        return self.status.get('runner')

    @property
    def runner_pod_name(self):
        ref = self.runner_pod_reference
        if ref:
            return ref['name']

    @property
    def runner_pod_reference(self):
        return self.status.get('runnerPod', {})

    @property
    def subject_name(self):
        return self.spec['subject']['name']

    @property
    def subject_reference(self):
        return dict(
            apiVersion = self.spec['subject']['apiVersion'],
            kind = self.spec['subject']['kind'],
            name = self.spec['subject']['name'],
            namespace = self.spec['subject']['namespace'],
            uid = self.spec['subject']['uid'],
        )

    @property
    def uid(self):
        return self.metadata['uid']

    def add_to_action_status(self, anarchy_runtime):
        try:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.action_namespace, 'anarchyactions', self.action_name,
                {
                    'status': {
                        'runRef': self.reference,
                        'runScheduled': self.creation_timestamp,
                    }
                }
            )
            action = AnarchyAction.get_from_cache(self.action_name)
            if action:
                action.__init__(resource_object)
                return action
            else:
                return AnarchyAction(resource_object)
        except kubernetes.client.rest.ApiException as e:
            # If error is 404, not found, then subject or action must have been deleted
            if e.status != 404:
                raise

    def delete(self, anarchy_runtime):
        try:
            anarchy_runtime.custom_objects_api.delete_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyruns', self.name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def get_governor(self):
        return AnarchyGovernor.get(self.governor_name)

    def get_runner(self):
        name = self.runner_name
        return AnarchyRunner.get(name) if name else None

    def get_runner_label_value(self, anarchy_runtime):
        return self.metadata.get('labels', {}).get(anarchy_runtime.runner_label, None)

    def get_subject(self, anarchy_runtime):
        return AnarchySubject.get(
            anarchy_runtime = anarchy_runtime,
            name = self.subject_name,
        )

    def handle_lost_runner(self, anarchy_runtime):
        """Notified that a runner has been lost, reset AnarchyRun to pending"""
        self.local_logger.warning(
            'AnarchyRun lost runner pod',
            extra = dict(runnerPod = self.runner_pod_reference)
        )
        self.post_result(
            anarchy_runtime = anarchy_runtime,
            result = {'status': 'lost'},
        )

    def manage(self, anarchy_runtime):
        runner_label_value = self.get_runner_label_value(anarchy_runtime)
        if runner_label_value == 'pending':
            pass
        elif runner_label_value == 'queued':
            pass
        elif runner_label_value == 'failed':
            if self.retry_after_datetime <= datetime.utcnow():
                self.set_to_pending(anarchy_runtime)
        elif self.runner_pod_name:
            runner = self.get_runner()
            if runner:
                if runner.get_pod(self.runner_pod_name):
                    pass # FIXME - Timeout?
                else:
                    self.handle_lost_runner(anarchy_runtime)
            else:
                self.local_logger.warning(
                    'Unable to find AnarchyRunner',
                    extra = dict(
                        runner = dict(
                            apiVersion = anarchy_runtime.api_group_version,
                            kind = 'AnarchyRunner',
                            name = runner_name,
                            namespace = anarchy_runtime.operator_namespace,
                        )
                    )
                )

    def post_result(self, result, anarchy_runtime):
        self.local_logger.info(
            'Post result for AnarchyRun',
            extra = dict(
                status = result['status']
            )
        )

        patch = {
            "metadata": {
                "labels": {
                    anarchy_runtime.runner_label: 'pending' if result['status'] == 'lost' else result['status']
                }
            }
        }

        status_patch = {
            'result': result,
            'runPostTimestamp': datetime.utcnow().strftime('%FT%TZ'),
        }

        if result['status'] == 'successful':
            patch['metadata']['labels'][anarchy_runtime.finished_label] = 'true'
        elif result['status'] == 'failed':
            if self.failures > 8:
                retry_delay = timedelta(minutes=30)
            else:
                retry_delay = timedelta(seconds=5 * 2**self.failures)
            status_patch['failures'] = self.failures + 1
            status_patch['retryAfter'] = (datetime.utcnow() + retry_delay).strftime('%FT%TZ')

        try:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyruns', self.name, {"status": status_patch}
            )
            self.__init__(resource_object)
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyruns', self.name, patch
            )
            self.__init__(resource_object)
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                self.local_logger.warning('Unable to updated deleted AnarchyRun')
            else:
                raise

    def set_runner_pod(self, runner, runner_pod, anarchy_runtime):
        runner_pod_reference = dict(
            apiVersion = 'v1',
            kind = 'Pod',
            name = runner_pod.metadata.name,
            namespace = runner_pod.metadata.namespace,
            uid = runner_pod.metadata.uid,
        )

        resource_object = self.to_dict()
        resource_object['metadata']['labels'][anarchy_runtime.runner_label] = runner_pod.metadata.name

        try:
            resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyruns', self.name, resource_object
            )
            self.__init__(resource_object=resource_object)
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyruns', self.name,
                {
                    "status": {
                        "runnerPod": runner_pod_reference,
                        "runner": runner.reference,
                    }
                }
            )
            self.__init__(resource_object=resource_object)

            self.local_logger.info(
                'Set runner',
                extra = dict(
                    runner = runner.reference,
                    runner_pod = runner_pod_reference,
                )
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 409:
                # Failed to set runner due to conflict
                return False
            else:
                raise
        return True

    def set_to_pending(self, anarchy_runtime):
        resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            self.namespace, 'anarchyruns', self.name,
            {
                "status": {
                    "runnerPod": None,
                    "runner": None,
                }
            }
        )
        self.__init__(resource_object)
        resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            self.namespace, 'anarchyruns', self.name,
            {
                "metadata": {
                    "labels": {
                        anarchy_runtime.runner_label: 'pending'
                    }
                },
            }
        )
        self.__init__(resource_object)

    def to_dict(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            metadata = copy.deepcopy(self.metadata),
            spec = copy.deepcopy(self.spec),
            status = copy.deepcopy(self.status),
        )
