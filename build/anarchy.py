#!/usr/bin/env python

import base64
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

class GovernorAction(object):

    def __init__(self, action_spec):
        assert 'name' in action_spec, 'actions must define a name'
        self.name = action_spec['name']
        self.expire_after_seconds = time_to_seconds(action_spec.get('expireAfter', 0))
        assert 'api' in action_spec, 'actions must define api'
        self.api = GovernorActionApi(action_spec['api'])

class Governor(object):

    def __init__(self, resource):
        self.name = resource['metadata']['name']
        self.set_actions(resource['spec'].get('actions',[]))
        self.set_parameters(resource['spec'].get('parameters',{}))
        self.set_subject_event_actions(resource['spec'].get('subjectEventActions',{}))

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

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_governors()
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
    logger.setLevel(os.environ.get('LOGGING_LEVEL', 'INFO'))

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

def register_governor(governor_resource):
    """Register governor"""
    governor = Governor(governor_resource)

def main():
    """Main function."""
    init()
    #threading.Thread(
    #    name = 'Provision',
    #    target = poll_provision_loop
    #).start()
    prometheus_client.start_http_server(8000)
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
