import base64

class AnarchyRuntime(object):
    def __init__(self, config):
        self.crd_domain = config['crd_domain']
        self.kube_api = config['kube_api']
        self.kube_custom_objects = config['kube_custom_objects']
        self.logger = config['logger']
        self.namespace = config['namespace']

    def get_secret_data(self, secret_name):
        secret = self.kube_api.read_namespaced_secret(
            secret_name, self.namespace
        )
        return { k: base64.b64decode(v).decode('utf-8') for (k, v) in secret.data.items() }
