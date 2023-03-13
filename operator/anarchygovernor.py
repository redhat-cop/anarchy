import kopf
import logging
import pytimeparse

from datetime import timedelta

from anarchy import Anarchy

class AnarchyGovernor:
    cache = {}
    kind = 'AnarchyGovernor'
    plural = 'anarchygovernors'

    @classmethod
    def get(cls, name):
        anarchy_governor = cls.cache.get(name)
        if not anarchy_governor:
            raise kopf.TemporaryError(f"AnarchyGovernor not found: {name}", delay=60)
        return anarchy_governor

    @classmethod
    async def handle_event(cls, event):
        obj = event.get('object')
        if obj.get('apiVersion') != Anarchy.api_version:
            logging.warning(f"AnarchyGovernor unknown event: {event}")
            return

        event_type = event['type']
        name = obj['metadata']['name']
        resource_version = obj['metadata']['resourceVersion']

        if event_type == 'DELETED':
            anarchy_governor = cls.cache.pop(name, None)
            if anarchy_governor:
                logging.info(f"Cache removed {anarchy_governor}")
            else:
                logging.warning(f"Cache did not have {anarchy_governor} on delete?")
        else:
            anarchy_governor = cls.cache.get(name)
            if anarchy_governor:
                if anarchy_governor.resource_version == resource_version:
                    logging.debug(f"Already cached {anarchy_governor}")
                else:
                    anarchy_governor.definition = obj
                    logging.info(f"Cache updated {anarchy_governor}")
            else:
                anarchy_governor = AnarchyGovernor(obj)
                cls.cache[name] = anarchy_governor
                logging.info(f"Cache loaded {anarchy_governor}")


    @classmethod
    async def preload(cls):
        _continue = None
        while True:
            anarchy_governor_list = await Anarchy.custom_objects_api.list_namespaced_custom_object(
                group = Anarchy.domain,
                namespace = Anarchy.namespace,
                plural = 'anarchygovernors',
                version = Anarchy.version,
                limit = 50,
                _continue = _continue,
            )
            for definition in anarchy_governor_list['items']:
                anarchy_governor = AnarchyGovernor(definition)
                cls.cache[anarchy_governor.name] = anarchy_governor
                logging.info(f"Cache preloaded {anarchy_governor}")
            _continue = anarchy_governor_list['metadata'].get('continue')
            if not _continue:
                break

    def __init__(self, definition):
        self.definition = definition

    def __str__(self):
        return f"AnarchyGovernor {self.name} [{self.resource_version}]"

    @property
    def action_configs(self):
        return self.spec.get('actions', {})

    @property
    def ansible_galaxy_requirements(self):
        return self.spec.get('ansibleGalaxyRequirements', [])

    @property
    def api_version(self):
        return self.definition['api_version']

    @property
    def has_create_handler(self):
        return 'create' in self.subject_event_handlers

    @property
    def has_delete_handler(self):
        return 'delete' in self.subject_event_handlers

    @property
    def has_update_handler(self):
        return 'update' in self.subject_event_handlers

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
    def python_requirements(self):
        return self.spec.get('pythonRequirements', None)

    @property
    def remove_finished_actions_after_seconds(self):
        time_interval = self.spec.get('removeFinishedActions', {}).get('after')
        if time_interval:
            return pytimeparse.parse(time_interval)
        else:
            return pytimeparse.parse('1d')

    @property
    def remove_finished_actions_after_timedelta(self):
        return timedelta(seconds=self.remove_finished_actions_after_seconds)

    @property
    def remove_successful_runs_after_seconds(self):
        time_interval = self.spec.get('removeSuccessfulRuns', {}).get('after')
        if time_interval:
            return pytimeparse.parse(time_interval)
        else:
            return pytimeparse.parse('1d')

    @property
    def remove_successful_runs_after_timedelta(self):
        return timedelta(seconds=self.remove_successful_runs_after_seconds)

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def spec(self):
        return self.definition['spec']

    @property
    def subject_event_handlers(self):
        return self.spec['subjectEventHandlers']

    @property
    def supported_actions(self):
        ret = {}
        for k, v in self.action_configs.items():
            ret[k] = {}
            for f in ('description', 'timeEstimate'):
                if v.get(f):
                    ret[k][f] = v[f]
        return ret

    @property
    def uid(self):
        return self.metadata['uid']

    def as_reference(self):
        return {
            "apiVersion": Anarchy.api_version,
            "kind": "AnarchyGovernor",
            "name": self.name,
            "namespace": self.namespace,
            "uid": self.uid,
        }

    def get_action_config(self, name):
        definition = self.action_configs.get(name)
        if definition:
            return ActionConfig(name, definition)

    def has_action(self, name):
        return name in self.spec.get('actions', {})

class RunConfig:
    def __init__(self, name, definition):
        self.definition = definition
        self.name = name

class ActionConfig(RunConfig):
    @property
    def finish_on_successful_run(self):
        return self.definition.get('finishOnSuccessfulRun', True)

    @property
    def has_callback_handlers(self):
        return True if self.definition.get('callbackHandlers') else False
