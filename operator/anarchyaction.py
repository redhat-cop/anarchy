import copy
import kubernetes
import logging
import os
import time
import kopf

from datetime import datetime, timedelta

from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject

class AnarchyAction(object):
    """
    AnarchyAction class
    """

    @staticmethod
    def get(name, anarchy_runtime):
        resource = AnarchyAction.get_resource_from_api(name, anarchy_runtime)
        if resource:
            subject = AnarchyAction(resource)
            return subject
        else:
            return None

    @staticmethod
    def get_resource_from_api(name, anarchy_runtime):
        try:
            return anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def __init__(self, resource, logger=None):
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = kopf.LocalObjectLogger(
                body = resource,
                settings = kopf.OperatorSettings(),
            )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status')

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
    def finished_timestamp(self):
        if self.status:
            return self.status.get('finishedTimestamp')

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
    def has_run_scheduled(self):
        return True if self.status and 'runScheduled' in self.status else False

    @property
    def is_finished(self):
        return True if self.status and 'finishedTimestamp' in self.status else False

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

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def add_run_to_status(self, anarchy_run, anarchy_runtime):
        try:
            anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
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
            resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.run_label: anarchy_run['metadata']['name']
                        }
                    }
                }
            )
            self.__init__(resource)
        except kubernetes.client.rest.ApiException as e:
            # If error is 404, not found, then subject or action must have been deleted
            if e.status != 404:
                raise

    def check_callback_token(self, authorization_header):
        if not authorization_header.startswith('Bearer '):
            return false
        return self.callback_token == authorization_header[7:]

    def get_governor(self, anarchy_runtime):
        return AnarchyGovernor.get(self.governor_name)

    def get_subject(self, anarchy_runtime):
        return AnarchySubject.get(self.subject_name, anarchy_runtime)

    def governor_ref(self, anarchy_runtime):
        return dict(
            apiVersion = anarchy_runtime.api_group_version,
            kind = 'AnarchyGovernor',
            name = self.governor_name,
            namespace = self.namespace,
        )

    def delete(self, anarchy_runtime):
        try:
            anarchy_runtime.custom_objects_api.deleted_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, self.namespace, 'anarchyactions', self.name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def governor_ref(self, anarchy_runtime):
        return dict(
            apiVersion = anarchy_runtime.api_group_version,
            kind = 'AnarchyGovernor',
            name = self.governor_name,
            namespace = self.namespace,
        )

    def process_callback(self, anarchy_runtime, callback_name, callback_data):
        subject = self.get_subject(anarchy_runtime)
        if not subject:
            self.logger.info(
                'Received callback but cannot find AnarchySubject',
                extra = dict(
                    subject = self.subject_ref(anarchy_runtime),
                )
            )
            return

        governor = subject.get_governor(anarchy_runtime)
        if not governor:
            self.logger.warning(
                'Received callback but cannot find AnarchyGovernor',
                extra = dict(
                    governor = self.governor_ref(anarchy_runtime),
                )
            )
            return

        action_config = governor.action_config(self.action)
        if not action_config:
            self.logger.warning(
                'Received callback for action which is not defined in governor',
                extra = dict(
                    action = self.action,
                    governor = self.governor_ref(anarchy_runtime),
                )
            )
            return
        if not callback_name:
            name_parameter = action_config.callback_name_parameter or governor.callback_name_parameter
            callback_name = callback_data.get(name_parameter, None)
            if not callback_name:
                self.logger.warning(
                    'Name parameter not set in callback data, cannot process callback',
                    extra = dict(
                        nameParameter = name_parameter
                    )
                )
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
                resource = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version, self.namespace, 'anarchyactions', self.name, self.to_dict(anarchy_runtime)
                )
                self.__init__(resource)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(anarchy_runtime)
                else:
                    raise

        handler = action_config.callback_handler(callback_name)
        if not handler:
            self.logger.warning(
                'Callback handler not found',
                extra = dict(
                    action = action_config.name,
                    callback = callback_name,
                )
            )
            return

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config),
            ('action', self),
            ('handler', handler),
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name': callback_name,
            'anarchy_action_callback_data': callback_data,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': anarchy_runtime.action_callback_url(self.name)
        }

        governor.run_ansible(anarchy_runtime, handler, run_vars, context, subject, self, callback_name)

    def refresh_from_api(self, anarchy_runtime):
        resource = AnarchyAction.get_resource_from_api(self.name, anarchy_runtime)
        if resource:
            self.__init__(resource)
        else:
            raise Exception('Unable to find AnarchyAction {} to refresh'.format(self.name))

    def schedule_continuation(self, args, anarchy_runtime):
        after_seconds = args.get('after', 0)
        try:
            resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'status': {
                        'runScheduled': None
                    }
                }
            )
            anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.run_label: None
                        }
                    },
                    'spec': {
                        'after': (datetime.utcnow() + timedelta(seconds=after_seconds)).strftime('%FT%TZ'),
                    },
                }
            )
            self.__init__(resource)
        except kubernetes.client.rest.ApiException as e:
            # If error is 404, not found, then subject or action must have been deleted
            if e.status != 404:
                raise

    def set_finished(self, state, anarchy_runtime):
        try:
            resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.finished_label: state
                        }
                    }
                }
            )
            self.__init__(resource)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

        try:
            resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'status': {
                        'finishedTimestamp': datetime.utcnow().strftime('%FT%TZ'),
                        'state': state
                    }
                }
            )
            self.__init__(resource)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def set_owner(self, anarchy_runtime):
        '''
        Anarchy when using on.event when an anarchyaction is added and modified
        we will verify that an ownerReferences exist and created it.
        '''
        subject = self.get_subject(anarchy_runtime)
        if not subject:
            raise kopf.TemporaryError('Cannot find subject of the action "%s"', self.action)
        governor = subject.get_governor(anarchy_runtime)
        if not governor:
            raise kopf.TemporaryError('Cannot find governor of the action "%s"', self.action)
        resource = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyactions',
            self.name,
            {
                'metadata': {
                    'labels': {
                        anarchy_runtime.action_label: self.action,
                        anarchy_runtime.governor_label: governor.name,
                        anarchy_runtime.subject_label: subject.name,
                    },
                    'ownerReferences': [{
                        'apiVersion': anarchy_runtime.api_group_version,
                        'controller': True,
                        'kind': 'AnarchySubject',
                        'name': subject.name,
                        'uid': subject.uid
                    }]
                },
                'spec': {
                    'governorRef': {
                        'apiVersion': anarchy_runtime.api_group_version,
                        'kind': 'AnarchyGovernor',
                        'name': governor.name,
                        'namespace': governor.namespace,
                        'uid': governor.uid
                    },
                    'subjectRef': {
                        'apiVersion': anarchy_runtime.api_group_version,
                        'kind': 'AnarchySubject',
                        'name': subject.name,
                        'namespace': subject.namespace,
                        'uid': subject.uid
                    }
                }
            }
        )
        self.__init__(resource)

    def start(self, anarchy_runtime):
        subject = self.get_subject(anarchy_runtime)

        # Subject may have been deleted, abort run of the action in this case
        if not subject:
            self.logger.info(
                'Not starting AnarchyAction for deleted AnarchySubject',
                extra = dict(
                    subject = self.subject_ref(anarchy_runtime),
                )
            )
            return

        # Attempt to set active action for subject
        if not subject.set_active_action(self, anarchy_runtime):
            return

        governor = subject.get_governor(anarchy_runtime)

        action_config = governor.action_config(self.action)
        if not action_config:
            self.logger.warning(
                'Attempt to start action which is not defined in governor',
                extra = dict(
                    action = self.action,
                    governor = self.governor_ref(anarchy_runtime),
                )
            )
            return

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config),
            ('action', self),
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': anarchy_runtime.action_callback_url(self.name)
        }

        governor.run_ansible(anarchy_runtime, action_config, run_vars, context, subject, self)
        return True

    def subject_ref(self, anarchy_runtime):
        return dict(
            apiVersion = anarchy_runtime.api_group_version,
            kind = 'AnarchySubject',
            name = self.subject_name,
            namespace = self.namespace,
        )

    def to_dict(self, anarchy_runtime):
        return dict(
            apiVersion = anarchy_runtime.api_group_version,
            kind = 'AnarchyAction',
            metadata = self.metadata,
            spec = self.spec,
            status = self.status
        )
