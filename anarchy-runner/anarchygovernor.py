from anarchyrunobject import AnarchyRunObject

class AnarchyGovernor(AnarchyRunObject):
    kind = 'AnarchyGovernor'

    @property
    def ansible_galaxy_requirements(self):
        return self.spec.get('ansibleGalaxyRequirements')

    @property
    def python_requirements(self):
        return self.spec.get('pythonRequirements')

    def get_action_config(self, action_name):
        try:
            return Handler(self.spec['actions'][action_name])
        except KeyError:
            raise Exception(f"{self} has no {action_name} action")

    def get_action_callback_handler(self, action_name, callback_name):
        try:
            action_definition = self.spec['actions'][action_name]
            callback_name_parameter = action_definition.get('callbackNameParameter')
            return Handler({
                "callbackNameParameter": callback_name_parameter,
                **action_definition['callbackHandlers'][callback_name],
            })
        except KeyError:
            raise Exception(f"{self} has no handler for {action_name} action {callback_name} callback")

    def get_subject_event_handler(self, event_name):
        try:
            return Handler(self.spec['subjectEventHandlers'][event_name])
        except KeyError:
            raise Exception(f"{self} has no configuration for subject event {event_name}")

class Handler:
    def __init__(self, definition):
        self.pre_tasks = definition.get('preTasks', [])
        self.roles = definition.get('roles', [])
        self.tasks = definition.get('tasks', [])
        self.post_tasks = definition.get('postTasks', [])
        self.vars = definition.get('vars', {})
        self.callback_name_parameter = definition.get('callbackNameParameter')
