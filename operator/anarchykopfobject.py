import kopf
import kubernetes_asyncio

from anarchy import Anarchy

class AnarchyKopfObject:
    @classmethod
    def from_definition(cls, definition):
        metadata = definition['metadata']
        return cls(
            annotations = metadata.get('annotations', {}),
            labels = metadata.get('labels', {}),
            meta = metadata,
            name = metadata['name'],
            namespace = metadata['namespace'],
            spec = definition['spec'],
            status = definition.get('status', {}),
            uid = metadata['uid'],
        )

    @classmethod
    async def fetch(cls, name):
        return cls.from_definition(await cls.fetch_definition(name))

    @classmethod
    async def fetch_definition(cls, name):
        return await Anarchy.custom_objects_api.get_namespaced_custom_object(
            group = Anarchy.domain,
            name = name,
            namespace = Anarchy.namespace,
            plural = cls.plural,
            version = Anarchy.version,
        )

    def __init__(self, annotations, labels, meta, name, namespace, spec, status, uid, **_):
        self.annotations = annotations
        self.labels = labels
        self.metadata = meta
        self.name = name
        self.namespace = namespace
        self.spec = spec
        self.status = status
        self.uid = uid

    def __str__(self):
        return f"{self.kind} {self.name}"

    @property
    def creation_timestamp(self):
        return self.metadata['creationTimestamp']

    @property
    def deletion_timestamp(self):
        return self.metadata.get('deletionTimestamp')

    @property
    def finalizers(self):
        return self.metadata.get('finalizers', [])

    @property
    def is_deleted(self):
        """
        Object should be treated as functionally deleted.
        Some other operator may be holding it with a finalizer, but Anarchy is done with it.
        """
        return self.is_deleting and Anarchy.domain not in self.finalizers and Anarchy.deleting_label not in self.finalizers

    @property
    def is_deleting(self):
        return 'deletionTimestamp' in self.metadata

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
            "apiVersion": Anarchy.api_version,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
        }

    def has_anarchy_finalizer(self):
        return Anarchy.domain in self.finalizers

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

    async def delete(self):
        definition = await Anarchy.custom_objects_api.delete_namespaced_custom_object(
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
        )
        self.update_from_definition(definition)

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
        self.update_from_definition(definition)

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
        self.update_from_definition(definition)

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
        self.update_from_definition(definition)

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
        self.update_from_definition(definition)

    async def refresh(self):
        definition = await self.__class__.fetch_definition(self.name)
        self.update_from_definition(definition)

    async def raise_error_if_still_exists(self, msg):
        try:
            await self.refresh()
            raise kopf.TemporaryError(msg, delay=60)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise
