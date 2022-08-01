import base64
import copy
import json
import kubernetes
import logging
import os
import queue
import socket
import threading
import time

operator_logger = logging.getLogger('operator')

class AnarchyRuntime(object):
    def __init__(
        self,
        operator_domain=None,
        operator_namespace=None
    ):
        self.__init_domain(operator_domain)
        self.__init_namespace(operator_namespace)
        self.__init_kube_apis()
        self.is_active = False
        self.is_active_condition = threading.Condition()
        self.pod_name = os.environ.get('POD_NAME', os.environ.get('HOSTNAME', None))
        self.pod = self.core_v1_api.read_namespaced_pod(self.pod_name, self.operator_namespace)
        self.running_all_in_one = 'true' == os.environ.get('ANARCHY_RUNNING_ALL_IN_ONE', '')
        if self.running_all_in_one:
            self.anarchy_service_name = os.environ.get('ANARCHY_SERVICE', os.environ.get('HOSTNAME'))
            self.anarchy_service = self.pod
        else:
            self.anarchy_service_name = os.environ.get('ANARCHY_SERVICE', 'anarchy')
            self.anarchy_service = self.core_v1_api.read_namespaced_service(self.anarchy_service_name, self.operator_namespace)
        self.__init_callback_base_url()
        self.anarchy_runners = {}
        self.last_lost_runner_check = time.time()
        self.api_version = 'v1'
        self.api_group_version = self.operator_domain + '/' + self.api_version
        self.action_label = self.operator_domain + '/action'
        self.event_label = self.operator_domain + '/event'
        self.finished_label = self.operator_domain + '/finished'
        self.governor_label = self.operator_domain + '/governor'
        self.run_label = self.operator_domain + '/run'
        self.runner_label = self.operator_domain + '/runner'
        self.runner_terminating_label = self.operator_domain + '/runner-terminating'
        self.subject_label = self.operator_domain + '/subject'
        # Detect if running in development environment and switch to single pod, all-in-one, mode

    def __init_domain(self, operator_domain):
        if operator_domain:
            self.operator_domain = operator_domain
        else:
            self.operator_domain = os.environ.get('ANARCHY_DOMAIN', 'anarchy.gpte.redhat.com')

    def __init_kube_apis(self):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes.config.load_incluster_config()
        else:
            kubernetes.config.load_kube_config()
        self.core_v1_api = kubernetes.client.CoreV1Api()
        self.custom_objects_api = kubernetes.client.CustomObjectsApi()
        self.api_client = self.core_v1_api.api_client

    def __init_namespace(self, operator_namespace):
        if operator_namespace:
            self.operator_namespace = operator_namespace
        elif 'OPERATOR_NAMESPACE' in os.environ:
            self.operator_namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.operator_namespace = f.read()
        else:
            raise Exception('Unable to determine operator namespace. Please set OPERATOR_NAMESPACE environment variable.')

    def __init_callback_base_url(self):
        url = os.environ.get('CALLBACK_BASE_URL', '')
        if url and len(url) > 8:
            self.callback_base_url = url
            return
        if self.running_all_in_one:
            self.callback_base_url = 'http://{}:5000'.format(socket.gethostbyname(self.anarchy_service_name))
            return
        try:
            route = self.custom_objects_api.get_namespaced_custom_object(
                'route.openshift.io', 'v1', self.operator_namespace, 'routes', self.anarchy_service_name
            )
            spec = route.get('spec', {})
            if spec.get('tls', None):
                self.callback_base_url = 'https://' + spec['host']
            else:
                self.callback_base_url = 'http://' + spec['host']
            operator_logger.info('Set callback base url from OpenShift route: %s', self.callback_base_url)
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                route = self.custom_objects_api.create_namespaced_custom_object(
                    'route.openshift.io', 'v1', self.operator_namespace, 'routes',
                    {
                        'apiVersion': 'route.openshift.io/v1',
                        'kind': 'Route',
                        'metadata': {
                            'name': self.anarchy_service_name,
                            'namespace': self.operator_namespace,
                            'ownerReferences': [{
                                'apiVersion': self.anarchy_service.api_version,
                                'controller': True,
                                'kind': self.anarchy_service.kind,
                                'name': self.anarchy_service.metadata.name,
                                'uid': self.anarchy_service.metadata.uid
                            }]
                        },
                        'spec': {
                            'port': { 'targetPort': 'api' },
                            'tls': { 'termination': 'edge' },
                            'to': {
                                'kind': 'Service',
                                'name': self.anarchy_service_name
                            }
                        }
                    }
                )
                self.callback_base_url = 'https://' + route['spec']['host']
                operator_logger.info('Created OpenShift route %s and set callback base url: %s', route['metadata']['name'], self.callback_base_url)
            else:
                operator_logger.warning('Unable to determine a callback url. Callbacks will not function.')
                self.callback_base_url = None

    def action_callback_url(self, action_name):
        if not self.callback_base_url:
            raise Exception('Unable to set action callback URL. Please set CALLBACK_BASE_URL environment variable.')
        return '{}/action/{}'.format(self.callback_base_url, action_name)

    def dict_to_k8s_object(self, src, cls):
        return self.api_client.deserialize(FakeKubeResponse(src), cls)

    def get_secret_data(self, secret_name, secret_namespace=None):
        if not secret_namespace:
            secret_namespace = self.operator_namespace
        secret = self.core_v1_api.read_namespaced_secret(
            secret_name, secret_namespace
        )
        data = { k: base64.b64decode(v).decode('utf-8') for (k, v) in secret.data.items() }

        # Attempt to evaluate secret data valuse as YAML
        for k, v in data.items():
            try:
                data[k] = json.loads(v)
            except json.decoder.JSONDecodeError:
                pass
        return data

# See: https://github.com/kubernetes-client/python/issues/977
class FakeKubeResponse:
    def __init__(self, obj):
        self.data = json.dumps(obj)
