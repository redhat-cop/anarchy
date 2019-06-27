class AnarchyRuntime(object):
    def __init__(self, config):
        self.crd_domain = config['crd_domain']
        self.kube_api = config['kube_api']
        self.kube_custom_objects = config['kube_custom_objects']
        self.logger = config['logger']
        self.namespace = config['namespace']
