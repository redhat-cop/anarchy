import copy
import kubernetes
import logging
import os
import requests
import requests.auth
import tempfile

from anarchyutil import deep_update

operator_logger = logging.getLogger('operator')

class AnarchyRunner(object):
    """Represents a pool of runner pods to process for AnarchyGovernors"""

    runners = {}

    @staticmethod
    def refresh_all_runner_pods(runtime):
        for runner in AnarchyRunner.runners.values():
            runner.refresh_runner_pods(runtime)

    @staticmethod
    def register(resource):
        runner = AnarchyRunner(resource)
        current_runner = AnarchyRunner.runners.get(runner.name, None)
        if current_runner:
            runner.runner_pods = current_runner.runner_pods
        operator_logger.info("Registered runner %s (%s)", runner.name, runner.resource_version)
        AnarchyRunner.runners[runner.name] = runner
        return runner

    @staticmethod
    def unregister(runner):
        if isinstance(runner, AnarchyRunner):
            del AnarchyRunner.runners[runner.name]
        else:
            del AnarchyRunner.runners[runner]

    @staticmethod
    def get(name):
        return AnarchyRunner.runners.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.runner_pods = {}
        self.spec = resource['spec']
        self.sanity_check()

    def sanity_check(self):
        # ansibleGalaxyRoles
        # preTasks
        # postTasks
        pass

    @property
    def image_pull_policy(self):
        return self.spec.get('imagePullPolicy', os.environ.get('RUNNER_IMAGE_PULL_POLICY', 'Always'))

    @property
    def max_replicas(self):
        return self.spec.get('maxReplicas', self.min_replicas)

    @property
    def min_replicas(self):
        return self.spec.get('minReplicas', 1)

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def pod_namespace(self):
        return self.spec.get('podNamespace', None)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def post_tasks(self):
        return self.spec.get('postTasks', [])

    @property
    def pre_tasks(self):
        return self.spec.get('preTasks', [])

    @property
    def resources(self):
        return self.spec.get('resources', {
            'limits': { 'cpu': '1', 'memory': '256Mi' },
            'requests': { 'cpu': '200m', 'memory': '256Mi' },
        })

    @property
    def runner_token(self):
        return self.spec.get('token', self.metadata['uid'])

    @property
    def service_account_name(self):
        return self.spec.get('serviceAccountName', 'anarchy-runner-' + self.name)

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def get_image(self, runtime):
        '''
        Return anarchy-runner image, checking in order of precedence:
        - The AnarchyRunner spec.image
        - Environment variable RUNNER_IMAGE
        - OpenShift imagestream anarchy-runner with tag "latest"
        - quay.io/redhat-cop/anarchy-runner:latest
        '''
        image = self.spec.get('image', os.environ.get('RUNNER_IMAGE', ''))
        if image != '':
            return image
        try:
            imagestream = runtime.custom_objects_api.get_namespaced_custom_object(
                'image.openshift.io', 'v1', self.namespace, 'imagestreams', 'anarchy-runner'
            )
            tags = imagestream.get('status', {}).get('tags', [])
            for tag in tags:
                if tag['tag'] == 'latest' and len(tag['items']) > 0:
                    return tag['items'][0]['dockerImageReference']
        except kubernetes.client.rest.ApiException as e:
            pass
        return 'quay.io/redhat-cop/anarchy-runner:latest'

    def manage_runner_deployment(self, runtime):
        """Manage Deployment for AnarchyRunner pods"""
        self.manage_runner_service_account(runtime)
        deployment_name = 'anarchy-runner-' + self.name
        deployment_namespace = self.pod_namespace or runtime.operator_namespace
        deployment_definition = {
            'apiVersion': 'apps/v1',
            'kind': 'Deployment',
            'metadata': {
                'name': deployment_name,
                'namespace': deployment_namespace,
                'labels': { runtime.runner_label: self.name }
            },
            'spec': {
                'selector': {
                    'matchLabels': { runtime.runner_label: self.name }
                },
                'template': {
                    'metadata': {
                        'labels': { runtime.runner_label: self.name }
                    },
                    'spec': {
                       'serviceAccountName': self.service_account_name,
                       'containers': [{
                           'name': 'runner',
                           'env': [{
                               'name': 'ANARCHY_URL',
                               'value': 'http://{}.{}.svc:5000'.format(
                                   runtime.anarchy_service, runtime.operator_namespace
                               )
                           },{
                               'name': 'OPERATOR_DOMAIN',
                               'value': runtime.operator_domain
                           },{
                               'name': 'POD_NAME',
                               'valueFrom': {
                                   'fieldRef': {
                                       'apiVersion': 'v1',
                                       'fieldPath': 'metadata.name'
                                   }
                               }
                           },{
                               'name': 'RUNNER_NAME',
                               'value': self.name
                           },{
                               'name': 'RUNNER_TOKEN',
                               'value': self.runner_token
                           }],
                           'image': self.get_image(runtime),
                           'imagePullPolicy': self.image_pull_policy,
                           'resources': self.resources
                       }]
                    }
                }
            }
        }

        deployment = None
        try:
            deployment = runtime.custom_objects_api.get_namespaced_custom_object(
                'apps', 'v1', deployment_namespace, 'deployments', deployment_name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

        if deployment:
            updated_deployment = copy.deepcopy(deployment)
            deep_update(updated_deployment, deployment_definition) 
            if updated_deployment['spec']['replicas'] < self.min_replicas:
                updated_deployment['spec']['replicas'] = self.min_replicas
            if updated_deployment['spec']['replicas'] > self.max_replicas:
                updated_deployment['spec']['replicas'] = self.max_replicas
            # FIXME - Scale based on queue size
            if deployment != updated_deployment:
                runtime.custom_objects_api.replace_namespaced_custom_object(
                    'apps', 'v1', deployment_namespace,
                    'deployments', deployment_name, updated_deployment
                )
        else:
            deployment_definition['spec']['replicas'] = self.min_replicas
            runtime.custom_objects_api.create_namespaced_custom_object(
                'apps', 'v1', deployment_namespace, 'deployments', deployment_definition
            )

    def manage_runner_service_account(self, runtime):
        """Create service account if not found"""
        name = 'anarchy-runner-' + self.name
        namespace = self.pod_namespace or runtime.operator_namespace
        try:
            runtime.core_v1_api.read_namespaced_service_account(name, namespace)
            return
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise
        runtime.core_v1_api.create_namespaced_service_account(
            namespace, 
            kubernetes.client.V1ServiceAccount(
               metadata=kubernetes.client.V1ObjectMeta(name=name)
            )
        )

    def refresh_runner_pods(self, runtime):
        """Refresh runtime list of AnarchyRunner pods"""
        missing_runner_pods = set(self.runner_pods.keys())
        for pod in runtime.core_v1_api.list_namespaced_pod(
            runtime.operator_namespace, 
            label_selector = '{}={}'.format(runtime.runner_label, self.name)
        ).items:
            if pod.status.phase == 'Running':
                pod_name = pod.metadata.name
                missing_runner_pods.discard(pod_name)
                if pod_name not in self.runner_pods:
                    self.runner_pods[pod_name] = None

        for pod_name in missing_runner_pods:
            run = self.runner_pods[pod_name]
            del self.runner_pods[pod_name]
            if run:
                run.handle_lost_runner(pod_name, runtime)
