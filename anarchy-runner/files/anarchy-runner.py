#!/usr/bin/env python

from base64 import b64encode
from datetime import datetime, timedelta
import ansible_runner
import copy
import json
import kopf
import kubernetes
import logging
import os
import shutil
import threading

class AnarchyEventRunner(object):

    def __init__(self):
        self.ansible_runner_lock = threading.Lock()
        self.ansible_runner = None
        self.ansible_thread = None
        self.domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
        self.event_queue = {}
        self.event_queue_batch = {}
        self.event_queue_lock = threading.Lock()
        self.kubeconfig = os.environ.get('KUBECONFIG', None)
        self.name = os.environ.get('RUNNER_NAME', None)
        self.runner_dir = os.environ.get('RUNNER_DIR', '/anarchy-runner/ansible-runner')
        if not self.kubeconfig:
            raise Exception('Environment variable KUBECONFIG must be set')
        if not self.name:
            raise Exception('Environment variable RUNNER_NAME must be set')
        self.__init_kube_apis()
        self.__init_namespace()
        self.__init_runner_dir()

    def __init_kube_apis(self):
        self.kube_auth_token = open('/run/secrets/kubernetes.io/serviceaccount/token').read().strip()
        self.kube_ca_cert = open('/run/secrets/kubernetes.io/serviceaccount/ca.crt').read()
        kube_config = kubernetes.client.Configuration()
        kube_config.api_key['authorization'] = self.kube_auth_token
        kube_config.api_key_prefix['authorization'] = 'Bearer'
        kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
        kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        api_client = kubernetes.client.ApiClient(kube_config)
        self.custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)

    def __init_namespace(self):
        if 'OPERATOR_NAMESPACE' in os.environ:
            self.namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.namespace = f.read()
        else:
            self.namespace = 'anarchy-operator'

    def __init_runner_dir(self):
        kubeconfig_fh = open(self.kubeconfig, 'w')
        kubeconfig_fh.write(json.dumps({
            'apiVersion': 'v1',
            'clusters': [{
                'name': 'cluster',
                'cluster': {
                    'certificate-authority-data': \
                        b64encode(self.kube_ca_cert.encode('ascii')).decode('ascii'),
                    'server': 'https://kubernetes.default.svc.cluster.local'
                }
            }],
            'contexts': [{
                'name': 'ansible',
                'context': {
                    'cluster': 'cluster',
                    'namespace': self.namespace,
                    'user': 'ansible'
                }
            }],
            'current-context': 'ansible',
            'users': [{
                'name': 'ansible',
                'user': {
                    'token': self.kube_auth_token
                }
            }]
        }))

    def clean_runner_dir(self):
        artifacts_dir = os.path.join(self.runner_dir, 'artifacts')
        for subdir in os.listdir(artifacts_dir):
            try:
                shutil.rmtree(os.path.join(artifacts_dir, subdir))
            except OSError as e:
                logging.warn('Failed to clean arifacts dir %s/%s: %s', artifacts_dir, subdir, str(e))
        tasks_dir = os.path.join(self.runner_dir, 'project', 'tasks')
        for subdir in os.listdir(tasks_dir):
            os.unlink(os.path.join(tasks_dir, subdir))
        vars_dir = os.path.join(self.runner_dir, 'project', 'vars')
        for subdir in os.listdir(vars_dir):
            os.unlink(os.path.join(vars_dir, subdir))

    def handle_event(self, anarchy_event, logger):
        anarchy_event_meta = anarchy_event['metadata']
        anarchy_event_name = anarchy_event_meta['name']
        anarchy_event_status = anarchy_event.get('status', None)
        if not anarchy_event_status \
        or anarchy_event_status['state'] == 'retry':
            self.queue_event_run(anarchy_event, logger)

    def queue_event_run(self, anarchy_event, logger):
        anarchy_event_meta = anarchy_event['metadata']
        anarchy_event_name = anarchy_event_meta['name']
        if anarchy_event_name in self.event_queue_batch:
            logger.info('already in current batch')
        else:
            logger.info('queueing')
            self.event_queue_lock.acquire()
            self.event_queue[anarchy_event_name] = anarchy_event
            self.event_queue_lock.release()
            self.start_runner()

    def runner_status_handler(self, status, runner_config):
        status_str = status['status']
        logging.info('ansible runner ' + status_str)
        if status_str == 'starting':
            pass
        elif status_str == 'running':
            pass
        elif status_str in ['successful', 'failed', 'canceled', 'timeout']:
            self.event_queue_batch = {}
            self.ansible_runner_lock.release()

    def start_runner(self):
        self.event_queue_lock.acquire()
        if self.event_queue \
        and self.ansible_runner_lock.acquire(False):
            self._start_runner()
        else:
            self.event_queue_lock.release()

    def _start_runner(self):
        self.event_queue_batch = self.event_queue
        self.event_queue = {}
        self.event_queue_lock.release()
        self.clean_runner_dir()
        self.write_runner_vars()
        self.write_runner_tasks()

        logging.info('starting ansible runner')
        ansible_runner.interface.run_async(
            playbook = 'main.yml',
            private_data_dir = self.runner_dir,
            status_handler = self.runner_status_handler
        )

    def write_runner_vars(self):
        open(os.path.join(self.runner_dir, 'env', 'extravars'), mode='w').write(
            json.dumps({
                # Write event names to extra vars
                'anarchy_events': list(self.event_queue_batch.keys()),
                'anarchy_runner_name': self.name,
                'anarchy_runner_timestamp': datetime.utcnow().strftime('%FT%TZ')
            })
        )
        # Write event vars to project vars files
        vars_dir = os.path.join(self.runner_dir, 'project', 'vars')
        for anarchy_event_name, anarchy_event in self.event_queue_batch.items():
            # Remove circular references
            pruned_event = copy.deepcopy(anarchy_event)
            del pruned_event['spec']['event']['tasks']
            del pruned_event['spec']['governor']
            del pruned_event['spec']['subject']
            if 'action' in pruned_event['spec']:
                del pruned_event['spec']['action']
            open(os.path.join(vars_dir, anarchy_event_name + '.yml'), mode='w').write(
                json.dumps({
                    'anarchy_event': pruned_event,
                    'anarchy_action': anarchy_event['spec'].get('action', None),
                    'anarchy_governor': anarchy_event['spec']['governor'],
                    'anarchy_subject': anarchy_event['spec']['subject']
                })
            )

    def write_runner_tasks(self):
        tasks_dir = os.path.join(self.runner_dir, 'project', 'tasks')
        for anarchy_event_name, anarchy_event in self.event_queue_batch.items():
            open(os.path.join(tasks_dir, anarchy_event_name + '.yml'), mode='w').write(
                json.dumps(anarchy_event['spec']['event']['tasks'])
            )

anarchy_runner = AnarchyEventRunner()

logging.warning(anarchy_runner.name)

@kopf.on.event(
    anarchy_runner.domain, 'v1', 'anarchyevents',
    labels={anarchy_runner.domain + '/runner': anarchy_runner.name}
)
def handle_event_event(event, logger, **_):
    event_type = event['type']
    anarchy_event = event['object']

    if event_type == 'DELETED':
        logger.info('AnarchyEvent %s deleted', anarchy_event['metadata']['name'])
    elif event_type in ['ADDED', 'MODIFIED', None]:
        anarchy_runner.handle_event(anarchy_event, logger)
