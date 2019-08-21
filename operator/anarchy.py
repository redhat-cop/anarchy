#!/usr/bin/env python

import datetime
import flask
import gevent.pywsgi
import kubernetes
import kubernetes.client.rest
import logging
import os
import prometheus_client
import sys
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
    if subject.is_pending_delete():
        logger.debug("Subject %s is pending delete", subject.namespace_name())
        if not subject.delete_started():
            subject.patch_status(anarchy_runtime, {
                'deleteHandlersStarted': True
            })
            subject.process_subject_event_handlers(anarchy_runtime, 'delete')
    elif subject.is_new:
        subject.add_finalizer(anarchy_runtime)
        subject.process_subject_event_handlers(anarchy_runtime, 'add')
    elif subject.is_updated:
        subject.process_subject_event_handlers(anarchy_runtime, 'update')

def handle_subject_modified(resource):
    handle_subject_added(resource)

def handle_subject_deleted(resource):
    logger.info("AnarchySubject %s/%s deleted", resource['metadata']['namespace'], resource['metadata']['name'])
    subject = AnarchySubject.get(
        resource['metadata']['namespace'],
        resource['metadata']['name'],
    )
    AnarchySubject.unregister(subject)

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
        event_obj = event['object']
        if event['type'] == 'ERROR':
            logger.info('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            return
        else:
            logger.debug("Action %s/%s %s",
                event_obj['metadata']['namespace'],
                event_obj['metadata']['name'],
                event['type']
            )
            if event['type'] == 'ADDED':
                handle_subject_added(event_obj)
            elif event['type'] == 'MODIFIED':
                handle_subject_modified(event_obj)
            elif event['type'] == 'DELETED':
                handle_subject_deleted(event_obj)

def watch_subjects_loop():
    while True:
        try:
            watch_subjects()
        except Exception as e:
            logger.exception("Error in subjects loop " + str(e))
            time.sleep(60)

def handle_action_added(action_resource):
    action = AnarchyAction(action_resource)
    logger.debug("Action add on %s", action.namespace_name())
    action.subject().queue_action(action)

def handle_action_modified(action_resource):
    action = AnarchyAction(action_resource)
    logger.debug("Action update on %s", action.namespace_name())
    action.subject().requeue_action(action)

def handle_action_deleted(action_resource):
    action = AnarchyAction(action_resource)
    logger.debug("Action delete on %s", action.namespace_name())
    subject = action.subject()
    if subject:
        subject.dequeue_action(action)

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
        logger.debug(event)
        event_obj = event['object']
        if event['type'] == 'ERROR':
            logger.info('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            return
        else:
            logger.debug("Action %s/%s %s",
                event_obj['metadata']['namespace'],
                event_obj['metadata']['name'],
                event['type']
            )
            if event['type'] == 'ADDED':
                handle_action_added(event_obj)
            elif event['type'] == 'MODIFIED':
                handle_action_modified(event_obj)
            elif event['type'] == 'DELETED':
                handle_action_deleted(event_obj)

def watch_actions_loop():
    while True:
        try:
            watch_actions()
        except Exception as e:
            logger.exception("Error in actions loop " + str(e))
            time.sleep(60)

def watch_action_pods():
    logger.debug('Starting watch for action pods')
    watch = kubernetes.watch.Watch()
    stream = watch.stream(
        kube_api.list_namespaced_pod,
        namespace,
        label_selector='gpte.redhat.com/anarchy-event-name'
    )
    action_pod_keep_seconds = int(os.environ.get('ACTION_POD_KEEP_SECONDS', 600))
    watch_start = time.time()
    restart_watch_after = watch_start + action_pod_keep_seconds / 2
    if restart_watch_after < 60:
        restart_watch_after = 60
    for event in stream:
        obj = event['object']
        if event['type'] == 'ERROR':
            logger.info('Watch %s - reason %s, %s',
                event_obj['status'],
                event_obj['reason'],
                event_obj['message']
            )
            return
        else:
            logger.debug("Pod %s/%s %s",
                obj.metadata.namespace,
                obj.metadata.name,
                event['type']
            )

            delete_older_than = (
                datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) -
                datetime.timedelta(0, action_pod_keep_seconds)
            )

            # Delete pods that have completed, are older than the threshold
            # and are not already marked for deletion.
            if obj.status.phase in ('Succeeded', 'Failed') \
            and obj.metadata.creation_timestamp < delete_older_than \
            and not obj.metadata.deletion_timestamp:
                kube_api.delete_namespaced_pod(
                    obj.metadata.name,
                    obj.metadata.namespace
                )

        if restart_watch_after < time.time():
            watch.stop()


def watch_action_pods_loop():
    while True:
        try:
            watch_action_pods()
        except Exception as e:
            logger.exception("Error in action pods loop " + str(e))
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

def __event_callback(action_namespace, action_name, event_name):
    if not flask.request.json:
        flask.abort(400)
        return

    action_resource = kube_custom_objects.get_namespaced_custom_object(
        anarchy_crd_domain, 'v1', action_namespace, 'anarchyactions', action_name
    )
    action = AnarchyAction(action_resource)

    if not action.check_callback_token(flask.request.headers.get('Authorization')):
        logger.warn("Invalid callback token for %s/%s", action_namespace, action_name)
        flask.abort(403)
        return

    action.process_event(anarchy_runtime, flask.request.json)
    return flask.jsonify({'status': 'ok'})

@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>',
    methods=['POST']
)
def action_callback(action_namespace, action_name):
    logger.info("Action callback for %s/%s", action_namespace, action_name)
    return __event_callback(action_namespace, action_name, None)

@flask_api.route(
    '/event/<string:action_namespace>/<string:action_name>/<string:event_name>',
    methods=['POST']
)
def event_callback(action_namespace, action_name, event_name):
    logger.info("Event %s called for %s/%s", event_name, action_namespace, action_name)
    return __event_callback(action_namespace, action_name, event_name)

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
        name = 'action-pods',
        target = watch_action_pods_loop
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
