#!/usr/bin/env python

# TODO
#
# * Flask webserver to get events
# * Implement secret support for governor parameters
# * Implement ability to customize data sent to api rather than all parameters
# * Implement email event handler
# * Locking mechanism to prevent more than one anarchy operator from running
#

import base64
import datetime
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import jinja2
import os
import prometheus_client
import random
import re
import requests
import six
import socket
import sys
import string
import threading
import time
import yaml

api = flask.Flask('rest')

# Variables initialized during init()
namespace = None
kube_api = None
kube_custom_objects = None
logger = None
service_account_token = None
anarchy_crd_domain = None
anarchy_governors = {}
anarchy_subjects = {}

# Lock used by to trigger immediate subject action check
action_release_lock = threading.Lock()

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

def jinja2_render(template_string, template_vars):
    template = jinja2.Template(template_string)
    return template.render(template_vars)

def get_secret_data(secret_name):
    # FIXME
    return {}

class GovernorActionApiRequestConfig(object):

    def __init__(self, config):
        self.method = config.get('method', 'GET')

class GovernorActionApi(object):

    def __init__(self, api_spec):
        assert 'url' in api_spec, 'apis must define a url'
        self.url = api_spec['url']
        self.request_config = GovernorActionApiRequestConfig(api_spec.get('request', {}))
        self.status_code_events = api_spec.get('statusCodeEvents', {})
        self.sanity_check()

    def sanity_check(self):
        assert isinstance(self.status_code_events, dict), \
            'statusCodeEvents must be a dict'

    def call_api(self, subject, action):
        governor = subject.governor()
        parameters = governor.parameters()
        parameters.update(subject.parameters())

        url = jinja2_render(self.url, {
            'governor': governor,
            'subject': subject,
            'action': action,
            'parameters': parameters
        })

        logger.debug("{} to {}".format(self.request_config.method, url))
        logger.debug(parameters)

        if self.request_config.method == 'GET':
            return requests.get(url, params=parameters)
        elif self.request_config.method == 'DELETE':
            return requests.delete(url, params=parameters)
        elif self.request_config.method == 'POST':
            return requests.post(url, json=parameters)
        elif self.request_config.method == 'PUT':
            return requests.put(url, json=parameters)
        else:
            raise Exception('unknown request method ' + self.request_config.method)

    def status_code_event(self, code):
        return self.status_code_events.get(str(code), None)


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

    def process(self, subject, action):
        # FIXME
        pass

class ScheduleActionEventHandler(object):
    def __init__(self, handler_params):
        assert 'action' in handler_params, 'scheduleAction event handler requires action'
        self.action = handler_params['action']
        self.after_seconds = time_to_seconds(handler_params.get('after',0))

    def process(self, subject, action):
        subject.schedule_action(self.action, self.after_seconds)

class SetLabelsEventHandlerItem(object):
    def __init__(self, set_label):
        assert 'name' in set_label, 'setLabels must define name'
        self.name = set_label['name']
        assert 'jinja2Template' in set_label, 'setLabels must define jinja2Template'
        self.jinja2_template = jinja2.Template(set_label['jinja2Template'])

class SetLabelsEventHandler(object):
    def __init__(self, handler_params):
        assert 'setLabels' in handler_params, 'setLabelsParams must include setLabels list'
        self.set_labels = []
        for set_label in handler_params.get('setLabels', []):
            self.set_labels.append( SetLabelsEventHandlerItem(set_label) )

    def process(self, subject, action):
        set_labels = {}
        for item in self.set_status:
            set_values[item.name] = item.jinja2_template.render({
                'action': action,
                'governor': subject.governor(),
                'subject': subject
            })
        subject.metadata['labels'].update(set_values)
        subject.update()

class SetStatusEventHandlerItem(object):
    def __init__(self, set_status):
        assert 'name' in set_status, 'setStatus must define name'
        self.name = set_status['name']
        assert 'jinja2Template' in set_status, 'setStatus must define jinja2Template'
        self.jinja2_template = jinja2.Template(set_status['jinja2Template'])

