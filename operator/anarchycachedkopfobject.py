from anarchy import Anarchy
from anarchykopfobject import AnarchyKopfObject

class AnarchyCachedKopfObject(AnarchyKopfObject):
    @classmethod
    async def get(cls, name):
        obj = cls.cache.get(name)
        if obj:
            return obj
        obj = await cls.fetch(name)
        if not obj.ignore:
            cls.cache[name] = obj
        return obj

    @classmethod
    def load(cls, definition=None, **kwargs):
        if definition:
            return cls.load_definition(definition)
        return cls.load_kopf_object(**kwargs)

    @classmethod
    def load_definition(cls, definition):
        metadata = definition['metadata']
        name = metadata['name']
        obj = cls.cache.get(name)
        if obj:
            obj.update_from_definition(definition)
        else:
            obj = cls.from_definition(definition)
            cls.cache[name] = obj
        return obj

    @classmethod
    def load_kopf_object(cls, name, **kwargs):
        obj = cls.cache.get(name)
        if obj:
            obj.update(**kwargs)
        else:
            obj = cls(name=name, **kwargs)
            cls.cache[name] = obj
        return obj

    def remove_from_cache(self):
        self.cache.pop(self.name, None)
