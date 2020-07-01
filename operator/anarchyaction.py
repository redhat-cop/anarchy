from datetime import datetime
import copy
import kubernetes
import logging
import os
import time
import kopf

from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject

operator_logger = logging.getLogger('operator')

class AnarchyAction(object):
    """
    AnarchyAction class
    """

    # Cache of active AnarchyActions
    cache = {}

    @staticmethod
    def cache_put(action):
        AnarchyAction.cache[action.name] = action

    @staticmethod
    def cache_remove(action):
        AnarchyAction.cache.pop(action.name if isinstance(action, AnarchyAction) else action, None)

    @staticmethod
    def cache_update(resource):
        """Update action in cache if present in cache"""
        resource_meta = resource['metadata']
        action_name = resource_meta['name']
        action = AnarchyAction.cache.get(action_name, None)
        if action:
            action.metadata = resource_meta
            action.spec = resource['spec']
            action.status = resource.get('status', None)
            return action

    @staticmethod
    def get(name, runtime):
        operator_logger.debug('Getting AnarchyAction %s', name)
        resource = AnarchyAction.get_resource_from_api(name, runtime)
        if resource:
            subject = AnarchyAction(resource)
            return subject
        else:
            return None

    @staticmethod
    def get_resource_from_api(name, runtime):
        try:
            return runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def start_actions(runtime):
        for action in list(AnarchyAction.cache.values()):
            if not action.has_started \
            and action.after_datetime <= datetime.utcnow():
                try:
                    action.start(runtime)
                except Exception as e:
                    operator_logger.exception("Error running action %s", action.name)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.__sanity_check()

    def __sanity_check(self):
        # FIXME
        pass

    @property
    def action(self):
        return self.spec['action']

    @property
    def after(self):
        return self.spec.get('after', '')

    @property
    def after_datetime(self):
        return datetime.strptime(
            self.spec['after'], '%Y-%m-%dT%H:%M:%SZ'
        ) if 'after' in self.spec else datetime.utcnow()

    @property
    def callback_token(self):
        return self.spec.get('callbackToken', '')

    @property
    def governor(self):
        return AnarchyGovernor.get(self.spec['governorRef']['name'])

    @property
    def governor_name(self):
        return self.spec['governorRef']['name']

    @property
    def has_owner(self):
        return True if self.metadata.get('ownerReferences')else False

    @property
    def has_started(self):
        return True if self.status and 'runScheduled' in self.status else False

    @property
    def kind(self):
        return 'AnarchyAction'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def subject_name(self):
        return self.spec['subjectRef']['name']

    @property
    def uid(self):
        return self.metadata['uid']

    def add_run_to_status(self, anarchy_run, runtime):
        try:
            runtime.custom_objects_api.patch_namespaced_custom_object_status(
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'status': {
                        'runRef': {
                            'apiVersion': anarchy_run['apiVersion'],
                            'kind': anarchy_run['kind'],
                            'name': anarchy_run['metadata']['name'],
                            'namespace': anarchy_run['metadata']['namespace'],
                            'uid': anarchy_run['metadata']['uid']
                        },
                        'runScheduled': anarchy_run['metadata']['creationTimestamp']
                    }
                }
            )
            resource = runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            runtime.run_label: anarchy_run['metadata']['name']
                        }
                    }
                }
            )
            self.refresh_from_resource(resource)
        except kubernetes.client.rest.ApiException as e:
            # If error is 404, not found, then subject or action must have been deleted
            if e.status != 404:
                raise

    def check_callback_token(self, authorization_header):
        if not authorization_header.startswith('Bearer '):
            return false
        return self.callback_token == authorization_header[7:]

    def get_subject(self, runtime):
        return AnarchySubject.get(self.subject_name, runtime)

    def process_callback(self, runtime, callback_name, callback_data):
        subject = self.get_subject(runtime)
        governor = subject.get_governor(runtime)
        if not governor:
            operator_logger.warning('Received callback for subject "%s", but cannot find AnarchyGovernor %s', subject.name, governor.name)
            return
        action_config = governor.actions.get(self.action, None)

        if not action_config:
            operator_logger.warning('Received callback for action, "%s", which is not defined in AnarchyGovernor %s', self.action, governor.name)
            return
        if not callback_name:
            name_parameter = action_config.callback_name_parameter or governor.callback_name_parameter
            callback_name = callback_data.get(name_parameter, None)
            if not callback_name:
                operator_logger.warning('Name parameter, "%s", not set in callback data', name_parameter)
                return

        while True:
            if not self.status:
                self.status = {}
            if not 'callbackEvents' in self.status:
                self.status['callbackEvents'] = []

            self.status['callbackEvents'].append({
                'name': callback_name,
                'data': callback_data,
                'timestamp': datetime.utcnow().strftime('%FT%TZ')
            })

            try:
                resource = runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    runtime.operator_domain, 'v1', self.namespace, 'anarchyactions', self.name, self.to_dict(runtime)
                )
                self.refresh_from_resource(resource)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(runtime)
                else:
                    raise

        handler = action_config.callback_handlers.get(callback_name, None)
        if not handler:
            operator_logger.warning('No callback handler in %s for %s', action_config.name, callback_name)
            return

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config),
            ('handler', handler)
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name': callback_name,
            'anarchy_action_callback_data': callback_data,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': runtime.action_callback_url(self.name)
        }

        governor.run_ansible(runtime, handler, run_vars, context, subject, self, callback_name)

    def refresh_from_api(self, runtime):
        resource = AnarchyAction.get_resource_from_api(self.name, runtime)
        if resource:
            self.refresh_from_resource(resource)
        else:
            raise Exception('Unable to find AnarchyAction {} to refresh'.format(self.name))

    def refresh_from_resource(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status')

    def set_owner(self, runtime):
        '''
        Anarchy when using on.event when an anarchyaction is added and modified
        we will verify that an ownerReferences exist and created it.
        '''
        subject = self.get_subject(runtime)
        if not subject:
            raise kopf.TemporaryError('Cannot find subject of the action "%s"', self.action)
        governor = subject.get_governor(runtime)
        if not governor:
            raise kopf.TemporaryError('Cannot find governor of the action "%s"', self.action)
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions',
            self.name,
            {
                'metadata': {
                    'labels': {
                        runtime.action_label: self.action,
                        runtime.governor_label: governor.name,
                        runtime.subject_label: subject.name,
                    },
                    'ownerReferences': [{
                        'apiVersion': runtime.operator_domain + '/v1',
                        'controller': True,
                        'kind': 'AnarchySubject',
                        'name': subject.name,
                        'uid': subject.uid
                    }]
                },
                'spec': {
                    'governorRef': {
                        'apiVersion': runtime.operator_domain + '/v1',
                        'kind': 'AnarchyGovernor',
                        'name': governor.name,
                        'namespace': governor.namespace,
                        'uid': governor.uid
                    },
                    'subjectRef': {
                        'apiVersion': runtime.operator_domain + '/v1',
                        'kind': 'AnarchySubject',
                        'name': subject.name,
                        'namespace': subject.namespace,
                        'uid': subject.uid
                    }
                }
            }
        )

    def start(self, runtime):
        AnarchyAction.cache_remove(self)
        subject = self.get_subject(runtime)
        governor = subject.get_governor(runtime)

        action_config = governor.actions.get(self.action, None)
        if not action_config:
            operator_logger.warning('Attempting to start action, "%s", which is not defined in AnarchyGovernor %s', self.action, governor.name)
            return

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config)
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': runtime.action_callback_url(self.name)
        }

        governor.run_ansible(runtime, action_config, run_vars, context, subject, self)
        return True

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.operator_domain + '/v1',
            kind = 'AnarchyAction',
            metadata = self.metadata,
            spec = self.spec,
            status = self.status
        )