class SetStatusEventHandler(object):
    def __init__(self, handler_params):
        assert 'setStatus' in handler_params, 'setStatusParams must include setStatus list'
        self.set_status = []
        for set_status in handler_params.get('setStatus', []):
            self.set_status.append( SetStatusEventHandlerItem(set_status) )

    def process(self, subject, action):
        set_values = {}
        for item in self.set_status:
            set_values[item.name] = item.jinja2_template.render({
                'action': action,
                'governor': subject.governor(),
                'subject': subject
            })
        subject.set_status(set_values)

class EventHandler(object):

    def __init__(self, handler_spec):
        assert 'handlerType' in handler_spec, 'eventHandlers must define handlerType'
        self.handler_type = handler_spec['handlerType']
        assert len(self.handler_type) > 0, 'eventHandlers must define handlerType'

        handler_class_name = (
            self.handler_type[0].upper() +
            self.handler_type[1:] +
            'EventHandler'
        )

        handler_class = getattr(sys.modules[__name__], handler_class_name)

        handler_params_name = self.handler_type + 'Params'
        assert handler_params_name in handler_spec, \
            'handler type {} requires {}'.format(self.handler_type, handler_params_name)

        self.handler = handler_class(handler_spec[handler_params_name])

    def process(self, subject, action):
        logger.info("Running {} event handler for {} in {}".format(
            self.handler_type, subject.name(), subject.namespace()
        ))
        self.handler.process(subject, action)

class EventHandlerList(object):

    def __init__(self, event_handler_spec):
        assert 'event' in event_handler_spec, 'eventHandlers list must define event'
        self.event = event_handler_spec['event']
        self.handlers = []
        for handler_spec in event_handler_spec.get('handlers', []):
            self.handlers.append( EventHandler(handler_spec) )

    def process(self, subject, action):
        logger.info("Processing event handlers for {} in {}".format(
            subject.name(), subject.namespace()
        ))
        for handler in self.handlers:
            handler.process(subject, action)

class GovernorAction(object):

    def __init__(self, action_spec):
        assert 'name' in action_spec, 'actions must define a name'
        self.name = action_spec['name']
        self.expire_after_seconds = time_to_seconds(action_spec.get('expireAfter', 0))
        assert 'api' in action_spec, 'actions must define api'
        self.api = GovernorActionApi(action_spec['api'])
        self.set_event_handlers(action_spec['eventHandlers'])

    def set_event_handlers(self, event_handlers):
        self.event_handler_lists = []
        for event_handler_spec in event_handlers:
            self.event_handler_lists.append(EventHandlerList(event_handler_spec))

    def start(self, subject, action):
        resp = self.api.call_api(subject, action)
        resp_data = None
        try:
            resp_data = resp.json()
        except ValueError as e:
            pass

        action.set_status({
            "apiResponse": {
                "status_code": resp.status_code,
                "text": resp.text,
                "data": resp_data
            }
        })

        event = self.api.status_code_event(resp.status_code)
        if event:
            self.process_event_handlers(subject, event, action)

    def process_event_handlers(self, subject, event, action):
        for event_handler_list in self.event_handler_lists:
            if event_handler_list.event == event:
                event_handler_list.process(subject, action)

class Governor(object):
    """AnarchyGovernor class"""

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))
        self.__set_actions()
        self.sanity_check()

    def __set_actions(self):
        actions = {}
        for action_spec in self.spec.get('actions', []):
            action = GovernorAction(action_spec)
            actions[action.name] = action
        self.actions = actions

    def sanity_check(self):
        if 'parameters' in self.spec:
            for name, value in self.spec['parameters'].items():
                if isinstance(value, dict):
                    assert 'secret_name' in param_value, 'dictionary parameters must define secret_name'
                    assert 'secret_key' in param_value, 'dictionary parameters must define secret_key'

    def name(self):
        return self.metadata['name']

    def uid(self):
        return self.metadata['uid']

    def resource_version(self):
        return self.metadata['resourceVersion']

    def governor_action(self, action):
        assert action in self.actions, \
            'governor has no action named {}'.format(action)
        return self.actions[action]

    def parameters(self):
        parameters = {}
        secrets = {}
        for name, value in self.spec.get('parameters', {}).items():
            if isinstance(value, dict):
                secret_name = value['secret_name']
                secret_key = value['secret_key']
                secret = secrets.get(secret_name, None)
                if secret == None:
                    secret = get_secret_data(secret_name)
                assert secret_key in secret, \
                    'data key {} not found in secret {}'.format(secret_key, secret_name)
                parameters[name] = secret[secret_key]
            else:
                parameters[name] = value
        return parameters

    def start_action(self, subject, action):
        governor_action = self.governor_action(action.action())
        governor_action.start(subject, action)

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handler_lists = []
        for event_handler_spec in event_handlers:
            logger.debug(event_handler_spec)
            self.subject_event_handler_lists.append(EventHandlerList(event_handler_spec))

    def process_subject_event_handlers(self, subject, event):
        for event_handler_list in self.subject_event_handler_lists:
            if event_handler_list.event == event:
                event_handler_list.process(subject, None)

