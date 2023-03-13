import kubernetes_asyncio
import logging
import os
import re

from asgi_tools import ResponseError

from anarchy import Anarchy
from anarchywatchobject import AnarchyWatchObject

import anarchyrunner

class AnarchyRunnerPod(AnarchyWatchObject):
    api_version = 'v1'
    cache = {}
    kind = 'Pod'
    plural = 'pods'
    preload = True

    @classmethod
    def get_from_request(cls, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            raise ResponseError.FORBIDDEN("Authorization header required")

        match = re.match(r'Bearer ([^: ]+):([^: ]+):(.*)', auth_header)
        if not match:
            raise ResponseError.FORBIDDEN("Invalid Authorization header")

        runner_name = match.group(1)
        pod_name = match.group(2)
        runner_token = match.group(3)

        anarchy_runner = anarchyrunner.AnarchyRunner.cache.get(runner_name)
        anarchy_runner_pod = cls.cache.get(pod_name)

        if not anarchy_runner \
        or not anarchy_runner_pod \
        or anarchy_runner_pod.runner_token != runner_token:
            raise ResponseError.FORBIDDEN("Invalid Bearer token")

        return anarchy_runner, anarchy_runner_pod

    @classmethod
    def watch_method(cls):
        return Anarchy.core_v1_api.list_namespaced_pod

    @classmethod
    def watch_method_kwargs(cls):
        return dict(
            label_selector = Anarchy.runner_label,
            namespace = Anarchy.namespace,
        )

    @classmethod
    async def on_startup(cls):
        if Anarchy.running_all_in_one:
            await cls.on_startup_running_all_in_one()
        else:
            await super().on_startup()

    @classmethod
    async def on_startup_running_all_in_one(cls):
        all_in_one = cls(
            kubernetes_asyncio.client.V1Pod(
                api_version = "v1",
                kind = "Pod",
                metadata = kubernetes_asyncio.client.V1ObjectMeta(
                    labels = {
                        Anarchy.runner_label: "default",
                    },
                    name = os.environ['HOSTNAME'],
                    namespace = Anarchy.namespace,
                    resource_version = 0,
                    uid = "00000000-0000-0000-0000-000000000000",
                ),
                spec = kubernetes_asyncio.client.V1PodSpec(
                    containers = [
                        kubernetes_asyncio.client.V1Container(
                            env = [
                                kubernetes_asyncio.client.V1EnvVar(
                                    name = "RUNNER_NAME",
                                    value = "default",
                                ),
                                kubernetes_asyncio.client.V1EnvVar(
                                    name = "RUNNER_TOKEN",
                                    value = os.environ['RUNNER_TOKEN'],
                                ),
                            ],
                            name = "runner",
                        )
                    ]
                ),
                status = kubernetes_asyncio.client.V1PodStatus()
            )
        )
        await cls.cache_put(all_in_one)
        logging.info(f"Cache preloaded {all_in_one}")

    def __init__(self, definition):
        super().__init__(definition)
        self.consecutive_failure_count = 0
        self.run_count = 0

    def __str__(self):
        return f"AnarchyRunnerPod {self.name} [{self.pod_ip}]"

    @property
    def is_deleting(self):
        return True if self.metadata.deletion_timestamp else False

    @property
    def is_marked_for_termination(self):
        return Anarchy.runner_terminating_label in self.labels

    @property
    def labels(self):
        return self.metadata.labels

    @property
    def metadata(self):
        return self.definition.metadata

    @property
    def name(self):
        return self.metadata.name

    @property
    def namespace(self):
        return self.metadata.namespace

    @property
    def pod_ip(self):
        self.status.pod_ip

    @property
    def resource_version(self):
        return self.metadata.resource_version

    @property
    def runner_name(self):
        return self.metadata.labels.get(Anarchy.runner_label)

    @property
    def runner_token(self):
        for env_var in self.spec.containers[0].env:
            if env_var.name == 'RUNNER_TOKEN':
                return env_var.value

    @property
    def spec(self):
        return self.definition.spec

    @property
    def status(self):
        return self.definition.status

    @property
    def uid(self):
        return self.metadata.uid

    async def delete(self):
        await Anarchy.core_v1_api.delete_namespaced_pod(
            name = self.name,
            namespace = self.namespace,
        )
