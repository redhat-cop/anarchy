import copy
import jinja2
import json
import logging
import os
import random
import six

from anarchyapi import AnarchyAPI

operator_logger = logging.getLogger('operator')

jinja2env = jinja2.Environment(
    block_start_string='{%:',
    block_end_string=':%}',
    comment_start_string='{#:',
    comment_end_string=':#}',
    variable_start_string='{{:',
    variable_end_string=':}}'
)

def recursive_dict_update(a, b):
    for k, v in b.items():
        if k in a \
        and isinstance(a[k], dict) \
        and isinstance(v, dict):
            recursive_dict_update(a[k], v)
        else:
            a[k] = v
    return a

jinja2env.filters['combine'] = lambda a, b: recursive_dict_update(copy.deepcopy(a), b)
jinja2env.filters['to_json'] = lambda x: json.dumps(x, separators=(',', ':'))

def add_secret_values(parameters, runtime, secret_refs):
    for secret in secret_refs:
        if isinstance(secret, dict):
            name = secret['name']
            namespace = secret.get('namespace', None)
        else:
            name = secret
            namespace = None
        parameters.update(runtime.get_secret_data(name, namespace))

def add_values(parameters, runtime, add):
    secrets = {}
    for name, value in add.items():
        if isinstance(value, dict) \
        and 'secretName' in value \
        and 'secretKey' in value:
            secret_name = value['secretName']
            secret_key = value['secretKey']
            secret = secrets.get(secret_name, None)
            if secret == None:
                secret = runtime.get_secret_data(secret_name)
            assert secret_key in secret, \
                'data key {} not found in secret {}'.format(secret_key, secret_name)
            parameters[name] = secret[secret_key]
        else:
            parameters[name] = value

def jinja2render(template_string, template_vars):
    template = jinja2env.from_string(template_string)
    return template.render(template_vars)

def time_to_seconds(time):
    if isinstance(time, int):
        return time
    if isinstance(time, six.string_types):
        if time.endswith('s'):
            return int(time[:-1])
        elif time.endswith('m'):
            return int(time[:-1]) * 60
        elif time.endswith('h'):
            return int(time[:-1]) * 3600
        elif time.endswith('d'):
            return int(time[:-1]) * 86400
        else:
            int(time)
    else:
        raise Exception("time not int or string")