class Subject(object):
    """AnarchySubject class"""

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.action_queue = []
        self.sanity_check()

    def sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'
        assert self.spec['governor'] in anarchy_governors, \
            'governor {} is unknown'.format(self.spec['governor'])

    def uid(self):
        return self.metadata['uid']

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def governor(self):
        return anarchy_governors[self.spec['governor']]

    def governor_name(self):
        return self.spec['governor']

    def parameters(self):
        return self.spec.get('parameters', {})

    def resource_version(self):
        return self.metadata['resourceVersion']

    def queue_action(self, action):
        logger.debug("Queued action {} on {} in {}".format(
            action.name(),
            self.name(),
            self.namespace()
        ))
        self.action_queue.append(action)

    def start_actions(self):
        logger.debug("Starting actions that are due on {}".format(self.name()))
        for action in self.action_queue[:]:
            if action.is_due():
                self.governor().start_action(self, action)
                self.action_queue.remove(action)

    def process_subject_event_handlers(self, event):
        return self.governor().process_subject_event_handlers(self, event)

    def schedule_action(self, action, after_seconds):
        after = (
            datetime.datetime.utcnow() +
            datetime.timedelta(0, after_seconds)
        ).strftime('%FT%TZ')

        logger.info("Scheduling action {} for {} governed by {} to run after {}".format(
            action, self.name(), self.governor_name(), after
        ))

        action_obj = Action({
            "metadata": {
                "generateName": self.name() + '-' + action + '-',
                "labels": {
                    anarchy_crd_domain + "/anarchy-subject": self.name(),
                    anarchy_crd_domain + "/anarchy-governor": self.governor_name(),
                },
                "ownerReferences": [{
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": self.name(),
                    "uid": self.uid()
                }]
            },
            "spec": {
                "action": action,
                "after": after,
                "governorRef": {
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "kind": "AnarchyGovernor",
                    "name": self.governor_name(),
                    "namespace": namespace,
                    "uid": self.governor().uid()
                },
                "subjectRef": {
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "kind": "AnarchySubject",
                    "name": self.name(),
                    "namespace": self.namespace(),
                    "uid": self.uid()
                }
            }
        })
        action_obj.create()
        self.set_status({
            "currentAction": action_obj.name()
        })

    def update(self):
        resource = kube_custom_objects.replace_namespaced_custom_object(
            anarchy_crd_domain, 'v1', self.namespace(), 'anarchysubjects', self.name(),
            {
                "apiVersion": anarchy_crd_domain + "/v1",
                "kind": "AnarchySubject",
                "metadata": self.metadata,
                "spec": self.spec,
                "status": self.status
            }
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

    def set_status(self, status_update):
        if self.status == None:
            self.status = status_update
        else:
            self.status.update(status_update)

        resource = kube_custom_objects.replace_namespaced_custom_object_status(
            anarchy_crd_domain, 'v1', self.namespace(), 'anarchysubjects', self.name(),
            {
                "apiVersion": anarchy_crd_domain + "/v1",
                "kind": "AnarchySubject",
                "metadata": self.metadata,
                "spec": self.spec,
                "status": self.status
            }
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

class Action(object):
    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.sanity_check()

    def sanity_check(self):
        # FIXME
        pass

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def action(self):
        return self.spec['action']

    def is_due(self):
        after_datetime = self.spec.get('after','')
        return after_datetime < datetime.datetime.utcnow().strftime('%FT%TZ')

    def governor(self):
        return anarchy_governors[self.spec['governorRef']['name']]

    def governor_name(self):
        return self.spec['governorRef']['name']

    def subject(self):
        return anarchy_subjects[
            self.spec['subjectRef']['namespace'] + ':' +
            self.spec['subjectRef']['name']
        ]

    def subject_name(self):
        return self.spec['subjectRef']['name']

    def subject_namespace(self):
        return self.spec['subjectRef']['namespace']

    def start(self):
        logger.debug('Starting action {}'.format(self.name()))
        self.subject().start_action(self)

    def create(self):
        scheduled_action = kube_custom_objects.create_namespaced_custom_object(
            anarchy_crd_domain, 'v1', self.subject_namespace(), 'anarchyactions',
            {
                "apiVersion": anarchy_crd_domain + "/v1",
                "kind": "AnarchyAction",
                "metadata": self.metadata,
                "spec": self.spec
            }
        )
        self.metadata = scheduled_action['metadata']
        self.spec = scheduled_action['spec']

    def set_status(self, status_update):
        if self.status == None:
            self.status = status_update
        else:
            self.status.update(status_update)

        kube_custom_objects.replace_namespaced_custom_object_status(
            anarchy_crd_domain, 'v1', self.namespace(), 'anarchyactions', self.name(),
            {
                "apiVersion": anarchy_crd_domain + "/v1",
                "kind": "AnarchyAction",
                "metadata": self.metadata,
                "spec": self.spec,
                "status": self.status
            }
        )

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_governors()
    init_subjects()
    logger.debug("Completed init")

def init_logging():
    """Define logger global and set default logging level.
    Default logging level is INFO and may be overridden with the
    LOGGING_LEVEL environment variable.
    """
    global logger
    logging.basicConfig(
        format='%(levelname)s %(threadName)s - %(message)s',
    )
    logger = logging.getLogger('anarchy')
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'DEBUG'))

