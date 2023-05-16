import json
import kopf
import kubernetes_asyncio
import logging
import os

class Anarchy():
    action_check_interval = int(os.environ.get('ACTION_CHECK_INTERVAL', 3))
    cleanup_interval = int(os.environ.get('CLEANUP_INTERVAL', 60))
    domain = os.environ.get('ANARCHY_DOMAIN', 'anarchy.gpte.redhat.com')
    run_check_interval = int(os.environ.get('RUN_CHECK_INTERVAL', 3))
    running_all_in_one = 'true' == os.environ.get('ANARCHY_RUNNING_ALL_IN_ONE', '')
    version = os.environ.get('ANARCHY_VERSION', 'v1')

    api_version = f"{domain}/{version}"
    action_label = f"{domain}/action"
    canceled_label = f"{domain}/canceled"
    delete_handler_label = f"{domain}/delete-handler"
    event_label = f"{domain}/event"
    finished_label = f"{domain}/finished"
    governor_label = f"{domain}/governor"
    runner_label = f"{domain}/runner"
    runner_terminating_label = f"{domain}/runner-terminating"
    spec_sha256_annotation = f"{domain}/spec-sha256"
    subject_label = f"{domain}/subject"

    @classmethod
    def k8s_obj_from_dict(cls, definition, k8s_class):
        return cls.api_client.deserialize(FakeKubeResponse(definition), k8s_class)

    @classmethod
    def k8s_obj_to_dict(cls, obj):
        return cls.api_client.sanitize_for_serialization(obj)

    @classmethod
    async def init_pod(cls):
        cls.pod = await cls.core_v1_api.read_namespaced_pod(
            name = os.environ['HOSTNAME'],
            namespace = cls.namespace,
        )

    @classmethod
    async def on_cleanup(cls):
        await cls.api_client.close()

    @classmethod
    async def on_startup(cls):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes_asyncio.config.load_incluster_config()
            with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
                cls.namespace = f.read()
        else:
            await kubernetes_asyncio.config.load_kube_config()
            if 'OPERATOR_NAMESPACE' in os.environ:
                cls.namespace = os.environ['OPERATOR_NAMESPACE']
            else:
                raise Exception(
                    'Unable to determine operator namespace. '
                    'Please set OPERATOR_NAMESPACE environment variable.'
                )

        cls.api_client = kubernetes_asyncio.client.ApiClient()
        cls.core_v1_api = kubernetes_asyncio.client.CoreV1Api(cls.api_client)
        cls.custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi(cls.api_client)

        if not cls.running_all_in_one:
            await cls.init_pod()

# See: https://github.com/kubernetes-client/python/issues/977
class FakeKubeResponse:
    def __init__(self, obj):
        self.data = json.dumps(obj)
