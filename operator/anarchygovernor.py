import base64
import jinja2
import logging
import six

logger = logging.getLogger('anarchy')

from anarchyapi import AnarchyAPI

def get_secret_data(runtime, secret_name):
    secret = runtime.kube_api.read_namespaced_secret(
        secret_name, runtime.namespace
    )
    return { k: base64.b64decode(v) for (k, v) in secret.data.items() }

def add_parameters(parameters, runtime, add):
    secrets = {}
    for name, value in add.items():
        if isinstance(value, dict):
            secret_name = value['secret_name']
            secret_key = value['secret_key']
            secret = secrets.get(secret_name, None)
            if secret == None:
                secret = get_secret_data(runtime, secret_name)
            assert secret_key in secret, \
                'data key {} not found in secret {}'.format(secret_key, secret_name)
            parameters[name] = secret[secret_key]
        else:
            parameters[name] = value

def jinja2render(template_string, template_vars):
    template = jinja2.Template(template_string)
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

    class EventHandlerList(object):
        def __init__(self, spec):
            assert 'event' in spec, 'eventHandlers list must define event'
            self.event = spec['event']
            self.handlers = []
            for handler_spec in spec.get('handlers', []):
                assert 'handlerType' in handler_spec, \
                    'eventHandlers must define handlerType'
                handlerType = handler_spec['handlerType']
                assert handlerType + 'Params' in handler_spec, \
                    '{0}EventHandler must define {0}Params'.format(handlerType)
                handler_class_name = (
                    handlerType[0].upper() +
                    handlerType[1:] +
                    'EventHandler'
                )
                handler_class = getattr(AnarchyGovernor, handler_class_name)
                self.handlers.append( handler_class(handler_spec[handlerType + 'Params']) )

        def process(self, runtime, governor, subject, action, event_data, event_name):
            logger.info("Processing event handlers for %s",
                subject.namespace_name()
            )
            for handler in self.handlers:
                handler.process(runtime, governor, subject, action, event_data, event_name)

    class EmailEventHandler(object):
        def __init__(self, handler_params):
            self.email_to = handler_params['to']
            self.email_from = handler_params['from']
            self.email_subject_template = handler_params['subject']
            self.email_body_template = handler_params['body']
            self.sanity_check()

        def sanity_check(self):
            # FIXME
            pass

        def process(self, runtime, governor, subject, action, event_data, event_name):
            # FIXME
            pass

    class ScheduleActionEventHandler(object):
        def __init__(self, handler_params):
            assert 'action' in handler_params, 'scheduleAction event handler requires action'
            self.action = handler_params['action']
            self.after_seconds = time_to_seconds(handler_params.get('after',0))

        def process(self, runtime, governor, subject, action, event_data, event_name):
            subject.schedule_action(runtime, self.action, self.after_seconds)

    class SetLabelsEventHandler(object):
        def __init__(self, handler_params):
            assert 'setLabels' in handler_params, 'setLabelsParams must include setLabels list'
            self.set_labels = {}
            for set_label in handler_params.get('setLabels', []):
                assert 'name' in set_label, 'setLabels must define name'
                assert 'jinja2Template' in set_label, 'setLabels must define jinja2Template'
                set_labels[set_label['name']] = jinja2.Template(set_label['jinja2Template'])

        def process(self, runtime, governor, subject, action, event_data, event_name):
            set_labels = {}
            for label, jinja2_template in self.set_labels.items():
                set_labels[label] = jinja2_template.render({
                    'action': action,
                    'event': event_name,
                    'event_data': event_data,
                    'governor': governor,
                    'subject': subject
                })
            subject.metadata['labels'].update(set_labels)
            subject.update(runtime)

    class SetStatusEventHandler(object):
        def __init__(self, handler_params):
            assert 'setStatus' in handler_params, 'setStatusParams must include setStatus list'
            self.set_status = {}
            for set_status in handler_params.get('setStatus', []):
                assert 'name' in set_status, 'setStatus must define name'
                assert 'jinja2Template' in set_status, 'setStatus must define jinja2Template'
                set_status[set_status['name']] = jinja2.Template(set_status['jinja2Template'])

        def process(self, runtime, governor, subject, action, event_data, event_name):
            set_status = {}
            for status, jinja2_template in self.set_status.items():
                set_status[status] = jinja2_template.render({
                    'action': action,
                    'event': event_name,
                    'event_data': event_data,
                    'governor': governor,
                    'subject': subject
                })
            subject.set_status(runtime, set_status)

    class RequestConfig(object):
        def __init__(self, spec):
            assert 'api' in spec, 'request must define api'
            self.api = spec['api']
            self.callback_url_parameter = spec.get('callbackUrlParameter', None)
            self.method = spec.get('method', 'POST')
            assert 'path' in spec, 'request must define path'
            self.path = spec['path']
            self.status_code_events = spec.get('statusCodeEvents', {})

        def status_code_event(self, status_code):
            return self.status_code_events.get(str(status_code), None)

    class ActionConfig(object):
        def __init__(self, spec):
            assert 'name' in spec, 'actions must define a name'
            self.name = spec['name']
            self.callback_event_name_parameter = spec.get('callbackEventNameParameter', None)

            assert 'request' in spec, 'actions must define request'
            self.request = AnarchyGovernor.RequestConfig(spec['request'])

            assert 'eventHandlers' in spec, 'actions must define eventHandlers'
            self.event_handler_lists = []
            for event_handler_spec in spec['eventHandlers']:
                self.event_handler_lists.append(
                    AnarchyGovernor.EventHandlerList(event_handler_spec)
                )

    governors = {}

    @classmethod
    def register(_class, resource):
        governor = _class(resource)
        logger.info("Registered governor %s", governor.name())
        AnarchyGovernor.governors[governor.name()] = governor
        return governor

    @classmethod
    def unregister(_class, governor):
        if isinstance(governor, AnarchyGovernor):
            del AnarchyGovernor.governors[governor.name()]
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
            action = AnarchyGovernor.ActionConfig(action_spec)
            self.actions[action.name] = action
        self.sanity_check()

    def __set_actions(self):
        actions = {}
        self.actions = actions

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handler_lists = []
        for event_handler_spec in event_handlers:
            logger.debug(event_handler_spec)
            self.subject_event_handler_lists.append(
                AnarchyGovernor.EventHandlerList(event_handler_spec)
            )

    def sanity_check(self):
        if 'parameters' in self.spec:
            for name, value in self.spec['parameters'].items():
                if isinstance(value, dict):
                    assert 'secret_name' in value, 'dictionary parameters must define secret_name'
                    assert 'secret_key' in value, 'dictionary parameters must define secret_key'

    def name(self):
        return self.metadata['name']

    def uid(self):
        return self.metadata['uid']

    def resource_version(self):
        return self.metadata['resourceVersion']

    def get_parameters(self, runtime, api):
        parameters = {}
        add_parameters(parameters, runtime, api.parameters())
        add_parameters(parameters, runtime, self.spec.get('parameters', {}))
        return parameters

    def action_config(self, name):
        assert name in self.actions, \
            'governor has no action named {}'.format(name)
        return self.actions[name]

    def start_action(self, runtime, subject, action):
        action_name = action.action()
        action_config = self.action_config(action_name)

        api = AnarchyAPI.get(action_config.request.api)

        parameters = self.get_parameters(runtime, api)
        parameters.update(subject.parameters())
        if action_config.request.callback_url_parameter:
            parameters[action_config.request.callback_url_parameter] = action.callback_url()

        jinja2vars = {
            'governor': self,
            'subject': subject,
            'action': action,
            'parameters': parameters
        }

        path = jinja2render(action_config.request.path, jinja2vars)

        headers = {}
        for header in api.headers():
            headers[header['name']] = jinja2render(header['value'], jinja2vars)

        resp, url, method = api.call(path, parameters, headers, action_config.request)

        resp_data = None
        try:
            resp_data = resp.json()
        except ValueError as e:
            pass

        action.set_status(runtime, {
            "apiUrl": url,
            "apiMethod": method,
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
            action_config = self.action_config(action.action())
        if event_name == None:
            event_name = event_data[action_config.callback_event_name_parameter]

        action.status_event_log(runtime, event_name, event_data)

        self.process_event_handlers(runtime, action_config.event_handler_lists, subject, action, event_data, event_name)

    def process_subject_event_handlers(self, runtime, subject, event_name):
        self.process_event_handlers(runtime, self.subject_event_handler_lists, subject, None, {}, event_name)

    def process_event_handlers(self, runtime, event_handler_lists, subject, action, event_data, event_name):
        for event_handler_list in event_handler_lists:
            if event_handler_list.event == event_name:
                for event_handler in event_handler_list.handlers:
                    event_handler.process(runtime, self, subject, action, event_data, event_name)
