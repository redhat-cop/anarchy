import logging

from copy import deepcopy

from anarchy import Anarchy

class AnarchyObject:
    api_version = Anarchy.api_version

    @classmethod
    async def create(cls, definition):
        definition['apiVersion'] = Anarchy.api_version
        definition['kind'] = cls.__name__
        definition['metadata']['namespace'] = Anarchy.namespace
        definition = await Anarchy.custom_objects_api.create_namespaced_custom_object(
            body = definition,
            group = Anarchy.domain,
            namespace = Anarchy.namespace,
            plural = cls.plural,
            version = Anarchy.version,
        )
        return cls(definition)

    @classmethod
    async def get(cls, name):
        return await cls.fetch(name)

    @classmethod
    async def fetch(cls, name):
        definition = await Anarchy.custom_objects_api.get_namespaced_custom_object(
            group = Anarchy.domain,
            name = name,
            namespace = Anarchy.namespace,
            plural = cls.plural,
            version = Anarchy.version,
        )
        return cls(definition)

    @classmethod
    async def list(cls, label_selector=None):
        item_list = await Anarchy.custom_objects_api.list_namespaced_custom_object(
            group = Anarchy.domain,
            label_selector = label_selector,
            namespace = Anarchy.namespace,
            plural = cls.plural,
            version = Anarchy.version
        )
        return [cls(item) for item in item_list['items']]


    def __init__(self, definition):
        self.definition = definition

    def __str__(self):
        return f"{self.kind} {self.name} [{self.resource_version}]"

    @property
    def labels(self):
        return self.metadata.get('labels', {})

    @property
    def metadata(self):
        return self.definition['metadata']

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
    def spec(self):
        return self.definition['spec']

    @property
    def status(self):
        return self.definition.get('status', {})

    @property
    def uid(self):
        return self.metadata['uid']

    def as_dict(self):
        return self.definition

    def as_owner_ref(self):
        return {
            "apiVersion": Anarchy.api_version,
            "controller": True,
            "kind": self.kind,
            "name": self.name,
            "uid": self.uid,
        }

    def as_reference(self):
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
        }

    async def merge_patch(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
            body = patch,
            _content_type = 'application/merge-patch+json',
        )
        await self.update_definition(definition)

    async def merge_patch_status(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object_status(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
            body = {"status": patch},
            _content_type = 'application/merge-patch+json',
        )
        await self.update_definition(definition)

    async def json_patch(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        await self.update_definition(definition)

    async def json_patch_status(self, patch):
        definition = await Anarchy.custom_objects_api.patch_namespaced_custom_object_status(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
            body = patch,
            _content_type = 'application/json-patch+json',
        )
        await self.update_definition(definition)

    async def refetch(self):
        definition = await Anarchy.custom_objects_api.get_namespaced_custom_object(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
        )
        await self.update_definition(definition)

    async def replace(self, updated_definition):
        definition = await Anarchy.custom_objects_api.replace_namespaced_custom_object(
            body = updated_definition,
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
        )
        await self.update_definition(definition)

    async def update_definition(self, definition):
        self.definition = definition
