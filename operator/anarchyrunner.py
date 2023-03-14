import asyncio
import kubernetes_asyncio
import logging
import os
import pytimeparse

from copy import deepcopy
from datetime import datetime, timezone

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
        self.last_scale_up_datetime = datetime.now(timezone.utc)

    @property
    def consecutive_failure_limit(self):
        return self.spec.get('consecutiveFailureLimit')

    @property
    def last_scale_up_timestamp(self):
        return self.status.get('lastScaleUpTimestamp')

    @property
    def max_replicas(self):
        return self.spec.get('maxReplicas')

    @property
    def max_replicas_reached(self):
        if not self.max_replicas:
            return False
        return len(self.pods) >= self.max_replicas

    @property
    def min_replicas(self):
        return self.spec.get('minReplicas', 1)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def run_limit(self):
        return self.spec.get('runLimit')

    @property
    def runner_image(self):
        return os.environ.get('RUNNER_IMAGE', Anarchy.pod.spec.containers[0].image)

    @property
    def scale_up_delay(self):
        return pytimeparse.parse(self.spec.get('scaleUpDelay', '5m'))

    @property
    def scale_up_delay_exceeded(self):
        return (
            datetime.now(timezone.utc) - self.last_scale_up_datetime
        ).total_seconds() > self.scale_up_delay

    @property
    def scale_up_threshold(self):
        return self.spec.get('scaleUpThreshold')

    @property
    def scale_up_threshold_exceeded(self):
        idle_runner_count = 0
        for pod in self.status.get('pods', []):
            if 'run' not in pod:
                idle_runner_count += 1
        return self.scale_up_threshold < len(self.status.get('pendingRuns', [])) - idle_runner_count

    @property
    def scaling_check_interval(self):
        return pytimeparse.parse(self.spec.get('scalingCheckInterval', '1m'))

    @property
    def scaling_enabled(self):
        return self.scale_up_threshold != None and self.scale_up_threshold > 1

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

    async def create_runner_pod(self, logger):
        pod_template = self.make_pod_template()
        pod = await Anarchy.core_v1_api.create_namespaced_pod(Anarchy.namespace, pod_template)
        logger.info(f"Created pod {pod.metadata.name} for {self}")
        self.pods[pod.metadata.name] = pod
        self.last_scale_up_datetime = datetime.now(timezone.utc)

    async def handle_create(self, logger):
        await self.manage_service_account()
        await self.manage_pods(logger=logger)

    async def handle_delete(self, logger):
        pass

    async def handle_resume(self, logger):
        await self.manage_service_account()
        await self.manage_pods(logger=logger)

    async def handle_runner_pod_deleted(self, pod, logger):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods(logger=logger)

    async def handle_runner_pod_deleting(self, pod, logger):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods(logger=logger)

    async def handle_runner_pod_labeled_for_temination(self, pod, logger):
        self.pods.pop(pod.metadata.name, None)
        await self.manage_pods(logger=logger)

    async def handle_runner_pod_event(self, pod, logger):
        if pod.metadata.deletion_timestamp:
            await self.handle_runner_pod_deleting(pod=pod, logger=logger)
        elif Anarchy.runner_terminating_label in pod.metadata.labels:
            await self.handle_runner_pod_labeled_for_temination(pod=pod, logger=logger)
        else:
            await self.handle_runner_pod_update(pod=pod, logger=logger)

    async def handle_runner_pod_labeled_for_termination(self, pod, logger):
        pass

    async def handle_runner_pod_update(self, pod, logger):
        await self.manage_pod(pod=pod, logger=logger)

    async def handle_update(self, logger):
        await self.manage_service_account()
        await self.manage_pods(logger=logger)

    async def manage_pods(self, logger):
        if not self.pods_preloaded:
            await self.preload_pods()

        have_pending_pod = False
        for name, pod in list(self.pods.items()):
            await self.manage_pod(pod=pod, logger=logger)
            if pod.status.phase == 'Pending':
                have_pending_pod = True

        while len(self.pods) < self.min_replicas:
            await self.create_runner_pod(logger=logger)

        if not have_pending_pod \
        and self.scaling_enabled \
        and self.scale_up_threshold_exceeded \
        and not self.max_replicas_reached \
        and self.scale_up_delay_exceeded:
            logger.info(f"Scaling up {self}")
            await self.create_runner_pod(logger=logger)

    async def manage_pod(self, pod, logger):
        for entry in self.status.get('pods', []):
            if entry['name'] == pod.metadata.name:
                consecutive_failure_count = entry.get('consecutiveFailureCount', 0)
                run_count = entry.get('runCount', 0)
                break
        else:
            consecutive_failure_count = run_count = 0

        if self.consecutive_failure_limit and consecutive_failure_count >= self.consecutive_failure_limit:
            logging.info(
                f"Marking {self} pod {pod.metadata.name} for deletion "
                f"after {consecutive_failure_count} consecutive failures"
            )
            await self.mark_pod_for_termination(pod)
            return

        if self.run_limit and run_count > self.run_limit:
            logging.info(
                f"Marking {self} pod {pod.metadata.name} for deletion "
                f"after {run_count} consecutive runs"
            )
            await self.mark_pod_for_termination(pod)
            return

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
            return

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
