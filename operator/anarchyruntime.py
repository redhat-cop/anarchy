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

from anarchyutil import deep_update

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
        self.running_all_in_one = '' != os.environ.get('ODO_S2I_SCRIPTS_URL', '')
        if self.running_all_in_one:
            self.anarchy_service_name = os.environ.get('ANARCHY_SERVICE', socket.gethostbyname(os.environ.get('HOSTNAME')))
            self.anarchy_service = self.pod
        else:
            self.anarchy_service_name = os.environ.get('ANARCHY_SERVICE', 'anarchy')
            self.anarchy_service = self.core_v1_api.read_namespaced_service(self.anarchy_service_name, self.operator_namespace)
        self.__init_callback_base_url()
        self.anarchy_runners = {}
        self.last_lost_runner_check = time.time()
        self.action_label = self.operator_domain + '/action'
        self.active_label = self.operator_domain + '/active'
        self.event_label = self.operator_domain + '/event'
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
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/token'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/token')
            kube_auth_token = f.read()
            kube_config = kubernetes.client.Configuration()
            kube_config.api_key['authorization'] = 'Bearer ' + kube_auth_token
            kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
            kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        else:
            kubernetes.config.load_kube_config()
            kube_config = None

        self.api_client = kubernetes.client.ApiClient(kube_config)
        self.core_v1_api = kubernetes.client.CoreV1Api(self.api_client)
        self.custom_objects_api = kubernetes.client.CustomObjectsApi(self.api_client)

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
            self.callback_base_url = 'http://{}:5000'.format(self.anarchy_service_name)
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

    def get_vars(self, obj):
        if not obj:
            return
        merged_vars = copy.deepcopy(obj.vars)
        for var_secret in obj.var_secrets:
            secret_name = var_secret.get('name', None)
            secret_namespace = var_secret.get('namespace', None)
            if secret_name:
                try:
                    secret_data = self.get_secret_data(secret_name, secret_namespace)
                    var_name = var_secret.get('var', None)
                    if var_name:
                        deep_update(merged_vars, {var_name: secret_data})
                    else:
                        deep_update(merged_vars, secret_data)
                except kubernetes.client.rest.ApiException as e:
                    if e.status != 404:
                        raise
                    operator_logger.warning('varSecrets references missing secret, %s', secret_name)
            else:
                operator_logger.warning('varSecrets has entry with no name')
        return merged_vars

    def register_runner(self, runner):
        self.anarchy_runners[runner] = time.time()

    def remove_runner(self, runner):
        try:
            del self.anarchy_runners[runner]
        except KeyError:
            pass

    def watch_peering(self):
        '''
        Wait for KopfPeering to indicate this pod should be active.
        '''
        for event in kubernetes.watch.Watch().stream(
            self.custom_objects_api.list_namespaced_custom_object,
            'zalando.org', 'v1', self.operator_namespace, 'kopfpeerings'
        ):
            obj = event.get('object')
            if obj \
            and obj.get('apiVersion') == 'zalando.org/v1' \
            and obj.get('kind') == 'KopfPeering' \
            and obj['metadata']['name'] == self.anarchy_service_name:
                with self.is_active_condition:
                    active_peer = None
                    priority = 0
                    for peerid, status in obj.get('status', {}).items():
                        if status['priority'] > priority:
                            active_peer = peerid
                            priority = status['priority']
                    if active_peer and '@{}/'.format(self.pod_name) in active_peer:
                        if not self.is_active:
                            operator_logger.info('Became active kopf peer: %s', active_peer)
                        self.is_active = True
                    else:
                        if self.is_active:
                            operator_logger.info('Active kopf peer is now: %s', active_peer)
                        self.is_active = False
                    self.is_active_condition.notify()
