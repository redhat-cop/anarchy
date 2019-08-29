import logging
import random

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchyEvent(object):

    active_cache = {}

    @staticmethod
    def redistribute(runtime):
        runner_label = runtime.operator_domain + '/anarchy-runner'

        if not AnarchyEvent.active_cache:
            pass
        elif not runtime.anarchy_runners:
            logger.warning('Unable to redistribute events, no runners')
            return
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

    @staticmethod
    def register(anarchy_event):
        if anarchy_event.is_active:
            AnarchyEvent.active_cache[anarchy_event.name] = anarchy_event
        elif anarchy_event.name in AnarchyEvent.active_cache:
            del AnarchyEvent.active_cache[anarchy_event.name]

    @staticmethod
    def unregister(anarchy_event):
        if anarchy_event.name in AnarchyEvent.active_cache:
            del AnarchyEvent.active_cache[anarchy_event.name]

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
