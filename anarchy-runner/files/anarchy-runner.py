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
import queue

class AnarchyEventRunner(object):

    def __init__(self):
        self.domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
        self.event_cache = {}
        self.event_queue = queue.Queue()
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
        for tasks_file in os.listdir(tasks_dir):
            os.unlink(os.path.join(tasks_dir, tasks_file))

    def queue_event_run(self, anarchy_event, logger):
        anarchy_event_meta = anarchy_event['metadata']
        anarchy_event_name = anarchy_event_meta['name']
        
        if anarchy_event_name in self.event_cache:
            self.event_cache[anarchy_event_name] = anarchy_event
        else:
            self.event_cache[anarchy_event_name] = anarchy_event
            logger.info('queueing %s', anarchy_event_name)
            self.event_queue.put(anarchy_event_name)

    def run(self):
        anarchy_event_name = self.event_queue.get()
        anarchy_event = self.event_cache[anarchy_event_name]
        self.run_event(anarchy_event_name, anarchy_event)
        del self.event_cache[anarchy_event_name]

    def run_event(self, anarchy_event_name, anarchy_event):
        self.clean_runner_dir()
        self.write_runner_vars(anarchy_event)
        self.write_runner_tasks(anarchy_event)
        logging.info('starting ansible runner')
        ansible_run = ansible_runner.interface.run(
            playbook = 'main.yml',
            private_data_dir = self.runner_dir
        )
        stdout = ansible_run.stdout.read()
        status_str = ansible_run.status
        if status_str == 'successful':
            logging.info('Ansible run for %s successful', anarchy_event_name)
            self.update_anarchy_event(anarchy_event_name, anarchy_event, 'success', status_str, stdout)
        else:
            logging.warning('Ansible run for %s %s', anarchy_event_name, ansible_run.status)
            self.update_anarchy_event(anarchy_event_name, anarchy_event, 'failed', status_str, stdout)

    def update_anarchy_event(self, anarchy_event_name, anarchy_event, status, status_str, stdout):
        timestamp = datetime.utcnow().strftime('%FT%TZ')
        patch = {
            'metadata': {
                'labels': {
                    self.domain + '/runner': status + '-' + self.name
                }
            },
            'spec': {
                'lastRun': timestamp,
                'log': anarchy_event['spec'].get('log', []) + [{
                    'runner': self.name,
                    'result': status_str,
                    'stdout': stdout,
                    'timestamp': timestamp
                }]
            }
        }
        if status != 'success':
            patch['spec']['failures'] = anarchy_event['spec'].get('failures', 0) + 1
        self.custom_objects_api.patch_namespaced_custom_object(
            self.domain, 'v1', self.namespace, 'anarchyevents', anarchy_event_name, patch
        )

    def write_runner_vars(self, anarchy_event):
        extravars = {
            'anarchy_event': anarchy_event,
            'anarchy_governor': anarchy_event['spec']['governor'],
            'anarchy_governor_name': anarchy_event['spec']['governor']['metadata']['name'],
            'anarchy_operator_domain': self.domain,
            'anarchy_operator_namespace': self.namespace,
            'anarchy_subject': anarchy_event['spec']['subject'],
            'anarchy_subject_name': anarchy_event['spec']['subject']['metadata']['name'],
            'anarchy_runner_name': self.name,
            'anarchy_runner_timestamp': datetime.utcnow().strftime('%FT%TZ'),
            'event_data': anarchy_event['spec']['event']['data'],
            'event_name': anarchy_event['spec']['event']['name']
        }

        if 'action' in anarchy_event['spec']:
            extravars['anarchy_action'] = anarchy_event['spec']['action']
            extravars['anarchy_action_name'] = anarchy_event['spec']['action']['metadata']['name']

        open(self.runner_dir + '/env/extravars', mode='w').write(json.dumps(extravars))

    def write_runner_tasks(self, anarchy_event):
        tasks_file = self.runner_dir + '/project/tasks/event.yml'
        open(tasks_file, mode='w').write(
            json.dumps(anarchy_event['spec']['event']['tasks'])
        )

anarchy_runner = AnarchyEventRunner()

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
        anarchy_runner.queue_event_run(anarchy_event, logger)

def runner_loop():
    while True:
        try:
            anarchy_runner.run()
        except Exception:
            logging.exception('Error in runner loop')

threading.Thread(
    name = 'start-actions',
    target = runner_loop
).start()
