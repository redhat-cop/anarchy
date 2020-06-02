import copy
import kubernetes
import logging
import os
import requests
import requests.auth
import tempfile

from anarchyutil import deep_update, random_string

operator_logger = logging.getLogger('operator')

class AnarchyRunner(object):
    """Represents a pool of runner pods to process for AnarchyGovernors"""

    runners = {}

    @staticmethod
    def default_runner_definition(runtime):
        return {
            'apiVersion': runtime.operator_domain + '/v1',
            'kind': 'AnarchyRunner',
            'metadata': {
                'name': 'default',
                'resourceVersion': '0',
                'ownerReferences': [{
                    'apiVersion': runtime.anarchy_service.api_version,
                    'controller': True,
                    'kind': runtime.anarchy_service.kind,
                    'name': runtime.anarchy_service.metadata.name,
                    'uid': runtime.anarchy_service.metadata.uid
                }]
            },
            'spec': {
                'minReplicas': 1,
                'maxReplicas': 9,
                'token': random_string(50),
                'podTemplate': {
                    'spec': {
                        'serviceAccountName': 'anarchy-runner-default',
                        'containers': [{
                            'name': 'runner',
                            'resources': {
                                'limits': {
                                    'cpu': '1',
                                    'memory': '256Mi',
                                },
                                'requests': {
                                    'cpu': '500m',
                                    'memory': '256Mi',
                                },
                            },
                        }]
                    }
                }
            }
        }

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
        if not self.spec.get('token'):
            self.spec['token'] = random_string(50)
        self.sanity_check()

    def sanity_check(self):
        pass

    @property
    def image_pull_policy(self):
        return self.spec.get('imagePullPolicy', os.environ.get('RUNNER_IMAGE_PULL_POLICY', 'Always'))

    @property
    def kind(self):
        return 'AnarchyRunner'

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
        return self.spec.get('podTemplate', {}).get('metadata', {}).get('namespace', None)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def resources(self):
        return self.spec.get('resources', {
            'limits': { 'cpu': '1', 'memory': '256Mi' },
            'requests': { 'cpu': '200m', 'memory': '256Mi' },
        })

    @property
    def runner_token(self):
        '''
        Return runner token, used to authenticate callbacks.
        Default to use object uid if token is not set.
        '''
        return self.spec['token']

    @property
    def service_account_name(self):
        return self.spec.get('podTemplate', {}).get('spec', {}).get('serviceAccountName', 'anarchy-runner-' + self.name)

    @property
    def uid(self):
        return self.metadata.get('uid')

    def manage_runner_deployment(self, runtime):
        '''
        Manage Deployment for AnarchyRunner pods
        '''
        if runtime.running_all_in_one:
            return
        self.manage_runner_service_account(runtime)
        deployment_name = 'anarchy-runner-' + self.name
        deployment_namespace = self.pod_namespace or runtime.operator_namespace
        pod_template = self.pod_template.copy()
        deep_update(pod_template, {
            'metadata': {
                'labels': { runtime.runner_label: self.name }
            },
            'spec': {}
        })
        if not 'serviceAccountName' in pod_template['spec']:
            pod_template['spec']['serviceAccountName'] = 'anarchy-runner-' + self.name
        if not 'containers' in pod_template['spec']:
            pod_template['spec']['containers'] = [{}]
        runner_container = pod_template['spec']['containers'][0]
        if not 'name' in runner_container:
            runner_container['name'] = 'runner'
        if not runner_container.get('image'):
            image = os.environ.get('RUNNER_IMAGE', '')
            if image != '':
                runner_container['image'] = image
            else:
                runner_container['image'] = runtime.pod.spec.containers[0].image
        if not 'env' in runner_container:
            runner_container['env'] = []
        runner_container['env'].extend([
            {
                'name': 'ANARCHY_COMPONENT',
                'value': 'runner'
            },{
                'name': 'ANARCHY_URL',
                'value': 'http://{}.{}.svc:5000'.format(
                    runtime.anarchy_service_name, runtime.operator_namespace
                )
            },{
                'name': 'ANARCHY_DOMAIN',
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
            }
        ])

        deployment_definition = {
            'apiVersion': 'apps/v1',
            'kind': 'Deployment',
            'metadata': {
                'name': deployment_name,
                'namespace': deployment_namespace,
                'labels': { runtime.runner_label: self.name },
                'ownerReferences': [{
                    'apiVersion': runtime.operator_domain + '/v1',
                    'controller': True,
                    'kind': 'AnarchyRunner',
                    'name': self.name,
                    'uid': self.uid,
                }]
            },
            'spec': {
                'selector': {
                    'matchLabels': { runtime.runner_label: self.name }
                },
                'template': pod_template
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
        name = self.service_account_name
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
