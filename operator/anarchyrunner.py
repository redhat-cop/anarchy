import asyncio
import kubernetes_asyncio
import logging
import os

from copy import deepcopy

from anarchy import Anarchy
from anarchycachedkopfobject import AnarchyCachedKopfObject
from deep_merge import deep_merge
from random_string import random_string

class AnarchyRunner(AnarchyCachedKopfObject):
    cache = {}
    kind = 'AnarchyRunner'
    plural = 'anarchyrunners'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lock = asyncio.Lock()
        self.pods = {}
        self.pods_preloaded = False

    @property
    def min_replicas(self):
        return self.spec.get('minReplicas', 1)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def runner_image(self):
        return os.environ.get('RUNNER_IMAGE', Anarchy.pod.spec.containers[0].image)

    @property
    def service_account_name(self):
        """
        Return service account name from pod template or generated name otherwise.
        """
        return self.pod_template.get('spec', {}).get('serviceAccountName', f"anarchy-runner-{self.name}")

    def make_pod_template(self, runner_token=None):
        ret = deepcopy(self.pod_template)

        if not runner_token:
            runner_token = random_string(24)

        if 'metadata' not in ret:
            ret['metadata'] = {}
        ret['metadata']['generateName'] = f"anarchy-runner-{self.name}-"
        ret['metadata']['ownerReferences'] = [self.as_owner_ref()]

        if 'labels' not in ret['metadata']:
            ret['metadata']['labels'] = {}
        ret['metadata']['labels'][Anarchy.runner_label] = self.name

        if 'spec' not in ret:
            ret['spec'] = {}

        if 'serviceAccountName' not in ret['spec']:
            ret['spec']['serviceAccountName'] = self.service_account_name

        if not 'containers' in ret['spec']:
            ret['spec']['containers'] = [{}]

        container = ret['spec']['containers'][0]
        if 'name' not in container:
            container['name'] = 'runner'

        if not container.get('image'):
            container['image'] = self.runner_image

        if not 'env' in container:
            container['env'] = []

        container['env'].extend([
            {
                'name': 'ANARCHY_COMPONENT',
                'value': 'runner'
            },{
                'name': 'ANARCHY_URL',
                'value': f"http://anarchy.{Anarchy.namespace}.svc:5000",
            },{
                'name': 'ANARCHY_DOMAIN',
                'value': Anarchy.domain,
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
                'value': runner_token
            }
        ])

        return ret

    async def create_runner_pod(self):
        pod_template = self.make_pod_template()
        pod = await Anarchy.core_v1_api.create_namespaced_pod(Anarchy.namespace, pod_template)
        logging.info(f"Created pod {pod.metadata.name} for {self}")
        self.pods[pod.metadata.name] = pod

    async def handle_create(self):
        await self.manage_service_account()
        await self.manage_pods()

    async def handle_delete(self):
        pass

    async def handle_resume(self):
        await self.manage_service_account()
        await self.manage_pods()

    async def handle_runner_pod_deleted(self, pod):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods()

    async def handle_runner_pod_deleting(self, pod):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods()

    async def handle_runner_pod_labeled_for_temination(self, pod):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods()

    async def handle_runner_pod_event(self, pod):
        if pod.metadata.deletion_timestamp:
            await self.handle_runner_pod_deleting(pod)
        elif Anarchy.runner_terminating_label in pod.metadata.labels:
            await self.handle_runner_pod_labeled_for_temination(pod)
        else:
            await self.handle_runner_pod_update(pod)

    async def handle_runner_pod_labeled_for_termination(self, pod):
        pass

    async def handle_runner_pod_update(self, pod):
        await self.manage_pod(pod)

    async def handle_update(self):
        await self.manage_service_account()
        await self.manage_pods()

    async def manage_pods(self):
        if not self.pods_preloaded:
            await self.preload_pods()

        for name, pod in list(self.pods.items()):
            await self.manage_pod(pod)

        while len(self.pods) < self.min_replicas:
            await self.create_runner_pod()

    async def manage_pod(self, pod):
        # Get runner token to feed into pod template to prevent auto-generation
        runner_token = None
        for env_var in pod.spec.containers[0].env:
            if env_var.name == 'RUNNER_TOKEN':
                runner_token = env_var.value
                break

        pod_dict = Anarchy.k8s_obj_to_dict(pod)
        pod_template = self.make_pod_template(runner_token)

        cmp = deepcopy(pod_dict)
        deep_merge(cmp, pod_template)
        if pod_dict != cmp:
            logging.info(f"Marking {self} pod {pod.metadata.name} for deletion")
            await self.mark_pod_for_termination(pod)
        else:
            self.pods[pod.metadata.name] = pod

    async def manage_service_account(self):
        try:
            await Anarchy.core_v1_api.read_namespaced_service_account(self.service_account_name, Anarchy.namespace)
            return
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise
        service_account = await Anarchy.core_v1_api.create_namespaced_service_account(
            Anarchy.namespace,
            kubernetes.client.V1ServiceAccount(
                metadata = kubernetes.client.V1ObjectMeta(
                    name = self.service_account_name,
                    owner_references = [self.as_owner_ref()],
                )
            )
        )
        logging.info(f"Created ServiceAccount {service_account.metadata.name} for {self}")

    async def mark_pod_for_termination(self, pod):
        pod = await Anarchy.core_v1_api.patch_namespaced_pod(
            pod.metadata.name,
            pod.metadata.namespace,
            {
                "metadata": {
                    "labels": {
                        Anarchy.runner_terminating_label: ""
                    }
                }
            }
        )
        logging.info(f"Labeled AnarchyRunner pod {pod.metadata.name} for termination")
        self.pods.pop(pod.metadata.name, None)

    async def preload_pods(self):
        pod_list = await Anarchy.core_v1_api.list_namespaced_pod(
            Anarchy.namespace, label_selector=f"{Anarchy.runner_label}={self.name}"
        )
        for pod in pod_list.items:
            if not pod.metadata.deletion_timestamp \
            and not Anarchy.runner_terminating_label in pod.metadata.labels:
                self.pods[pod.metadata.name] = pod
        self.pods_preloaded = True
