import logging

from anarchy import Anarchy
from anarchywatchobject import AnarchyWatchObject

class AnarchyRunner(AnarchyWatchObject):
    cache = {}
    kind = 'AnarchyRunner'
    plural = 'anarchyrunners'
    preload = True

    @classmethod
    async def on_startup(cls):
        if Anarchy.running_all_in_one:
            await cls.on_startup_running_all_in_one()
        else:
            await super().on_startup()

    @classmethod
    async def on_startup_running_all_in_one(cls):
        default = cls({
            "apiVersion": Anarchy.api_version,
            "kind": "AnarchyRunner",
            "metadata": {
                "name": "default",
                "namespace": Anarchy.namespace,
                "resourceVersion": 0,
                "uid": "00000000-0000-0000-0000-000000000000",
            },
            "spec": {
                "maxReplicas": 1,
                "minReplicas": 1,
            }
        })
        await cls.cache_put(default)
        logging.info(f"Cache preloaded {default}")
