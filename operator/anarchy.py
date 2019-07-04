#!/usr/bin/env python

import base64
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
import socket
import sys
import string
import threading
import time
import yaml

from anarchyruntime import AnarchyRuntime
from anarchyapi import AnarchyAPI
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction

flask_api = flask.Flask('rest')

# Variables initialized during init()
anarchy_runtime = None
namespace = None
kube_api = None
kube_custom_objects = None
logger = None
service_account_token = None
anarchy_crd_domain = None

# Lock used by to trigger immediate subject action check
action_release_lock = threading.Lock()

def init():
    """Initialization function before management loops."""
    init_logging()
    init_namespace()
    init_service_account_token()
    init_kube_api()
    init_runtime()
    init_apis()
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

def override_select_header_content_type(content_types):
    """
    Returns `Content-Type` based on an array of content_types provided.

    Override default behavior to select application/merge-patch+json

    :param content_types: List of content-types.
    :return: Content-Type (e.g. application/json).
    """
    if not content_types:
        return 'application/json'

    content_types = [x.lower() for x in content_types]

    if 'application/json' in content_types or '*/*' in content_types:
        return 'application/json'
    if 'application/merge-patch+json' in content_types:
        return 'application/merge-patch+json'
    else:
        return content_types[0]

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
    kube_custom_objects.api_client.select_header_content_type = override_select_header_content_type

    anarchy_crd_domain = os.environ.get('ANARCHY_CRD_DOMAIN','gpte.redhat.com')

def init_runtime():
    global anarchy_runtime
    anarchy_runtime = AnarchyRuntime({
        "crd_domain": anarchy_crd_domain,
        "kube_api": kube_api,
        "kube_custom_objects": kube_custom_objects,
        "logger": logger,
        "namespace": namespace,
    })

def init_apis():
    """Get initial list of anarchy apis"""
    for resource in kube_custom_objects.list_namespaced_custom_object(
        anarchy_crd_domain, 'v1', namespace, 'anarchyapis'
    ).get('items', []):
        AnarchyAPI.register(resource)

def init_governors():
    """Get initial list of anarchy governors"""
    for resource in kube_custom_objects.list_namespaced_custom_object(
        anarchy_crd_domain, 'v1', namespace, 'anarchygovernors'
    ).get('items', []):
        AnarchyGovernor.register(resource)

def init_subjects():
    """Get initial list of anarchy subjects"""
    for resource in kube_custom_objects.list_namespaced_custom_object(
        anarchy_crd_domain, 'v1', namespace, 'anarchysubjects'
    ).get('items', []):
        handle_subject_added(resource)

def handle_subject_added(resource):
    subject = AnarchySubject.register(resource)
    if subject and subject.is_new():
        subject.process_subject_event_handlers(anarchy_runtime, 'added')

def handle_subject_modified(resource):
    handle_subject_added(resource)

def handle_subject_deleted(resource):
    logger.info("resource %s/%s deleted", resource['metadata']['namespace'], resource['metadata']['name'])
    logger.debug(resource)
    subject = AnarchySubject.get(
        resource['metadata']['namespace'],
        resource['metadata']['name'],
    )
    if subject:
        subject.process_subject_event_handlers(anarchy_runtime, 'deleted')

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
    action = AnarchyAction(action_resource)
    logger.debug("Action status on %s is %s", action.name(), action.status)
    if not action.has_started():
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
        logger.debug(event['object'])
        logger.debug("Action %s/%s %s",
            event['object']['metadata']['namespace'],
            event['object']['metadata']['name'],
            event['type']
        )
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
        logger.debug("Action release")
        action_release_lock.release()
    except threading.ThreadError:
        pass

def start_actions_loop():
    while True:
        try:
            action_release_lock.acquire()
            AnarchySubject.start_subject_actions(anarchy_runtime)
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

# FIXME? Do we need a security mechanism on the callback? or is the random
# string in the action name sufficient?
@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>',
    methods=['POST']
)
def action_callback(action_namespace, action_name):
    logger.info("Action callback for %s/%s", action_namespace, action_name)
    if not flask.request.json:
        flask.abort(400)
    action_resource = kube_custom_objects.get_namespaced_custom_object(
        anarchy_crd_domain, 'v1', action_namespace, 'anarchyactions', action_name
    )
    action = AnarchyAction(action_resource)
    action.process_event(anarchy_runtime, flask.request.json)
    return flask.jsonify({'status': 'ok'})

@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>/<string:event_name>',
    methods=['POST']
)
def event_callback(action_namespace, action_name, event_name):
    logger.info("Event %s called for %s/%s", event_name, action_namespace, action_name)
    if not flask.request.json:
        flask.abort(400)
    action_resource = kube_custom_objects.get_namespaced_custom_object(
        anarchy_crd_domain, 'v1', action_namespace, 'anarchyactions', action_name
    )
    action = AnarchyAction(action_resource)
    action.process_event(anarchy_runtime, flask.request.json, event_name)
    return flask.jsonify({'status': 'ok'})

def main():
    """Main function."""
    init()

    # FIXME - Watch for changes to apis
    #threading.Thread(
    #    name = 'watch-apis',
    #    target = watch_apis_loop
    #).start()

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
    http_server  = gevent.pywsgi.WSGIServer(('', 5000), flask_api)
    http_server.serve_forever()

if __name__ == '__main__':
    main()
