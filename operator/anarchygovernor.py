from anarchyutil import deep_update, parse_time_interval
from datetime import datetime, timedelta

import copy
import kopf
import kubernetes
import logging
import threading
import time
import urllib3

operator_logger = logging.getLogger('operator')

class AnarchyGovernor(object):
    """AnarchyGovernor class"""

    class EventHandler(object):
        def __init__(self, name, spec):
            self.name = name
            self.spec = spec

        @property
        def post_tasks(self):
            return self.spec.get('postTasks', [])

        @property
        def pre_tasks(self):
            return self.spec.get('preTasks', [])

        @property
        def roles(self):
            return self.spec.get('roles', [])

        @property
        def tasks(self):
            return self.spec.get('tasks', [])

        @property
        def vars(self):
            return self.spec.get('vars', {})

        @property
        def var_secrets(self):
            return self.spec.get('varSecrets', [])

    class ActionConfig(object):
        def __init__(self, name, spec, governor):
            self.name = name
            self.spec = spec
            self.governor = governor
            self.callback_handlers = {}
            for callback_name, handler_spec in spec.get('callbackHandlers', {}).items():
                self.callback_handlers[callback_name] = AnarchyGovernor.EventHandler(callback_name, handler_spec)

        @property
        def callback_name_parameter(self):
            """
            Optional configuration setting to allow the action callback name to be
            specified in the callback data rather than as a URL component to the
            API.
            """
            return self.spec.get('callbackNameParameter', None)

        @property
        def finish_on_successful_run(self):
            """
            Boolean flag indicating whether actions using this action config are
            automatically marked finished after a successful run. Defaults to True.
            """
            return self.spec.get('finishOnSuccessfulRun', True)

        @property
        def post_tasks(self):
            return self.spec.get('postTasks', [])

        @property
        def pre_tasks(self):
            return self.spec.get('preTasks', [])

        @property
        def roles(self):
            return self.spec.get('roles', [])

        @property
        def tasks(self):
            return self.spec.get('tasks', [])

        @property
        def vars(self):
            return self.spec.get('vars', {})

        @property
        def var_secrets(self):
            return self.spec.get('varSecrets', [])

        def callback_handler(self, name):
            if name in self.callback_handlers:
                return self.callback_handlers[name]
            elif '*' in self.callback_handlers:
                return self.callback_handlers['*']
            else:
                return None

    governors = {}
    register_lock = threading.Lock()

    @staticmethod
    def get(name):
        return AnarchyGovernor.governors.get(name)

    @staticmethod
    def preload(anarchy_runtime):
        '''
        Load all AnarchyGovernors

        This method is used during start-up to ensure that all AnarchyGovernor definitions are
        loaded before processing starts.
        '''
        operator_logger.info("Starting AnarchyGovernor preload")
        for resource_object in anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchygovernors'
        ).get('items', []):
            AnarchyGovernor.register(resource_object=resource_object)

    @staticmethod
    def register(
        logger=None,
        resource_object=None,
    ):
        '''
        Register AnarchyGovernor or refresh definition of already registered AnarchyGovernor.
        '''
        name = resource_object['metadata']['name']
        with AnarchyGovernor.register_lock:
            governor = AnarchyGovernor.governors.get(name)
            if governor:
                governor.__init__(logger=logger, resource_object=resource_object)
                governor.logger.debug("Refreshed AnarchyGovernor")
            else:
                governor = AnarchyGovernor(logger=logger, resource_object=resource_object)
                AnarchyGovernor.governors[name] = governor
                governor.logger.info("Registered AnarchyGovernor")
            return governor

    @staticmethod
    def unregister(name):
        '''
        Unregister AnarchyGovernor 
        '''
        with AnarchyGovernor.register_lock:
            if name in AnarchyGovernor.governors:
                governor = AnarchyGovernor.governors.pop(name)
                governor.local_logger.info("Unregistered AnarchyGovernor")

    @staticmethod
    def watch(anarchy_runtime):
        '''
        Watch AnarchyGovernors and keep definitions synchronized

        This watch is independent of the kopf watch and is used to keep governor definitions updated
        even when the pod is not the active peer.
        '''
        while True:
            try:
                AnarchyGovernor.__watch(anarchy_runtime)
            except kubernetes.client.rest.ApiException as e:
                if e.status == 410:
                    # 410 Gone, simply reset watch
                    operator_logger.warning(
                        "Restarting AnarchyGovernor watch",
                        extra = dict(reason = str(e))
                    )
                else:
                    operator_logger.exception("ApiException in AnarchyGovernor watch")
                    time.sleep(5)
            except urllib3.exceptions.ProtocolError as e:
                operator_logger.warning(
                    "ProtocolError in AnarchyGovernor watch",
                    extra = dict(reason = str(e))
                )
                time.sleep(5)
            except Exception as e:
                operator_logger.exception("Exception in AnarchyGovernor watch")
                time.sleep(5)

    @staticmethod
    def __watch(anarchy_runtime):
        operator_logger.info("Starting AnarchyGovernor watch")
        for event in kubernetes.watch.Watch().stream(
            anarchy_runtime.custom_objects_api.list_namespaced_custom_object,
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchygovernors'
        ):
            obj = event.get('object')
            if not obj:
                continue

            if event['type'] == 'ERROR' \
            and obj['kind'] == 'Status':
                if obj['status'] == 'Failure':
                    if obj['reason'] in ('Expired', 'Gone'):
                        operator_logger.info(
                            "AnarchyGovernor watch restarting",
                            extra = dict(event = event)
                        )
                        return
                    else:
                        operator_logger.error(
                            "AnarchyGovernor watch error",
                            extra = dict(event = event)
                        )
                        time.sleep(5)
                        return

            if obj.get('apiVersion') == anarchy_runtime.api_group_version:
                if event['type'] == 'DELETED':
                    AnarchyGovernor.unregister(name=obj['metadata']['name'])
                else:
                    AnarchyGovernor.register(resource_object=obj)

    def __init__(self, resource_object, logger=None):
        self.api_version = resource_object['apiVersion']
        self.kind = resource_object['kind']
        self.metadata = resource_object['metadata']
        self.spec = resource_object['spec']
        self.status = resource_object.get('status', {})

        subject_event_handlers = {}
        for event_name, handler_spec in self.spec.get('subjectEventHandlers', {}).items():
            subject_event_handlers[event_name] = AnarchyGovernor.EventHandler(event_name, handler_spec)
        self.subject_event_handlers = subject_event_handlers

        actions = {}
        for action_name, action_spec in self.spec.get('actions', {}).items():
            actions[action_name] = AnarchyGovernor.ActionConfig(action_name, action_spec, self)
        self.actions = actions

        self.local_logger = kopf.LocalObjectLogger(
            body = resource_object,
            settings = kopf.OperatorSettings(),
        )
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = self.local_logger

    @property
    def ansible_galaxy_requirements(self):
        return self.spec.get('ansibleGalaxyRequirements', None)

    @property
    def api_group_version(self):
        return f"{self.api_group}/{self.api_version}"

    @property
    def callback_name_parameter(self):
        return self.spec.get('callbackNameParameter', 'event')

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def parameter_secrets(self):
        return self.spec.get('parameterSecrets', [])

    @property
    def python_requirements(self):
        return self.spec.get('pythonRequirements', None)

    @property
    def reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            name = self.name,
            namespace = self.namespace,
            uid = self.uid,
        )

    @property
    def remove_finished_actions_after(self):
        time_interval = self.spec.get('removeFinishedActions', {}).get('after')
        if time_interval:
            return parse_time_interval(time_interval)
        else:
            return timedelta(days=1)

    @property
    def remove_successful_runs_after(self):
        time_interval = self.spec.get('removeSuccessfulRuns', {}).get('after')
        if time_interval:
            return parse_time_interval(time_interval)
        else:
            return timedelta(days=1)

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def action_config(self, name):
        if name in self.actions:
            return self.actions[name]
        elif '*' in self.actions:
            wildcard_action = self.actions['*']
            return AnarchyGovernor.ActionConfig(name, wildcard_action.spec, self)
        else:
            return None

    def get_context_vars(self, obj, context, anarchy_runtime):
        '''
        Get variables for a context object such as the governor, subject, action config, etc
        '''
        if not obj:
            return
        merged_vars = copy.deepcopy(obj.vars)
        for var_secret in obj.var_secrets:
            secret_name = var_secret.get('name')
            secret_namespace = var_secret.get('namespace', self.namespace)
            if secret_name:
                try:
                    secret_data = anarchy_runtime.get_secret_data(secret_name, secret_namespace)
                    var_name = var_secret.get('var', None)
                    if var_name:
                        deep_update(merged_vars, {var_name: secret_data})
                    else:
                        deep_update(merged_vars, secret_data)
                except kubernetes.client.rest.ApiException as e:
                    if e.status != 404:
                        raise
                    self.logger.warning(
                        'varSecrets references missing secret',
                        extra = dict(
                            context = context,
                            secret = dict(
                                apiVersion = 'v1',
                                kind = 'Secret',
                                name = secret_name,
                                namespace = secret_namespace,
                            )
                        )
                    )
            else:
                self.logger.warning(
                    'varSecrets has entry with no name',
                    extra = dict(
                        context = context
                    )
                )
        return merged_vars

    def run_ansible(
        self,
        anarchy_runtime,
        run_config,
        run_vars,
        context,
        anarchy_subject,
        anarchy_action,
        event_name=None,
    ):
        run_spec = {
            'preTasks': run_config.pre_tasks,
            'roles': run_config.roles,
            'tasks': run_config.tasks,
            'postTasks': run_config.post_tasks,
        }
        if self.ansible_galaxy_requirements:
            run_spec['ansibleGalaxyRequirements'] = self.ansible_galaxy_requirements
        if self.python_requirements:
            run_spec['pythonRequirements'] = self.python_requirements

        # Loop through each object in the run context setting reference and collecting vars
        # The variables are both collected into an aggregate with overrides and also preserved
        # for each context so that an execution can explictly reference a value otherwise overridden
        # at another level.
        collected_run_vars = {}
        for context_item in context:
            name, obj = context_item
            context_spec = {
                attr: getattr(obj, attr) for attr in ('kind', 'name', 'namespace', 'uid') if hasattr(obj, attr)
            }
            if hasattr(obj, 'uid'):
                context_spec['apiVersion'] = anarchy_runtime.api_group_version
            context_vars = self.get_context_vars(obj, context_spec, anarchy_runtime)
            if context_vars:
                context_spec['vars'] = context_vars
            run_spec[name] = context_spec
            collected_run_vars.update(context_vars)

        # Any passed run_vars override any values collected from configuration
        collected_run_vars.update(run_vars)

        # Variables set for this run execution
        run_spec['vars'] = collected_run_vars

        labels = {
            anarchy_runtime.governor_label: self.name,
            anarchy_runtime.runner_label: 'queued',
            anarchy_runtime.subject_label: anarchy_subject.name,
        }
        if event_name:
            labels[anarchy_runtime.event_label] = event_name

        if anarchy_action:
            if event_name:
                generate_name = f"{anarchy_action.name}-{event_name}-"
            else:
                generate_name = f"{anarchy_action.name}-"
            labels[anarchy_runtime.action_label] = anarchy_action.name
            owner_reference = { 'controller': True, **anarchy_action.reference }
        else:
            generate_name = f"{anarchy_subject.name}-{event_name}-"
            owner_reference = { 'controller': True, **anarchy_subject.reference }

        anarchy_run = anarchy_runtime.custom_objects_api.create_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyruns',
            {
                'apiVersion': anarchy_runtime.api_group_version,
                'kind': 'AnarchyRun',
                'metadata': {
                    'generateName': generate_name,
                    'labels': labels,
                    'namespace': anarchy_runtime.operator_namespace,
                    'ownerReferences': [owner_reference]
                },
                'spec': run_spec
            }
        )
        anarchy_run_meta = anarchy_run['metadata']
        anarchy_run_name = anarchy_run_meta['name']

    def subject_event_handler(self, name):
        if name in self.subject_event_handlers:
            return self.subject_event_handlers[name]
        elif '*' in self.subject_event_handlers:
            return self.subject_event_handlers['*']
        else:
            return None

    def to_dict(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            metadata = copy.deepcopy(self.metadata),
            spec = copy.deepcopy(self.spec),
            status = copy.deepcopy(self.status),
        )
