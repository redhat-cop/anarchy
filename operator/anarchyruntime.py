import base64
import kubernetes
import logging
import os
import queue
import time

#def override_select_header_content_type(content_types):
#    """
#    Returns `Content-Type` based on an array of content_types provided.
#
#    Override default behavior to select application/merge-patch+json
#
#    :param content_types: List of content-types.
#    :return: Content-Type (e.g. application/json).
#    """
#    if not content_types:
#        return 'application/json'
#
#    content_types = [x.lower() for x in content_types]
#
#    if 'application/json' in content_types or '*/*' in content_types:
#        return 'application/json'
#    if 'application/merge-patch+json' in content_types:
#        return 'application/merge-patch+json'
#    else:
#        return content_types[0]

class AnarchyRuntime(object):
    def __init__(
        self,
        logging_format='[%(asctime)s] %(threadName)s [%(levelname)8s] - %(message)s',
        logging_level=logging.INFO,
        operator_domain=None,
        operator_namespace=None
    ):
        self.__init_logger(logging_format, logging_level)
        self.__init_domain(operator_domain)
        self.__init_namespace(operator_namespace)
        self.__init_kube_apis()
        # Runners are registered and checked periodically to make sure they are still running
        self.anarchy_available_runner_queue = queue.Queue()
        self.anarchy_runners = {}
        self.last_lost_runner_check = time.time()
        self.runner_label = self.operator_domain + '/runner'

    def __init_domain(self, operator_domain):
        if operator_domain:
            self.operator_domain = operator_domain
        else:
            self.operator_domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')

    def __init_kube_apis(self):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/token'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/token')
            kube_auth_token = f.read()
            kube_config = kubernetes.client.Configuration()
            kube_config.api_key['authorization'] = kube_auth_token
            kube_config.api_key_prefix['authorization'] = 'Bearer'
            kube_config.host = os.environ['KUBERNETES_PORT'].replace('tcp://', 'https://', 1)
            kube_config.ssl_ca_cert = '/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        else:
            kubernetes.config.load_kube_config()
            kube_config = None

        api_client = kubernetes.client.ApiClient(kube_config)
        #api_client.select_header_content_type = override_select_header_content_type
        self.core_v1_api = kubernetes.client.CoreV1Api(api_client)
        self.custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)

    def __init_logger(self, logging_format, logging_level):
        self.logger = logging.getLogger('operator')

    def __init_namespace(self, operator_namespace):
        if operator_namespace:
            self.operator_namespace = operator_namespace
        elif 'OPERATOR_NAMESPACE' in os.environ:
            self.operator_namespace = os.environ['OPERATOR_NAMESPACE']
        elif os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.operator_namespace = f.read()
        else:
            self.operator_namespace = 'anarchy-operator'

    def get_secret_data(self, secret_name):
        secret = self.core_v1_api.read_namespaced_secret(
            secret_name, self.operator_namespace
        )
        return { k: base64.b64decode(v).decode('utf-8') for (k, v) in secret.data.items() }

    def get_available_runner(self):
        while True:
            runner = self.anarchy_available_runner_queue.get()
            if runner in self.anarchy_runners:
                return runner

    def put_available_runner(self, runner):
        self.logger.info('Runner %s is available', runner)
        self.anarchy_available_runner_queue.put(runner)

    def register_runner(self, runner):
        runner_is_new = runner not in self.anarchy_runners
        self.anarchy_runners[runner] = time.time()
        if runner_is_new:
            self.put_available_runner(runner)

    def remove_runner(self, runner):
        try:
            del self.anarchy_runners[runner]
        except KeyError:
            pass

    def runner_finished(self, runner):
        if runner in self.anarchy_runners:
            self.put_available_runner(runner)
