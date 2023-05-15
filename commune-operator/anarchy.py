import json
import os
import yaml

import kubernetes_asyncio

from inflection import pluralize

# pylint: disable=too-few-public-methods
class Anarchy():
    domain = os.environ.get('ANARCHY_DOMAIN', 'anarchy.gpte.redhat.com')
    version = os.environ.get('ANARCHY_VERSION', 'v1')

    api_version = f"{domain}/{version}"
    commune_label = f"{domain}/commune"

    @classmethod
    def k8s_obj_from_dict(cls, definition, k8s_class):
        return cls.api_client.deserialize(FakeKubeResponse(definition), k8s_class)

    @classmethod
    def k8s_obj_to_dict(cls, obj):
        return cls.api_client.sanitize_for_serialization(obj)

    @classmethod
    async def delete_resource(cls, api_version, kind, name, namespace):
        plural = pluralize(kind.lower())

        if '/' in api_version:
            resource_path = f"/apis/{api_version}/namespaces/{namespace}/{plural}/{name}"
        else:
            resource_path = f"/api/{api_version}/namespaces/{namespace}/{plural}/{name}"

        return await cls.api_client.call_api(
            auth_settings = ['BearerToken'],
            _return_http_data_only = True,
            header_params = {
                "Accept": "application/json",
            },
            method = 'DELETE',
            resource_path = resource_path,
            response_types_map = {
                200: "object",
            }
        )

    @classmethod
    async def server_side_apply(cls, definition):
        api_version = definition['apiVersion']
        kind = definition['kind']
        name = definition['metadata']['name']
        namespace = definition['metadata']['namespace']
        plural = pluralize(kind.lower())

        if '/' in api_version:
            resource_path = f"/apis/{api_version}/namespaces/{namespace}/{plural}/{name}"
        else:
            resource_path = f"/api/{api_version}/namespaces/{namespace}/{plural}/{name}"

        return await cls.api_client.call_api(
            auth_settings = ['BearerToken'],
            body = json.dumps(definition).encode('utf-8'),
            header_params = {
                "Accept": "application/json",
                "Content-Type": "application/apply-patch+yaml",
            },
            method = 'PATCH',
            query_params = {
                "fieldManager": "anarchy-commune-operator",
                "fieldValidation": "Ignore",
                "force": "true",
            },
            resource_path = resource_path,
            response_types_map = {
                200: "object",
                201: "object",
            },
            _return_http_data_only = True,
        )

    @classmethod
    async def on_cleanup(cls):
        await cls.api_client.close()

    @classmethod
    async def on_startup(cls):
        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount'):
            kubernetes_asyncio.config.load_incluster_config()
            with open(
                '/run/secrets/kubernetes.io/serviceaccount/namespace',
                encoding="utf-8"
            ) as ns_fh:
                cls.namespace = ns_fh.read()
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

        cls.pod = await cls.core_v1_api.read_namespaced_pod(
            name = os.environ['HOSTNAME'],
            namespace = cls.namespace,
        )


# See: https://github.com/kubernetes-client/python/issues/977
class FakeKubeResponse:
    def __init__(self, obj):
        self.data = json.dumps(obj)
