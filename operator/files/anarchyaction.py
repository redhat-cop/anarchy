from datetime import datetime
import copy
import kubernetes
import logging
import os
import time

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
    def cache_clean():
        for action_name in list(AnarchyAction.cache.keys()):
            action = AnarchyAction.cache[action_name]
            if time.time() - action.last_active > cache_age_limit \
            and not action.has_started:
                del AnarchyAction.cache[action_name]

    @staticmethod
    def cache_put(action):
        action.last_active = time.time()
        AnarchyAction.cache[action.name] = action

    @staticmethod
    def cache_remove(action):
        AnarchyAction.cache.pop(action.name, None)

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
        # FIXME
        """Get action by name from cache or get resource"""
        action = AnarchyAction.cache.get(name, None)
        if action:
            action.last_active = time.time()
            return action

        try:
            resource = runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchyactions', name
            )
            action = AnarchyAction(resource)
            AnarchyAction.cache_put(action)
            return action
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def start_actions(runtime):
        for action in AnarchyAction.cache.values():
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
    def has_started(self):
        return True if self.status else False

    @property
    def name(self):
        return self.metadata['name']

    @property
    def subject_name(self):
        return self.spec['subjectRef']['name']

    @property
    def uid(self):
        return self.metadata['uid']

    def check_callback_token(self, authorization_header):
        if not authorization_header.startswith('Bearer '):
            return false
        return self.callback_token == authorization_header[7:]

    def get_subject(self, runtime):
        return AnarchySubject.get(self.subject_name, runtime)

    def patch_status(self, runtime, patch):
        resource = runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyactions',
            self.name, {"status": patch}
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

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

        callback_events = self.status.get('callbackEvents', [])
        callback_events.append({
            'name': callback_name,
            'data': callback_data,
            'timestamp': datetime.utcnow().strftime('%FT%TZ')
        })
        self.patch_status(runtime, {'callbackEvents': callback_events})

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

    def start(self, runtime):
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

        self.status = dict(runScheduled=datetime.utcnow().strftime('%FT%TZ'))
        self.patch_status(runtime, self.status)
        governor.run_ansible(runtime, action_config, run_vars, context, subject, self)
        return True
