import copy
import jinja2
import jmespath
import json
import logging
import os
import six

logger = logging.getLogger('anarchy')

from anarchyapi import AnarchyAPI

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
            self.runner_image = spec.get('runner_image', os.environ.get(
                'ANSIBLE_RUNNER_IMAGE', 'docker-registry.default.svc:5000/anarchy-operator/anarchy-ansible-runner:latest'
            ))
            self.service_account = spec.get('service_account', os.environ.get(
                'ANSIBLE_RUNNER_SERVICE_ACCOUNT', 'anarchy-operator'
            ))
            self.cpu_limit = spec.get('cpu_limit', os.environ.get(
                'ANSIBLE_RUNNER_CPU_LIMIT', '1'
            ))
            self.cpu_request = spec.get('cpu_request', os.environ.get(
                'ANSIBLE_RUNNER_CPU_REQUEST', '200m'
            ))
            self.memory_limit = spec.get('memory_limit', os.environ.get(
                'ANSIBLE_RUNNER_MEMORY_LIMIT', '1Gi'
            ))
            self.memory_request = spec.get('memory_request', os.environ.get(
                'ANSIBLE_RUNNER_MEMORY_REQUEST', '250Mi'
            ))

        def process(self, runtime, governor, subject, action, event_data, event_name, logger):
            ansible_vars = {
                "anarchy_governor": {
                    "metadata": governor.metadata,
                    "spec": governor.spec
                },
                "anarchy_subject": {
                    "metadata": subject.metadata,
                    "spec": subject.spec,
                    "status": subject.status
                },
                "event_name": event_name,
                "event_data": event_data
            }
            labels = {
                runtime.operator_domain + '/anarchy-subject-namespace': subject.namespace,
                runtime.operator_domain + '/anarchy-subject-name': subject.name,
                runtime.operator_domain + '/anarchy-subject-namespace': subject.namespace,
                runtime.operator_domain + '/anarchy-event-name': event_name
            }

            if action:
                ansible_vars['anarchy_action'] = {
                    "metadata": action.metadata,
                    "spec": action.spec
                }
                generate_name = "{}-{}-{}-".format(
                    action.namespace,
                    action.name,
                    event_name
                )
                labels.update({
                    runtime.operator_domain + '/anarchy-action-namespace': action.namespace,
                    runtime.operator_domain + '/anarchy-action-name': action.name
                })
                owner_ref = {
                    "apiVersion": "gpte.redhat.com/v1",
                    "controller": True,
                    "kind": "AnarchyAction",
                    "name": action.name,
                    "uid": action.uid
                }
            else:
                generate_name = "{}-{}-{}-".format(
                    subject.namespace,
                    subject.name,
                    event_name
                )
                owner_ref = {
                    "apiVersion": "gpte.redhat.com/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": subject.name,
                    "uid": subject.uid
                }

            runtime.core_v1_api.create_namespaced_pod(
                runtime.operator_namespace,
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "generateName": generate_name,
                        "labels": labels,
                        "namespace": subject.namespace,
                        "ownerReferences": [owner_ref]
                    },
                    "spec": {
                        "containers": [{
                            "name": "ansible",
                            "env": [{
                                "name": "VARS",
                                "value": json.dumps(ansible_vars, separators=(',', ':'))
                            },{
                                "name": "TASKS",
                                "value": json.dumps(self.tasks, separators=(',', ':'))
                            },{
                                "name": "OPERATOR_DOMAIN",
                                "value": runtime.operator_domain
                            },{
                                "name": "POD_NAME",
                                "valueFrom": {
                                    "fieldRef": {
                                        "fieldPath": "metadata.name"
                                    }
                                }
                            }],
                            "image": self.runner_image,
                            "imagePullPolicy": "Always",
                            "resources": {
                                "limits": {
                                    "cpu": self.cpu_limit,
                                    "memory": self.memory_limit
                                },
                                "requests": {
                                    "cpu": self.cpu_request,
                                    "memory": self.memory_request
                                }
                            }
                        }],
                        "restartPolicy": "Never",
                        "serviceAccountName": self.service_account
                    }
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
            return AnarchyGovernor.governors[self.governor_name]

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

    governors = {}

    @classmethod
    def register(_class, resource):
        governor = _class(resource)
        logger.info("Registered governor %s", governor.name)
        AnarchyGovernor.governors[governor.name] = governor
        return governor

    @classmethod
    def unregister(_class, governor):
        if isinstance(governor, AnarchyGovernor):
            del AnarchyGovernor.governors[governor.name]
        else:
            del AnarchyGovernor.governors[governor]

    @classmethod
    def get(_class, name):
        return AnarchyGovernor.governors.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))
        self.actions = {}
        for action_spec in self.spec.get('actions', []):
            action = AnarchyGovernor.ActionConfig(action_spec, self)
            self.actions[action.name] = action

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
    def namespace(self):
        return self.metadata['namespace']

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def uid(self):
        return self.metadata['uid']

    def get_parameters(self, runtime, api, subject, action_config):
        parameters = {}
        add_values(parameters, runtime, subject.parameters)
        add_values(parameters, runtime, api.parameters)
        add_values(parameters, runtime, self.spec.get('parameters', {}))
        add_values(parameters, runtime, action_config.request.parameters)
        return parameters

    def action_config(self, name):
        assert name in self.actions, \
            'governor has no action named {}'.format(name)
        return self.actions[name]

    def start_action(self, runtime, subject, action):
        action_name = action.action
        action_config = self.action_config(action_name)

        api = action_config.request.api

        parameters = self.get_parameters(runtime, api, subject, action_config)
        if action_config.request.callback_url_parameter:
            parameters[action_config.request.callback_url_parameter] = action.callback_url
        if action_config.request.callback_token_parameter:
            parameters[action_config.request.callback_token_parameter] = action.callback_token

        jinja2vars = {
            'action': action,
            'action_config': action_config,
            'governor': self,
            'parameters': parameters,
            'subject': subject
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

        action.patch_status(runtime, {
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
                subject,
                action,
                resp_data,
                event_name,
                action_config
            )

    def process_action_event_handlers(self, runtime, subject, action, event_data, event_name, action_config=None):
        if action_config == None:
            action_config = self.action_config(action.action)
        if event_name == None:
            event_name = event_data[action_config.callback_event_name_parameter]

        action.status_event_log(runtime, event_name, event_data)

        self.process_event_handlers(runtime, action_config.event_handlers, subject, action, event_data, event_name, runtime.logger)

    def process_subject_event_handlers(self, runtime, subject, event_name, logger):
        return self.process_event_handlers(runtime, self.subject_event_handlers, subject, None, {}, event_name, logger)

    def process_event_handlers(self, runtime, event_handlers, subject, action, event_data, event_name, logger):
        event_handled = False
        for event_handler in event_handlers:
            if event_handler.event == event_name:
                event_handled = True
                event_handler.process(runtime, self, subject, action, event_data, event_name, logger)
        return event_handled
