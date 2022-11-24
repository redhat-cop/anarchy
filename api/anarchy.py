import kubernetes_asyncio
import logging
import os

class Anarchy():
    domain = os.environ.get('ANARCHY_DOMAIN', 'anarchy.gpte.redhat.com')
    running_all_in_one = 'true' == os.environ.get('ANARCHY_RUNNING_ALL_IN_ONE', '')
    version = os.environ.get('ANARCHY_VERSION', 'v1')

    action_label = f"{domain}/action"
    api_version = f"{domain}/{version}"
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
    async def on_shutdown(cls):
        cls.core_v1_api.api_client.close()
        cls.custom_objects_api.api_client.close()

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

        cls.core_v1_api = kubernetes_asyncio.client.CoreV1Api()
        cls.custom_objects_api = kubernetes_asyncio.client.CustomObjectsApi()
