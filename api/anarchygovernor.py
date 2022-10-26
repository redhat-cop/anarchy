from copy import deepcopy

from anarchywatchobject import AnarchyWatchObject
from varsecret import VarSecretMixin

class AnarchyGovernor(VarSecretMixin, AnarchyWatchObject):
    cache = {}
    kind = 'AnarchyGovernor'
    plural = 'anarchygovernors'
    preload = True

    @property
    def action_configs(self):
        return self.spec.get('actions', {})

    @property
    def subject_event_handlers(self):
        return self.spec.get('subjectEventHandlers', {})

    def get_action_config(self, name):
        definition = self.action_configs.get(name)
        if definition:
            return ActionConfig(name, definition)

    def get_subject_event_handler(self, name):
        definition = self.subject_event_handlers.get(name)
        if definition:
            return SubjectEventHandler(name, definition)

    def get_action_callback_handler(self, action_name, callback_name):
        action_config = self.get_action_config(action_name)
        if action_config:
            return action_config.get_callback_handler(callback_name)

    async def export(self):
        """
        Export definition while reading all var secrets into vars.
        """
        ret = await super().export()
        spec = ret['spec']
        for action_name, action_definition in spec.get('actions', {}).items():
            action_config = ActionConfig(action_name, action_definition)
            action_definition['vars'] = await action_config.get_vars()
            action_definition.pop('varSecrets', None)
            for callback_name, callback_definition in action_definition.get('callbackHandlers', {}).items():
                action_callback_handler = ActionCallbackHandler(callback_name, callback_definition)
                callback_definition['vars'] = await action_callback_handler.get_vars()
                callback_definition.pop('varSecrets', None)
        for name, definition in spec.get('subjectEventHandlers', {}).items():
            subject_event_handler = SubjectEventHandler(name, definition)
            definition['vars'] = await subject_event_handler.get_vars()
            definition.pop('varSecrets', None)
        return ret

class RunConfig(VarSecretMixin):
    def __init__(self, name, definition):
        self.definition = definition
        self.name = name

    @property
    def has_var_secrets(self):
        return 'varSecrets' in self.definition

    @property
    def vars(self):
        return self.definition.get('vars', {})

    @property
    def var_secrets(self):
        return [
            VarSecret(item) for item in self.definition.get('varSecrets', [])
        ]

class ActionConfig(RunConfig):
    @property
    def callback_handlers(self):
        return self.definition.get('callbackHandlers', {})

    @property
    def callback_name_parameter(self):
        return self.definition.get('callbackNameParameter')

    def get_action_config(self, name):
        definition = self.callback_handlers.get(name)
        if definition:
            return ActionCallbackHandler(name, definition)

    def get_callback_handler(self, name):
        definition = self.callback_handlers.get(name)
        if definition:
            return ActionCallbackHandler(name, definition)

class ActionCallbackHandler(RunConfig):
    pass

class SubjectEventHandler(RunConfig):
    pass
