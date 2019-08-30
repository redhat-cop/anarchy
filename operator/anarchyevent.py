import logging
import random
import threading

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchyEvent(object):

    active_cache = {}
    active_cache_lock = threading.Lock()

    @staticmethod
    def redistribute(runtime):
        runner_label = runtime.operator_domain + '/anarchy-runner'

        if not AnarchyEvent.active_cache:
            pass
        elif not runtime.anarchy_runners:
            logger.warning('Unable to redistribute events, no runners')
            return
        AnarchyEvent.active_cache_lock.acquire()
        try:
            for anarchy_event in AnarchyEvent.active_cache.values():
                runner_name = anarchy_event.metadata.get('labels', {}).get(runner_label, None)
                if runner_name not in runtime.anarchy_runners:
                    anarchy_runner = random.choice(tuple(runtime.anarchy_runners))
                    runtime.custom_objects_api.patch_namespaced_custom_object(
                        runtime.operator_domain, 'v1', anarchy_event.namespace,
                        'anarchyevents', anarchy_event.name,
                        {
                            'metadata': {
                                'labels': { runner_label: anarchy_runner }
                            }
                        }
                    )
        finally:
            AnarchyEvent.active_cache_lock.release()

    @staticmethod
    def register(anarchy_event):
        if anarchy_event.is_active:
            AnarchyEvent.active_cache_lock.acquire()
            AnarchyEvent.active_cache[anarchy_event.name] = anarchy_event
            AnarchyEvent.active_cache_lock.release()
        elif anarchy_event.name in AnarchyEvent.active_cache:
            AnarchyEvent.active_cache_lock.acquire()
            del AnarchyEvent.active_cache[anarchy_event.name]
            AnarchyEvent.active_cache_lock.release()

    @staticmethod
    def unregister(anarchy_event):
        if anarchy_event.name in AnarchyEvent.active_cache:
            AnarchyEvent.active_cache_lock.acquire()
            del AnarchyEvent.active_cache[anarchy_event.name]
            AnarchyEvent.active_cache_lock.release()

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', {})
        self.sanity_check()

    def sanity_check(self):
        # FIXME
        pass

    @property
    def is_active(self):
        return self.state in ['new', 'retry']

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def namespace_name(self):
        return self.metadata['namespace'] + '/' + self.metadata['name']

    @property
    def state(self):
        if self.status:
            return self.status['state']
        else:
            return 'new'

    @property
    def uid(self):
        return self.metadata['uid']
