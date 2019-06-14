#!/usr/bin/env python

import base64
import datetime
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import random
import re
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

class Secret(object):
    def __init__(self, name):
        self.name = name

class SecretRef(object):
    def __init__(self, secret, key):
        self.secret = secret
        self.key = key

class GovernorActionApiRequestConfig(object):

    def __init__(self, config):
        self.method = config.get('method', 'GET')

class GovernorActionApi(object):

    def __init__(self, api_spec):
        assert 'url' in api_spec, 'apis must define a url'
        self.url = api_spec['url']
        self.request_config = GovernorActionApiRequestConfig(api_spec.get('request', {}))
        self.status_code_events = api_spec.get('statusCodeEvents', {})
        assert isinstance(self.status_code_events, dict), 'statusCodeEvents must be a dict'

class GovernorEventHandler(object):

    def __init__(self, handler_spec):
        assert 'handlerType' in handler_spec, 'eventHandlers must define handlerType'
        self.handler_type = handler_spec['handlerType']
        assert len(self.handler_type) > 0, 'eventHandlers must define handlerType'

        handler_class_name = (
            'GovernorEventHandler' +
            self.handler_type[0].upper() +
            self.handler_type[1:]
        )

        handler_class = getattr(sys.modules[__name__], handler_class_name)

        handler_params_name = self.handler_type + 'Params'
        assert handler_params_name in handler_spec, \
            'handler type {} requires {}'.format(self.handler_type, handler_params_name)

        self.handler = handler_class(handler_spec[handler_params_name])

    def run(self, governor, subject):
        logger.info("Running {} event handler for {} governed by {}".format(
            self.handler_type, subject.name, governor.name
        ))
        self.handler.run(governor, subject)

class GovernorEventHandlerEmail(object):
    def __init__(self, handler_params):
        # FIXME - validate params
        self.email_to = handler_params['to']
        self.email_from = handler_params['from']
        self.email_subject_template = handler_params['subject']
        self.email_body_template = handler_params['body']

    def run(self, governor, subject):
        pass

class GovernorEventHandlerScheduleAction(object):
    def __init__(self, handler_params):
        assert 'action' in handler_params, 'scheduleAction event handler requires action'
        self.action = handler_params['action']
        self.after_seconds = time_to_seconds(handler_params.get('after',0))

    def run(self, governor, subject):
        subject.schedule_action(self.action, self.after_seconds)

class GovernorEventHandlerSetLabelItem(object):
    def __init__(self, set_label):
        assert 'name' in set_label, 'setLabels must define name'
        self.name = set_label['name']
        assert 'jinja2Template' in set_label, 'setLabels must define jinja2Template'
        self.jinja2_template = set_label['jinja2Template']

    def run(self, governor, subject):
        pass

class GovernorEventHandlerSetLabels(object):
    def __init__(self, handler_params):
        assert 'setLabels' in handler_params, 'setLabelsParams must include setLabels list'
        self.set_labels = []
        for set_label in handler_params.get('setLabels', []):
            self.set_labels.append( GovernorEventHandlerSetLabelItem(set_label) )

    def run(self, governor, subject):
        pass

class GovernorEventHandlerSetStatusItem(object):
    def __init__(self, set_status):
        assert 'name' in set_status, 'setStatus must define name'
        self.name = set_status['name']
        assert 'jinja2Template' in set_status, 'setLabels must define jinja2Template'
        self.jinja2_template = set_status['jinja2Template']

    def run(self, governor, subject):
        pass

class GovernorEventHandlerSetStatus(object):
    def __init__(self, handler_params):
        assert 'setStatus' in handler_params, 'setStatusParams must include setStatus list'
        self.set_status = []
        for set_status in handler_params.get('setStatus', []):
            self.set_status.append( GovernorEventHandlerSetStatusItem(set_status) )

    def run(self, governor, subject):
        pass

class GovernorEventHandlerList(object):

    def __init__(self, event_handler_spec):
        assert 'event' in event_handler_spec, 'eventHandlers list must define event'
        self.event = event_handler_spec['event']
        self.handlers = []
        for handler_spec in event_handler_spec.get('handlers', []):
            self.handlers.append( GovernorEventHandler(handler_spec) )

    def run(self, governor, subject):
        logger.info("Running event handlers for {} governed by {}".format(
            subject.name, governor.name
        ))
        for handler in self.handlers:
            handler.run(governor, subject)

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
            self.event_handler_lists.append(GovernorEventHandlerList(event_handler_spec))

