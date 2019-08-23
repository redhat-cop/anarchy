from datetime import datetime
import logging
import os
import threading
import uuid

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchySubject(object):
    """AnarchySubject class"""

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.is_new = 'status' not in resource
        self.is_updated = False
        self.sanity_check()

    def sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def namespace_name(self):
        return self.metadata['namespace'] + '/' + self.metadata['name']

    @property
    def delete_started(self):
        return 'deleteHandlersStarted' in self.status

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def add_finalizer(self, runtime, logger):
        finalizers = self.metadata.get('finalizers', [])
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': finalizers + [runtime.operator_domain] } }
        )

    def get_governor(self, runtime):
        governor = AnarchyGovernor.get(self.spec['governor'])
        if not governor:
            logger.error('Unable to find governor %s', self.governor_name)
        return governor

    def handle_create(self, runtime, logger):
        self.add_finalizer(runtime, logger)
        self.process_subject_event_handlers(runtime, 'create', logger)

    def handle_delete(self, runtime, logger):
        if self.delete_started:
            return
        self.record_delete_started(runtime, logger)
        event_handled = self.process_subject_event_handlers(runtime, 'delete', logger)
        if not event_handled:
            self.remove_finalizer(runtime, logger)

    def handle_update(self, runtime, logger):
        self.process_subject_event_handlers(runtime, 'update', logger)

    def process_action_event_handlers(self, runtime, action, event_data, event_name):
        governor = self.get_governor(runtime)
        if governor:
            governor.process_action_event_handlers(runtime, self, action, event_data, event_name)

    def process_subject_event_handlers(self, runtime, event_name, logger):
        governor = self.get_governor(runtime)
        if governor:
            return governor.process_subject_event_handlers(runtime, self, event_name, logger)

    def record_delete_started(self, runtime, logger):
        runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def remove_finalizer(self, runtime, logger):
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': None } }
        )

    def start_action(self, runtime, action):
        governor = self.get_governor(runtime)
        if governor:
            return governor.start_action(runtime, self, action)
