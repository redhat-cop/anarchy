#!/usr/bin/env python

from base64 import b64encode
from datetime import datetime, timedelta
import ansible_runner
import copy
import json
import kubernetes
import os
import requests
import shutil
import threading
import time
import queue

import logging
logging.basicConfig(
    format = '%(asctime)s %(levelname)s %(message)s',
    level = os.environ.get('LOG_LEVEL', 'INFO')
)

class AnarchyRunner(object):

    def __init__(self):
        self.anarchy_url = os.environ.get('ANARCHY_URL', 'http://anarchy-operator:5000')
        self.domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
        self.kubeconfig = os.environ.get('KUBECONFIG', None)
        self.name = os.environ.get('RUNNER_NAME', None)
        self.polling_interval = int(os.environ.get('POLLING_INTERVAL', 5))
        self.queue_name = os.environ.get('RUN_QUEUE', 'default')
        self.runner_dir = os.environ.get('RUNNER_DIR', '/anarchy-runner/ansible-runner')
        self.runner_token = os.environ.get('RUNNER_TOKEN', None)
        if not self.runner_token:
            raise Exception('Environment variable RUNNER_TOKEN must be set')
        if not self.kubeconfig:
            raise Exception('Environment variable KUBECONFIG must be set')
        if not self.name:
            raise Exception('Environment variable RUNNER_NAME must be set')
        self.__init_namespace()
        self.__init_runner_dir()

    def __init_namespace(self):
        if 'OPERATOR_NAMESPACE' in os.environ:
            self.namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.namespace = f.read()
        else:
            self.namespace = 'anarchy-operator'

    def __init_runner_dir(self):
        if not os.path.exists(self.kubeconfig) \
        or 0 == os.path.getsize(self.kubeconfig):
            self.__write_kubeconfig()

    def __write_kubeconfig(self):
        kube_auth_token = open('/run/secrets/kubernetes.io/serviceaccount/token').read().strip()
        kube_ca_cert = open('/run/secrets/kubernetes.io/serviceaccount/ca.crt').read()
        kubeconfig_fh = open(self.kubeconfig, 'w')
        kubeconfig_fh.write(json.dumps({
            'apiVersion': 'v1',
            'clusters': [{
                'name': 'cluster',
                'cluster': {
                    'certificate-authority-data': \
                        b64encode(kube_ca_cert.encode('ascii')).decode('ascii'),
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
                    'token': kube_auth_token
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

    def get_run(self):
        response = requests.get(
            '{}/runner/{}/{}'.format(self.anarchy_url, self.queue_name, self.name),
            headers={'Authorization': 'Bearer ' + self.runner_token}
        )
        return response.json()

    def post_result(self, run, result):
        requests.post(
            '{}/runner/{}/{}'.format(self.anarchy_url, self.queue_name, self.name),
            headers={'Authorization': 'Bearer ' + self.runner_token},
            json=dict(run=run, result=result)
        )

    def run(self):
        run = self.get_run()
        if not run:
            logging.info('No tasks to run')
            self.sleep()
        elif run['kind'] == 'AnarchyEvent':
            result = self.run_event(run)
            self.post_result(run, result)

    def run_event(self, anarchy_event):
        anarchy_event_name = anarchy_event['metadata']['name']
        self.clean_runner_dir()
        self.write_runner_vars(anarchy_event)
        self.write_runner_tasks(anarchy_event)
        logging.info('starting ansible runner')
        ansible_run = ansible_runner.interface.run(
            playbook = 'main.yml',
            private_data_dir = self.runner_dir
        )
        return {
            'rc': ansible_run.rc,
            'status': ansible_run.status,
            'stdout': ansible_run.stdout.read()
        }

    def sleep(self):
        time.sleep(self.polling_interval)

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

anarchy_runner = AnarchyRunner()

def runner_loop():
    while True:
        try:
            anarchy_runner.run()
        except Exception:
            logging.exception('Error in runner loop')
            anarchy_runner.sleep()

threading.Thread(
    name = 'start-actions',
    target = runner_loop
).start()