class AnarchyGovernor(object):
    """AnarchyGovernor class"""

    class EventHandler(object):
        def __init__(self, spec):
            assert 'event' in spec, 'eventHandlers must define event'
            assert 'tasks' in spec, 'eventHandlers must define tasks'
            self.event = spec['event']
            self.tasks = spec['tasks']

        def process(self, runtime, governor, anarchy_subject, anarchy_action, event_data, event_name):
            event_spec = {
                'event': {
                    'name': event_name,
                    'data': event_data,
                    'tasks': self.tasks
                },
                'governor': {
                    'metadata': governor.metadata,
                    'spec': {
                        'parameters': governor.spec.get('parameters', {}),
                        'vars': governor.spec.get('vars', {})
                    }
                },
                'subject': {
                    'metadata': anarchy_subject.metadata,
                    'spec': anarchy_subject.spec,
                    'status': anarchy_subject.status
                }
            }
            labels = {
                runtime.operator_domain + '/event-name': event_name,
                runtime.operator_domain + '/runner': 'pending',
                runtime.operator_domain + '/subject-name': anarchy_subject.name
            }

            if anarchy_action:
                event_spec['action'] = {
                    'metadata': anarchy_action.metadata,
                    'spec': anarchy_action.spec
                }
                generate_name = '{}-{}-'.format(anarchy_action.name, event_name)
                labels[runtime.operator_domain + '/action-name'] = anarchy_action.name
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
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyevents',
                {
                    'apiVersion': runtime.operator_domain + '/v1',
                    'kind': 'AnarchyEvent',
                    'metadata': {
                        'generateName': generate_name,
                        'labels': labels,
                        'namespace': runtime.operator_namespace,
                        'ownerReferences': [owner_ref]
                    },
                    'spec': event_spec
                }
            )

    class RequestConfig(object):
        def __init__(self, spec, action_config, governor):
            self.governor_name = governor.name
            self.spec = spec
            self.status_code_events = spec.get('statusCodeEvents', {})

            if 'data' in spec:
                self.data_template = jinja2env.from_string(spec['data'])
            else:
                self.data_template = None

            self.header_templates = {}
            for header in spec.get('headers', []):
                self.header_templates[header['name']] = jinja2env.from_string(header['value'])

        @property
        def api(self):
            if 'api' in self.spec:
                return AnarchyAPI.get(self.spec['api'])
            else:
                return self.governor.api
        @property
        def governor(self):
            return AnarchyGovernor.cache[self.governor_name]

        @property
        def callback_token_parameter(self):
            return self.spec.get('callbackTokenParameter', self.api.callback_token_parameter)

        @property
        def callback_url_parameter(self):
            return self.spec.get('callbackUrlParameter', self.api.callback_url_parameter)

        @property
        def method(self):
            return self.spec.get('method', self.api.method)

        @property
        def parameters(self):
            return self.spec.get('parameters', {})

        @property
        def parameter_secrets(self):
            return self.spec.get('parameterSecrets', [])

        @property
        def path(self):
            return self.spec.get('path', self.api.path)

        def status_code_event(self, status_code):
            return self.status_code_events.get(str(status_code), None)

        def data(self, jinja2vars):
            if self.data_template:
                return self.data_template.render(jinja2vars)
            elif self.api.data:
                return jinja2render(self.api.data, jinja2vars)
            else:
                # Otherwise data payload is the parameters
                return jinja2vars['parameters']

        def headers(self, jinja2vars):
            headers = {}
            for header in self.api.headers:
                headers[header['name']] = jinja2render(header['value'], jinja2vars)
            for name, template in self.header_templates.items():
                headers[name] = template.render(jinja2vars)
            return headers

    class ActionConfig(object):
        def __init__(self, spec, governor):
            assert 'name' in spec, 'actions must define a name'
            self.governor_name = governor.name
            self.spec = spec

            assert 'request' in spec, 'actions must define request'
            self.request = AnarchyGovernor.RequestConfig(spec['request'], self, governor)

            self.event_handlers = []
            for event_handler_spec in spec.get('eventHandlers', []):
                self.event_handlers.append(
                    AnarchyGovernor.EventHandler(event_handler_spec)
                )

        @property
        def callback_event_name_parameter(self):
            return self.spec.get(
                'callbackEventNameParameter',
                self.governor.callback_event_name_parameter
            )

        @property
        def governor(self):
            return AnarchyGovernor.get(self.governor_name)

        @property
        def name(self):
            return self.spec['name']

        @property
        def vars(self):
            return self.spec.get('vars', {})

    cache = {}

    @classmethod
    def register(_class, resource):
        governor = _class(resource)
        operator_logger.info("Registered governor %s", governor.name)
        AnarchyGovernor.cache[governor.name] = governor
        return governor

    @classmethod
    def unregister(_class, governor):
        governor_name = governor.name if isinstance(governor, AnarchyGovernor) else governor
        try:
            del AnarchyGovernor.cache[governor_name]
        except KeyError:
            pass

    @classmethod
    def get(_class, name):
        return AnarchyGovernor.cache.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))
        self.actions = {}
        for action_spec in self.spec.get('actions', []):
            action_config = AnarchyGovernor.ActionConfig(action_spec, self)
            self.actions[action_config.name] = action_config

    def __set_actions(self):
        actions = {}
        self.actions = actions

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handlers = []
        for event_handler_spec in event_handlers:
            self.subject_event_handlers.append(
                AnarchyGovernor.EventHandler(event_handler_spec)
            )

    def sanity_check(self):
        # FIXME
        pass

    @property
    def api(self):
        return AnarchyAPI.get(self.spec.get('api', None))

    @property
    def callback_event_name_parameter(self):
        return self.spec.get(
            'callbackEventNameParameter',
            self.api.callback_event_name_parameter
        )

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

    def start_action(self, runtime, anarchy_subject, anarchy_action):
        action_name = anarchy_action.action
        action_config = self.action_config(action_name)

        api = action_config.request.api

        parameters = self.get_parameters(runtime, api, anarchy_subject, action_config)
        if action_config.request.callback_url_parameter:
            parameters[action_config.request.callback_url_parameter] = anarchy_action.callback_url
        if action_config.request.callback_token_parameter:
            parameters[action_config.request.callback_token_parameter] = anarchy_action.callback_token

        jinja2vars = {
            'action': anarchy_action,
            'action_config': action_config,
            'governor': self,
            'parameters': parameters,
            'subject': anarchy_subject
        }

        path = jinja2render(action_config.request.path, jinja2vars)

        resp, url = api.call(
            runtime,
            path,
            action_config.request.method,
            headers=action_config.request.headers(jinja2vars),
            data=action_config.request.data(jinja2vars)
        )

        resp_data = None
        try:
            resp_data = resp.json()
        except ValueError as e:
            pass

        anarchy_action.patch_status(runtime, {
            "apiUrl": url,
            "apiMethod": action_config.request.method,
            "apiResponse": {
                "status_code": resp.status_code,
                "text": resp.text,
                "data": resp_data
            },
            "events": []
        })

        event_name = action_config.request.status_code_event(resp.status_code)
        if event_name:
            self.process_action_event_handlers(
                runtime,
                anarchy_subject,
                anarchy_action,
                resp_data,
                event_name,
                action_config
            )

    def process_action_event_handlers(self, runtime, anarchy_subject, anarchy_action, event_data, event_name, action_config=None):
        if action_config == None:
            action_config = self.action_config(anarchy_action.action)
        if event_name == None:
            event_name = event_data[action_config.callback_event_name_parameter]

        anarchy_action.status_event_log(runtime, event_name, event_data)

        self.process_event_handlers(runtime, action_config.event_handlers, anarchy_subject, anarchy_action, event_data, event_name)

    def process_subject_event_handlers(self, runtime, anarchy_subject, event_name):
        return self.process_event_handlers(runtime, self.subject_event_handlers, anarchy_subject, None, {}, event_name)

    def process_event_handlers(self, runtime, event_handlers, anarchy_subject, anarchy_action, event_data, event_name):
        event_handled = False
        for event_handler in event_handlers:
            if event_handler.event == event_name:
                event_handled = True
                event_handler.process(runtime, self, anarchy_subject, anarchy_action, event_data, event_name)
        return event_handled
