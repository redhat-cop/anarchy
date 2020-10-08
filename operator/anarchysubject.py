from datetime import datetime
from anarchyutil import k8s_ref
import kubernetes
import logging
import os
import threading
import time
import uuid

from anarchygovernor import AnarchyGovernor

operator_logger = logging.getLogger('operator')

class AnarchySubject(object):
    """AnarchySubject class"""

    @staticmethod
    def get(name, runtime):
        operator_logger.debug('Getting AnarchySubject %s', name)
        resource = AnarchySubject.get_resource_from_api(name, runtime)
        if resource:
            subject = AnarchySubject(resource)
            return subject
        else:
            return None

    @staticmethod
    def get_resource_from_api(name, runtime):
        try:
            return runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.__sanity_check()

    def __sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    @property
    def active_run_name(self):
        ref = self.active_run_ref
        if ref:
            return ref['name']
        else:
            return None

    @property
    def active_run_ref(self):
        if not self.status:
            return None
        active_runs = self.status.get('runs', {}).get('active', [])
        if active_runs:
            return active_runs[0]
        else:
            return None

    @property
    def delete_started(self):
        return self.status and 'deleteHandlersStarted' in self.status

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

    @property
    def kind(self):
        return 'AnarchySubject'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def parameter_secrets(self):
        return self.spec.get('parameterSecrets', [])

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def add_finalizer(self, runtime):
        finalizers = self.metadata.get('finalizers', [])
        if runtime.operator_domain not in finalizers:
            resource = runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects', self.name,
                {'metadata': {'finalizers': finalizers + [runtime.operator_domain] } }
            )
            self.refresh_from_resource(resource)

    def add_run_to_status(self, anarchy_run, runtime):
        '''
        Add AnarchyRun to AnarchySubject status.

        The list of active runs in the status is used to set runs from queued to pending when they reach the
        top of the list.
        '''
        while True:
            anarchy_subject = self.to_dict(runtime)
            if not anarchy_subject['status']:
                anarchy_subject['status'] = {}
            if not 'runs' in anarchy_subject['status']:
                anarchy_subject['status']['runs'] = {
                    'active': []
                }
            anarchy_subject['status']['runs']['active'].append(k8s_ref(anarchy_run))
            try:
                resource = runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    runtime.operator_domain, runtime.api_version, self.namespace, 'anarchysubjects', self.name, anarchy_subject
                )
                self.refresh_from_resource(resource)
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    if not self.refresh_from_api(runtime):
                        operator_logger.error('Cannot add run to status, unable to refresh AnarchySubject %s', self.subject_name)
                        return
                else:
                    raise

    def delete(self, remove_finalizers, runtime):
        result = runtime.custom_objects_api.delete_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects', self.name
        )
        if remove_finalizers:
            self.remove_finalizers(runtime)
        return result

    def get_governor(self, runtime):
        governor = AnarchyGovernor.get(self.spec['governor'])
        if not governor:
            operator_logger.error('Unable to find governor %s', self.governor_name)
        return governor

    def get_anarchy_run(self, runtime):
        return self.anarchy_runs.get(self.current_anarchy_run, None)

    def handle_create(self, runtime):
        self.add_finalizer(runtime)
        self.process_subject_event_handlers(runtime, 'create')

    def handle_delete(self, runtime):
        '''
        Handle delete if delete process has not started. If there is a delete
        subject event handler then an AnarchyRun will be created to process the
        delete, otherwise the finalizers are removed immediately.
        '''
        if self.delete_started:
            return
        self.record_delete_started(runtime)
        event_handled = self.process_subject_event_handlers(runtime, 'delete')
        if not event_handled:
            self.remove_finalizers(runtime)

    def handle_spec_update(self, runtime):
        '''
        Handle update to AnarchySubject spec.
        '''
        self.process_subject_event_handlers(runtime, 'update')

    def remove_active_run_from_status(self, anarchy_run, runtime):
        # Support passing run by object or name
        run_name = anarchy_run.name if hasattr(anarchy_run, 'name') else anarchy_run
        first_attempt = True
        while True:
            anarchy_subject = self.to_dict(runtime)
            if not anarchy_subject['status']:
                anarchy_subject['status'] = {}
            if not 'runs' in anarchy_subject['status']:
                anarchy_subject['status']['runs'] = {
                    'active': []
                }
            status_runs_active = anarchy_subject['status']['runs']['active']
            run_ref = None
            for i in range(len(status_runs_active)):
                if status_runs_active[i]['name'] == run_name:
                    run_ref = status_runs_active.pop(i)
                    if i != 0:
                        operator_logger.warning(
                            'Removing AnarchyRun %s in AnarchySubject %s, but it was not listed first!',
                            run_name, self.name
                        )
                    break
            if not run_ref:
                if first_attempt:
                    operator_logger.warning(
                        'Attempt to remove AnarchyRun %s in AnarchySubject %s status when not listed in active!',
                        run_name, self.name
                    )
                return
            try:
                resource = runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    runtime.operator_domain, runtime.api_version, self.namespace, 'anarchysubjects', self.name, anarchy_subject
                )
                self.refresh_from_resource(resource)
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    first_attempt = False
                    if not self.refresh_from_api(runtime):
                        operator_logger.info('Cannot remove active run from status, unable to refresh AnarchySubject %s', self.subject_name)
                        return
                else:
                    raise

    def patch(self, patch, runtime):
        '''
        Patch AnarchySubject resource and status.
        '''
        resource_patch = {}
        result = None
        if 'metadata' in patch:
            resource_patch['metadata'] = patch['metadata']
        if 'spec' in patch:
            resource_patch['spec'] = patch['spec']
        if resource_patch:
            result = runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace,
                'anarchysubjects', self.name, resource_patch
            )
        if 'status' in patch:
            result = runtime.custom_objects_api.patch_namespaced_custom_object_status(
                runtime.operator_domain, runtime.api_version, runtime.operator_namespace,
                'anarchysubjects', self.name, {'status': patch['status']}
            )
        return result

    def process_subject_event_handlers(self, runtime, event_name):
        governor = self.get_governor(runtime)
        if not governor:
            operator_logger.warning(
                'Received "%s" event for subject "%s", but cannot find AnarchyGovernor %s',
                event_name, self.name, self.governor_name
            )
            return

        handler = governor.subject_event_handler(event_name)
        if not handler:
            return

        context = (
            ('governor', governor),
            ('subject', self),
            ('handler', handler)
        )
        run_vars = {
            'anarchy_event_name': event_name
        }

        governor.run_ansible(runtime, handler, run_vars, context, self, None, event_name)
        return True

    def record_delete_started(self, runtime):
        runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def refresh_from_api(self, runtime):
        resource = AnarchySubject.get_resource_from_api(self.name, runtime)
        if resource:
            self.refresh_from_resource(resource)
            return True
        else:
            return False

    def refresh_from_resource(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status')

    def remove_active_run(self, run_ref, runtime):
        while True:
            anarchy_subject = self.to_dict(runtime)
            if not anarchy_subject['status']:
                anarchy_subject['status'] = {}
            if not 'runs' in anarchy_subject['status']:
                anarchy_subject['status']['runs'] = {
                    'active': []
                }
            status_runs_active = anarchy_subject['status']['runs']['active']
            found_run = False
            for i in range(len(status_runs_active)):
                if status_runs_active[i]['name'] == run_ref['name']:
                    found_run = True
                    status_runs_active.pop(i)
                    break
            if not found_run:
                return
            try:
                resource = runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    runtime.operator_domain, runtime.api_version, self.namespace, 'anarchysubjects', self.name, anarchy_subject
                )
                self.refresh_from_resource(resource)
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(runtime)
                else:
                    raise

    def remove_finalizers(self, runtime):
        return runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': None } }
        )

    def set_active_run_to_pending(self, runtime):
        '''
        Set next AnarchyRun to pending
        '''
        while True:
            run_ref = self.active_run_ref
            if not run_ref:
                return
            run_name = run_ref['name']
            run_namespace = run_ref['namespace']
            try:
                operator_logger.info('Setting AnarchyRun %s to pending for AnarchySubject %s', run_name, self.name)
                runtime.custom_objects_api.patch_namespaced_custom_object(
                    runtime.operator_domain, runtime.api_version, run_namespace, 'anarchyruns', run_name,
                    {'metadata': {'labels': { runtime.runner_label: 'pending' } } }
                )
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 404:
                    operator_logger.warning(
                        'AnarchyRun %s for AnarchySubject %s was deleted before execution',
                        run_name, self.name
                    )
                    self.remove_active_run(run_ref, runtime)
                else:
                    raise

    def set_active_run_to_pending_from_status(self, runtime):
        '''
        Set next AnarchyRun to pending from status
        '''
        while True:
            run_ref = self.active_run_ref
            if not run_ref:
                return

            run_name = run_ref['name']
            run_namespace = run_ref['namespace']
            try:
                run = runtime.custom_objects_api.get_namespaced_custom_object(
                    runtime.operator_domain, runtime.api_version, run_namespace, 'anarchyruns', run_name
                )
                runner_label_value = run['metadata']['labels'][runtime.runner_label]

                if runner_label_value == 'queued':
                    operator_logger.info('Setting AnarchyRun %s to pending for AnarchySubject %s', run_name, self.name)
                    runtime.custom_objects_api.patch_namespaced_custom_object(
                        runtime.operator_domain, runtime.api_version, run_namespace, 'anarchyruns', run_name,
                        {'metadata': {'labels': { runtime.runner_label: 'pending' } } }
                    )
                    return
                elif runner_label_value == 'successful':
                    self.remove_active_run_from_status(run_name, runtime)

            except kubernetes.client.rest.ApiException as e:
                if e.status == 404:
                    operator_logger.warning(
                        'AnarchyRun %s for AnarchySubject %s was deleted before execution',
                        run_name, self.name
                    )
                    self.remove_active_run(run_ref, runtime)
                    self.set_active_run_to_pending(runtime)
                else:
                    raise

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.api_group_version,
            kind = 'AnarchySubject',
            metadata = self.metadata,
            spec = self.spec,
            status = self.status
        )