def init_namespace():
    """
    Set the namespace global based on the namespace in which this pod is
    running.
    """
    global namespace
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
        namespace = f.read()

def init_service_account_token():
    global service_account_token
    with open('/run/secrets/kubernetes.io/serviceaccount/token') as f:
        service_account_token = f.read()

def init_kube_api():
    """Set kube_api global to communicate with the local kubernetes cluster."""
    global kube_api, kube_custom_objects, anarchy_crd_domain
    kube_config = kubernetes.client.Configuration()
    kube_config.api_key['authorization'] = service_account_token
    kube_config.api_key_prefix['authorization'] = 'Bearer'
    kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
    kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    kube_api = kubernetes.client.CoreV1Api(
        kubernetes.client.ApiClient(kube_config)
    )
    kube_custom_objects = kubernetes.client.CustomObjectsApi(
        kubernetes.client.ApiClient(kube_config)
    )
    anarchy_crd_domain = os.environ.get('ANARCHY_CRD_DOMAIN','gpte.redhat.com')

def init_governors():
    """Get initial list of anarchy governors"""
    for governor_resource in kube_custom_objects.list_namespaced_custom_object(
        anarchy_crd_domain, 'v1', namespace, 'anarchygovernors'
    ).get('items', []):
        register_governor(governor_resource)

def init_subjects():
    """Get initial list of anarchy subjects"""
    for subject_resource in kube_custom_objects.list_namespaced_custom_object(
        anarchy_crd_domain, 'v1', namespace, 'anarchysubjects'
    ).get('items', []):
        register_subject(subject_resource)

def register_governor(governor_resource):
    """Register governor"""
    governor = None
    try:
        governor = Governor(governor_resource)
        anarchy_governors[governor.name()] = governor
        logger.info("Registered governor {}".format(governor.name()))
    except Exception as e:
        logger.exception("Error registering governor {} {}".format(
            governor_resource['metadata']['name'], str(e)
        ))
    return governor

