import copy
import jinja2
import json
import logging
import os
import random
import six

operator_logger = logging.getLogger('operator')

class AnarchyGovernor(object):
    """AnarchyGovernor class"""

    class EventHandler(object):
        def __init__(self, name, spec):
            self.name = name
            self.spec = spec

        @property
        def tasks(self):
            return self.spec.get('tasks', [])

        @property
        def vars(self):
            return self.spec.get('vars', {})

        @property
        def var_secrets(self):
            return self.spec.get('varSecrets', [])

    class ActionConfig(object):
        def __init__(self, name, spec, governor):
            self.name = name
            self.spec = spec
            self.governor = governor
            self.callback_handlers = {}
            for callback_name, handler_spec in spec.get('callbackHandlers', {}).items():
                self.callback_handlers[callback_name] = AnarchyGovernor.EventHandler(callback_name, handler_spec)

        @property
        def callback_name_parameter(self):
            return self.spec.get('callbackNameParameter', None)

        @property
        def tasks(self):
            return self.spec.get('tasks', [])

        @property
        def vars(self):
            return self.spec.get('vars', {})

        @property
        def var_secrets(self):
            return self.spec.get('varSecrets', [])

    cache = {}

    @staticmethod
    def register(resource):
        governor = AnarchyGovernor(resource)
        operator_logger.info("Registered governor %s", governor.name)
        AnarchyGovernor.cache[governor.name] = governor
        return governor

    @staticmethod
    def unregister(governor):
        governor_name = governor.name if isinstance(governor, AnarchyGovernor) else governor
        try:
            del AnarchyGovernor.cache[governor_name]
        except KeyError:
            pass

    @staticmethod
    def get(name):
        return AnarchyGovernor.cache.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))
        self.actions = {}
        for action_name, action_spec in self.spec.get('actions', {}).items():
            self.actions[action_name] = AnarchyGovernor.ActionConfig(action_name, action_spec, self)

    def __set_actions(self):
        actions = {}
        self.actions = actions

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handlers = {}
        for event_name, handler_spec in event_handlers.items():
            self.subject_event_handlers[event_name] = AnarchyGovernor.EventHandler(event_name, handler_spec)

    def sanity_check(self):
        # FIXME
        pass

    @property
    def api(self):
        return AnarchyAPI.get(self.spec.get('api', None))

    @property
    def callback_name_parameter(self):
        return self.spec.get('callbackNameParameter', 'event')

    @property
    def name(self):
        return self.metadata['name']

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

    def get_parameters(self, runtime, api, anarchy_subject, action_config):
        parameters = {}
        add_values(parameters, runtime, anarchy_subject.parameters)
        add_secret_values(parameters, runtime, anarchy_subject.parameter_secrets)
        add_values(parameters, runtime, api.parameters)
        add_secret_values(parameters, runtime, api.parameter_secrets)
        add_values(parameters, runtime, self.parameters)
        add_secret_values(parameters, runtime, self.parameter_secrets)
        add_values(parameters, runtime, action_config.request.parameters)
        add_secret_values(parameters, runtime, action_config.request.parameter_secrets)
        return parameters

    def action_config(self, name):
        assert name in self.actions, \
            'governor has no action named {}'.format(name)
        return self.actions[name]

    def run_ansible(self, runtime, run_tasks, run_vars, context, anarchy_subject, anarchy_action, event_name=None):
        run_spec = { 'tasks': run_tasks }
        collected_run_vars = {}
        for context_item in context:
            name, obj = context_item
            context_vars = runtime.get_vars(obj)
            context_spec = {
                'name': obj.name,
                'vars': context_vars
            }
            for attr in ('metadata', 'status'):
                if hasattr(obj, attr):
                    context_spec[attr] = getattr(obj, attr)
            if hasattr(obj, 'spec') and name != 'governor':
                context_spec['spec'] = obj.spec
            run_spec[name] = context_spec
            collected_run_vars.update(context_vars)
        collected_run_vars.update(run_vars)
        run_spec['vars'] = collected_run_vars

        labels = {
            runtime.operator_domain + '/subject': anarchy_subject.name,
            runtime.runner_label: 'pending'
        }
        if event_name:
            labels[runtime.operator_domain + '/event'] = event_name

        if anarchy_action:
            run_spec['action'] = {
                'metadata': anarchy_action.metadata,
                'name': anarchy_action.name,
                'spec': anarchy_action.spec
            }
            if event_name:
                generate_name = '{}-{}-'.format(anarchy_action.name, event_name)
            else:
                generate_name = anarchy_action.name + '-'
            labels[runtime.operator_domain + '/action'] = anarchy_action.name
            owner_ref = {
                'apiVersion': runtime.operator_domain + '/v1',
                'controller': True,
                'kind': 'AnarchyAction',
                'name': anarchy_action.name,
                'uid': anarchy_action.uid
            }
        else:
            generate_name = '{}-{}-'.format(anarchy_subject.name, event_name)
            owner_ref = {
                'apiVersion': runtime.operator_domain + '/v1',
                'controller': True,
                'kind': 'AnarchySubject',
                'name': anarchy_subject.name,
                'uid': anarchy_subject.uid
            }

        runtime.custom_objects_api.create_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyruns',
            {
                'apiVersion': runtime.operator_domain + '/v1',
                'kind': 'AnarchyRun',
                'metadata': {
                    'generateName': generate_name,
                    'labels': labels,
                    'namespace': runtime.operator_namespace,
                    'ownerReferences': [owner_ref]
                },
                'spec': run_spec
            }
        )
