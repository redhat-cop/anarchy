import asyncio
import pathlib
import yaml

from inflection import pluralize
import aiofiles
import kubernetes_asyncio

from anarchy import Anarchy

class AnarchyCommune:
    cache = {}
    api_version = Anarchy.api_version
    group = Anarchy.domain
    kind = 'AnarchyCommune'
    plural = 'anarchycommunes'
    version = Anarchy.version

    @classmethod
    def load(cls, name, namespace, **kwargs):
        anarchy_commune = cls.cache.get((namespace, name))
        if anarchy_commune:
            anarchy_commune.update(**kwargs)
        else:
            anarchy_commune = cls(name=name, namespace=namespace, **kwargs)
            cls.cache[(namespace, name)] = anarchy_commune
        return anarchy_commune

    def __init__(self, annotations, labels, meta, name, namespace, spec, status, uid, **_):
        self.annotations = annotations
        self.labels = labels
        self.lock = asyncio.Lock()
        self.metadata = meta
        self.name = name
        self.namespace = namespace
        self.spec = spec
        self.status = status
        self.uid = uid

    def __str__(self):
        return f"{self.kind} {self.name} in {self.namespace}"

    @property
    def resource_references(self):
        return self.status.get('resources', [])

    def as_owner_reference(self):
        return {
            "apiVersion": self.api_version,
            "controller": True,
            "kind": self.kind,
            "name": self.name,
            "uid": self.uid,
        }

    def update(self, annotations, labels, meta, spec, status, uid, **_):
        self.annotations = annotations
        self.labels = labels
        self.metadata = meta
        self.spec = spec
        self.status = status
        self.uid = uid

    def update_from_definition(self, definition):
        metadata = definition['metadata']
        self.annotations = metadata.get('annotations', {})
        self.labels = metadata.get('labels', {})
        self.metadata = metadata
        self.spec = definition['spec']
        self.status = definition.get('status', {})
        self.uid = metadata['uid']

    async def add_resource_reference_to_status(self, add_reference):
        for idx, reference in enumerate(self.resource_references):
            if(
                reference['apiVersion'] == add_reference['apiVersion'] and
                reference['kind'] == add_reference['kind'] and
                reference['name'] == add_reference['name']
            ):
                if reference['uid'] == add_reference['uid']:
                    return
                await self.json_patch_status([{
                    "op": "test",
                    "path": f"/status/resources/{idx}/uid",
                    "value": reference['uid']
                }, {
                    "op": "replace",
                    "path": f"/status/resources/{idx}",
                    "value": add_reference
                }])
                return

        if 'resources' in self.status:
            await self.json_patch_status([{
                "op": "add",
                "path": "/status/resources/-",
                "value": add_reference
            }])
        else:
            await self.merge_patch_status({
                "resources": [add_reference]
            })

    async def delete_managed_resource(self, logger, reference):
        api_version = reference['apiVersion']
        kind = reference['kind']
        name = reference['name']

        try:
            await Anarchy.delete_resource(
                api_version=api_version,
                kind = kind,
                name = name,
                namespace = self.namespace
            )
            logger.info(f"Deleted {api_version} {kind} {name} in {self.namespace}")
        except kubernetes_asyncio.client.rest.ApiException as err:
            if err.status != 404:
                raise

    async def delete_managed_resources(self, logger):
        for reference in self.resource_references:
            await self.delete_managed_resource(logger=logger, reference=reference)

    async def get_resource_definitions(self, logger):
        async with aiofiles.tempfile.NamedTemporaryFile('w') as values_file:
            await values_file.write(
                yaml.safe_dump({
                    **self.spec,
                    "image": {
                        "pullPolicy": Anarchy.pod.spec.containers[0].image_pull_policy,
                        "repository": Anarchy.pod.spec.containers[0].image,
                    },
                    "namespace": {
                        "create": False,
                        "name": self.namespace,
                    }
                })
            )
            await values_file.flush()
            helm_chart_path = str(pathlib.Path(__file__).parent.resolve() / 'helm')
            proc = await asyncio.create_subprocess_exec(
                'helm', 'template', helm_chart_path, '--values', values_file.name,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            resource_definitions = list(yaml.safe_load_all(stdout))

            if stderr:
                logger.warning(f"Helm template error: {stderr}")

            for item in resource_definitions:
                item['metadata']['ownerReferences'] = [self.as_owner_reference()]

            return resource_definitions

    async def handle_create(self, logger):
        await self.manage_resources(logger=logger)
        logger.info(f"{self} created")

    async def handle_delete(self, logger):
        await self.delete_managed_resources(logger=logger)
        logger.info(f"{self} deleted")

    async def handle_resume(self, logger):
        await self.manage_resources(logger=logger)
        logger.info(f"{self} resumed")

    async def handle_update(self, logger):
        await self.manage_resources(logger=logger)
        logger.info(f"{self} updated")

    async def json_patch_status(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object_status(
            group = self.group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        self.update_from_definition(definition)

    async def merge_patch_status(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object_status(
            group = self.group,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = self.version,
            body = {"status": patch},
            _content_type = 'application/merge-patch+json',
        )
        self.update_from_definition(definition)

    async def manage_resource(self, definition, logger):
        api_version = definition['apiVersion']
        kind = definition['kind']
        name = definition['metadata']['name']
        namespace = definition['metadata']['namespace']
        plural = pluralize(kind.lower())

        if '/' in api_version:
            group, version = api_version.split('/')
        else:
            group = None
            version = api_version

        resource_state = await Anarchy.server_side_apply(definition)
        uid = resource_state['metadata']['uid']

        logger.info(f"Applied {api_version} {kind} {name} in {namespace} {uid}")

        reference = {
            "apiVersion": api_version,
            "kind": kind,
            "name": name,
            "uid": uid,
        }
        await self.add_resource_reference_to_status(reference)
        return reference

    async def manage_resources(self, logger):
        references = []
        resource_definitions = await self.get_resource_definitions(logger=logger)
        for definition in resource_definitions:
            references.append(
                await self.manage_resource(definition=definition, logger=logger)
            )

        status_patch = []
        for idx, reference in enumerate(self.resource_references):
            if reference not in references:
                self.delete_managed_resource(
                    api_version = reference['apiVersion'],
                    kind = reference['kind'],
                    logger = logger,
                    name = reference['name'],
                )
                status_patch = [
                    {
                        "op": "test",
                        "path": f"/status/resources/{idx}/uid",
                        "value": reference['uid'],
                    }, {
                        "op": "delete",
                        "path": "/status/resources/{idx}"
                    },
                    *status_patch
                ]
        if status_patch:
            await self.json_patch_status(status_patch)