def register_subject(subject_resource):
    """Register subject"""
    subject_key = (
        subject_resource['metadata']['namespace'] + ':' +
        subject_resource['metadata']['name']
    )

    subject = anarchy_subjects.get(subject_key, None)
    if subject and subject.resource_version() == subject_resource['metadata']['resourceVersion']:
        return

    try:
        subject = Subject(subject_resource)
        logger.info(subject_resource)
        logger.info("Registered subject {}".format(subject_key))
        anarchy_subjects[subject_key] = subject
    except Exception as e:
        logger.exception("Error registering subject {} in {}: {}".format(
            subject_resource['metadata']['name'],
            subject_resource['metadata']['namespace'],
            str(e)
        ))
    return subject

def handle_subject_added(subject_resource):
    subject = register_subject(subject_resource)
    if subject and subject.status == None:
        subject.process_subject_event_handlers('added')

def handle_subject_modified(subject_resource):
    # FIXME
    pass

def handle_subject_deleted(subject_resource):
    # FIXME
    pass

def watch_subjects():
    logger.debug('Starting watch for anarchysubjects')
    stream = kubernetes.watch.Watch().stream(
        kube_custom_objects.list_namespaced_custom_object,
        anarchy_crd_domain,
        'v1',
        namespace,
        'anarchysubjects'
    )
    for event in stream:
        if event['type'] == 'ADDED':
            handle_subject_added(event['object'])
        elif event['type'] == 'MODIFIED':
            handle_subject_modified(event['object'])
        elif event['type'] == 'DELETED':
            handle_subject_deleted(event['object'])

def watch_subjects_loop():
    while True:
        try:
            watch_subjects()
        except Exception as e:
            logger.exception("Error in subjects loop " + str(e))
            time.sleep(60)

def handle_action_added(action_resource):
    action = Action(action_resource)
    logger.debug("Action status on {} is {}".format(action.name(), action.status))
    if action.status == None:
        # FIXME - Add to action queue, process from queue
        action.subject().queue_action(action)

def handle_action_modified(action_resource):
    # FIXME
    pass

def handle_action_deleted(action_resource):
    # FIXME
    pass

def watch_actions():
    logger.debug('Starting watch for anarchyactions')
    stream = kubernetes.watch.Watch().stream(
        kube_custom_objects.list_namespaced_custom_object,
        anarchy_crd_domain,
        'v1',
        namespace,
        'anarchyactions'
    )
    for event in stream:
        logger.debug("action {} in {} {}".format(
            event['object']['metadata']['name'],
            event['object']['metadata']['namespace'],
            event['type']
        ))
        if event['type'] == 'ADDED':
            handle_action_added(event['object'])
        elif event['type'] == 'MODIFIED':
            handle_action_modified(event['object'])
        elif event['type'] == 'DELETED':
            handle_action_deleted(event['object'])

def watch_actions_loop():
    while True:
        try:
            watch_actions()
        except Exception as e:
            logger.exception("Error in actions loop " + str(e))
            time.sleep(60)

def action_release():
    """
    Signal start actions loop that there may be work to be done.

    This is done by releasing the action_release_lock. This
    lock may be already released.
    """
    try:
        action_release_lock.release()
    except threading.ThreadError:
        pass

def start_actions_loop():
    while True:
        try:
            action_release_lock.acquire()
            for subject in anarchy_subjects.values():
                subject.start_actions()
        except Exception as e:
            logger.exception("Error in start_actions_loop " + str(e))
            time.sleep(60)

def action_release_loop():
    """
    Periodically release the action_release_lock to trigger polling for actions
    that are due.
    """
    while True:
        try:
            action_release()
            time.sleep(5)
        except Exception as e:
            logger.exception("Error in action_release_loop " + str(e))
            time.sleep(60)

def main():
    """Main function."""
    init()
    # FIXME - Watch for changes to governors
    #threading.Thread(
    #    name = 'watch-governors',
    #    target = watch_governors_loop
    #).start()

    threading.Thread(
        name = 'subjects',
        target = watch_subjects_loop
    ).start()

    threading.Thread(
        name = 'actions',
        target = watch_actions_loop
    ).start()

    threading.Thread(
        name = 'start-actions',
        target = start_actions_loop
    ).start()

    threading.Thread(
        name = 'action-release',
        target = action_release_loop
    ).start()

    prometheus_client.start_http_server(8000)
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