class Governor(object):

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.name = self.metadata['name']
        self.set_actions(self.spec.get('actions',[]))
        self.set_parameters(self.spec.get('parameters',{}))
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))

    def uid(self):
        return self.metadata['uid']

    def resource_version(self):
        return self.metadata['resourceVersion']

    def set_actions(self, action_list):
        self.actions = {}
        for action_spec in action_list:
            action = GovernorAction(action_spec)
            self.actions[action.name] = action

    def set_parameters(self, parameter_dict):
        self.parameters = {}
        for param_name, param_value in parameter_dict.items():
            if isinstance(param_value, dict):
                assert 'secret_name' in param_value, 'dictionary parameters must define secret_name'
                assert 'secret_key' in param_value, 'dictionary parameters must define secret_key'
                secret = self.register_secret(v['secret_name'])
                self.parameters[k] = SecretRef(secret, param_value['secret_key'])
            else:
                self.parameters[k] = str(param_value)

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handler_lists = []
        for event_handler_spec in event_handlers:
            logger.debug(event_handler_spec)
            self.subject_event_handler_lists.append(GovernorEventHandlerList(event_handler_spec))

    def run_subject_event_handlers(self, subject, event):
        for event_handler_list in self.subject_event_handler_lists:
            if event_handler_list.event == event:
                event_handler_list.run(self, subject)

class Subject(object):
    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.name = self.metadata['name']
        self.namespace = self.metadata['namespace']
        assert 'governor' in self.spec, \
            'subjects must define governor'
        self.governor_name = self.spec['governor']
        assert self.governor_name in anarchy_governors, \
            'governor {} is unknown'.format(self.governor_name)
        self.parameters = self.spec.get('parameters', {})

    def uid(self):
        return self.metadata['uid']

    def governor(self):
        return anarchy_governors[self.governor_name]

    def resource_version(self):
        return self.metadata['resourceVersion']

    def run_subject_event_handlers(self, event):
        return self.governor().run_subject_event_handlers(self, event)

    def schedule_action(self, action, after_seconds):
        logger.info("Scheduling action {} for {} governed by {}".format(
            action, self.name, self.governor_name
        ))
        after = (datetime.datetime.utcnow() + datetime.timedelta(0, 100)).strftime('%FT%TZ')
        scheduled_action = Action({
            "metadata": {
                "generateName": self.name + '-' + action + '-',
                "labels": {
                    "gpte.redhat.com/anarchy-subject": self.name,
                    "gpte.redhat.com/anarchy-governor": self.governor_name,
                },
                "ownerReferences": [{
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": self.name,
                    "uid": self.uid()
                }]
            },
            "spec": {
                "action": action,
                "after": after,
                "governorRef": {
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "kind": "AnarchyGovernor",
                    "name": self.governor_name,
                    "namespace": namespace,
                    "uid": self.governor().uid()
                },
                "subjectRef": {
                    "apiVersion": anarchy_crd_domain + "/v1",
                    "kind": "AnarchySubject",
                    "name": self.name,
                    "namespace": self.namespace,
                    "uid": self.uid()
                }
            }
        })
        scheduled_action.create()
        self.set_status({
            "current_action": scheduled_action.name()
        })

    def set_status(self, status_update):
        if self.status == None:
            self.status = status_update
        else:
            self.status.update(status_update)

        kube_custom_objects.replace_namespaced_custom_object_status(
            anarchy_crd_domain, 'v1', self.namespace, 'anarchysubjects', self.name,
            {
                "apiVersion": anarchy_crd_domain + "/v1",
                "kind": "AnarchySubject",
                "metadata": self.metadata,
                "spec": self.spec,
                "status": self.status
            }
        )

class Action(object):
    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def governor_name(self):
        return self.spec['governorRef']['name']

    def subject_name(self):
        return self.spec['subjectRef']['name']

    def subject_namespace(self):
        return self.spec['subjectRef']['namespace']

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

    def run(self):
        logger.debug('Running action {}'.format(self.name()))

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
        anarchy_governors[governor.name] = governor
        logger.info("Registered governor {}".format(governor.name))
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
        subject.run_subject_event_handlers('added')

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
    if action and action.status == None:
        action.run()

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

def govern():
    while True:
        time.sleep(5)

def govern_loop():
    while True:
        try:
            govern()
        except Exception as e:
            logger.exception("Error in govern loop " + str(e))
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
        name = 'actions',
        target = watch_actions_loop
    ).start()

    threading.Thread(
        name = 'subjects',
        target = watch_subjects_loop
    ).start()

    threading.Thread(
        name = 'govern',
        target = govern_loop
    ).start()

    prometheus_client.start_http_server(8000)
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
