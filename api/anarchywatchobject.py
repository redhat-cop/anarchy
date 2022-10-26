import asyncio
import kubernetes_asyncio
import logging
import time

from anarchy import Anarchy
from anarchyobject import AnarchyObject

class AnarchyWatchObject(AnarchyObject):
    label_selector = None
    preload = False

    @classmethod
    def cache_remove(cls, name):
        return cls.cache.pop(name, None)

    @classmethod
    def handle_watch_deleted(cls, event_object):
        name = event_object['metadata']['name']
        if cls.cache_remove(name):
            logging.info(f"Watch cache removed {cls.__name__} {name}")

    @classmethod
    async def handle_watch_found(cls, event_object):
        if isinstance(event_object, dict):
            name = event_object['metadata']['name']
            resource_version = event_object['metadata']['resourceVersion']
        else:
            name = event_object.metadata.name
            resource_version = event_object.metadata.resource_version

        obj = cls.cache.get(name)
        if obj:
            if obj.resource_version == resource_version:
                logging.debug(f"Watch cache saw {obj} again")
            else:
                await obj.update_definition(event_object)
                logging.info(f"Watch cache updated {obj}")
        else:
            obj = cls(event_object)
            await obj.cache_put()
            logging.info(f"Watch cache added {obj}")

    @classmethod
    def watch_method(cls):
        return Anarchy.custom_objects_api.list_namespaced_custom_object

    @classmethod
    def watch_method_kwargs(cls):
        return dict(
            group = Anarchy.domain,
            label_selector = cls.label_selector,
            namespace = Anarchy.namespace,
            plural = cls.plural,
            version = Anarchy.version,
        )

    @classmethod
    async def get(cls, name):
        obj = cls.cache.get(name)
        if obj:
            return obj
        await cls.fetch(name)

    @classmethod
    async def on_shutdown(cls):
        cls.watch_task.cancel()
        await cls.watch_task

    @classmethod
    async def on_startup(cls):
        if cls.preload:
            await cls.preload_cache()
        cls.watch_task = asyncio.create_task(cls.watch_loop())

    @classmethod
    async def preload_cache(cls):
        _continue = None
        while True:
            object_list = await cls.watch_method()(
                **cls.watch_method_kwargs(),
                limit = 50,
                _continue = _continue,
            )

            items = object_list['items'] if isinstance(object_list, dict) else object_list.items
            for item in items:
                obj = cls(item)
                await obj.cache_put()
                logging.info(f"Preloaded cache {obj}")

            _continue = object_list.get('continue') if isinstance(object_list, dict) else object_list.metadata._continue
            if not _continue:
                break

    @classmethod
    async def watch(cls):
        watch = kubernetes_asyncio.watch.Watch()
        async for event in watch.stream(cls.watch_method(), **cls.watch_method_kwargs()):
            event_object = event['object']
            event_type = event['type']
            if event_type == 'ERROR':
                if event_object['kind'] == 'Status':
                    if event_object['reason'] in ('Expired', 'Gone'):
                        raise WatchRestartError(event_object['reason'].lower())
                    else:
                        raise WatchError(f"{event_obj['reason']} {event_obj['message']}")
                else:
                    raise WatchError(f"UKNOWN EVENT: {event}")
            elif event_type == 'DELETED':
                cls.handle_watch_deleted(event_object)
            else:
                await cls.handle_watch_found(event_object)

    @classmethod
    async def watch_loop(cls):
        while True:
            watch_start_time = time.time()
            try:
                logging.info(f"Watch {cls.__name__} starting")
                await cls.watch()
            except asyncio.CancelledError:
                logging.info(f"Watch {cls.__name__} exiting")
                return
            except Exception as e:
                if not isinstance(e, kubernetes_asyncio.client.exceptions.ApiException) and e.status == 410:
                    logging.exception(f"Watch {cls.__name__} Exception")

                # If watch is repeatedly crashing then backoff retry
                watch_duration = time.time() - watch_start_time
                if watch_duration < 60:
                    asyncio.sleep(60 - watch_duration)

                logging.info(f"Watch {cls.__name__} restarting")

    async def cache_put(self):
        self.cache[self.name] = self

class WatchError(Exception):
    pass

class WatchRestartError(Exception):
    pass
