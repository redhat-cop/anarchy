from anarchyutil import parse_time_interval
from datetime import datetime, timedelta
import copy
import jinja2
import json
import kubernetes
import logging
import os
import six

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

    # AnarchyGovernor cache
    cache = {}

    @staticmethod
    def cleanup(runtime):
        for governor in list(AnarchyGovernor.cache.values()):
            governor.cleanup_actions(runtime)
            governor.cleanup_runs(runtime)

    @staticmethod
    def get(name):
        return AnarchyGovernor.cache.get(name, None)

    @staticmethod
    def init(runtime):
        '''
        Get initial list of AnarchyGovernors.

        This method is used during start-up to ensure that all AnarchyGovernor definitions are
        loaded before processing starts.
        '''
        for resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchygovernors'
        ).get('items', []):
            AnarchyGovernor.register(resource)

    @staticmethod
    def register(resource):
        name = resource['metadata']['name']
        governor = AnarchyGovernor.cache.get(name)
        if governor:
            operator_logger.info("Refreshed AnarchyGovernor %s", governor.name)
            governor.refresh_from_resource(resource)
        else:
            governor = AnarchyGovernor(resource)
            AnarchyGovernor.cache[name] = governor
            operator_logger.info("Registered AnarchyGovernor %s", governor.name)
        return governor

    @staticmethod
    def unregister(governor):
        name = governor.name if isinstance(governor, AnarchyGovernor) else governor
        if name in AnarchyGovernor.cache:
            AnarchyGovernor.cache.pop(name)
            operator_logger.info("Unregistered AnarchyGovernor %s", name)

    @staticmethod
    def watch(runtime):
        '''
        Watch AnarchyGovernors and keep definitions synchronized

        This watch is independent of the kopf watch and is used to keep governor definitions updated
        even when the pod is not the active peer.
        '''
        for event in kubernetes.watch.Watch().stream(
            runtime.custom_objects_api.list_namespaced_custom_object,
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchygovernors'
        ):
            obj = event.get('object')

            if event['type'] == 'ERROR' \
            and obj['kind'] == 'Status':
                if obj['status'] == 'Failure':
                    if obj['reason'] in ('Expired', 'Gone'):
                        operator_logger.info('AnarchyGovernor watch restarting, reason %s', obj['reason'])
                        return
                    else:
                        raise Exception("AnarchyGovernor watch failure: reason {}, message {}", obj['reason'], obj['message'])

            if obj and obj.get('apiVersion') == runtime.api_group_version:
                if event['type'] in ('ADDED', 'MODIFIED', None):
                    AnarchyGovernor.register(obj)
                elif event['type'] == 'DELETED':
                    AnarchyGovernor.unregister(obj['metadata']['name'])

    def __init__(self, resource):
        self.refresh_from_resource(resource)

    def set_subject_event_handlers(self, event_handlers):
        self.subject_event_handlers = {}
        for event_name, handler_spec in event_handlers.items():
            self.subject_event_handlers[event_name] = AnarchyGovernor.EventHandler(event_name, handler_spec)

    def sanity_check(self):
        # FIXME
        pass

    @property
    def ansible_galaxy_requirements(self):
        return self.spec.get('ansibleGalaxyRequirements', None)

    @property
    def api(self):
        return AnarchyAPI.get(self.spec.get('api', None))

    @property
    def callback_name_parameter(self):
        return self.spec.get('callbackNameParameter', 'event')

    @property
    def kind(self):
        return 'AnarchyGovernor'

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
    def remove_successful_actions_after(self):
        time_interval = self.spec.get('removeSuccessfulActions', {}).get('after')
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

    def cleanup_actions(self, runtime):
        time_interval = self.remove_successful_actions_after
        if not isinstance(time_interval, timedelta):
            return
        for action_resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions',
            label_selector='{}={},{}'.format(runtime.governor_label, self.name, runtime.run_label)
        ).get('items', []):
            action_name = action_resource['metadata']['name']
            run_name = action_resource.get('status', {}).get('runRef', {}).get('name')
            run_scheduled_timestamp = action_resource.get('status', {}).get('runScheduled')
            if not run_name or not run_scheduled_timestamp:
                operator_logger.warning(
                    'AnarchyAction %s has label %s but status does not have runRef or runScheduled',
                    action_name, runtime.run_label
                )
                continue

            run_scheduled_datetime = datetime.strptime(run_scheduled_timestamp, '%Y-%m-%dT%H:%M:%SZ')

            # If run has not be scheduled longer ago than the interval then do not delete
            if run_scheduled_datetime + time_interval > datetime.utcnow():
                continue

            try:
                run_resource = runtime.custom_objects_api.get_namespaced_custom_object(
                    runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns', run_name
                )
                # If run has not posted a successful result longer ago than the interval then do not delete
                if run_resource['metadata']['labels'][runtime.runner_label] != 'successful':
                    continue

                # If run has not posted a result longer ago than the interval then do not delete
                run_post_datetime = datetime.strptime(run_resource['spec']['runPostTimestamp'], '%Y-%m-%dT%H:%M:%SZ')
                if run_post_datetime + time_interval > datetime.utcnow():
                    continue
            except kubernetes.client.rest.ApiException as e:
                # If run is already deleted then action may be deleted
                if e.status != 404:
                    raise

            try:
                runtime.custom_objects_api.delete_namespaced_custom_object(
                    runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions', action_name
                )
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise

    def cleanup_runs(self, runtime):
        time_interval = self.remove_successful_runs_after
        if not isinstance(time_interval, timedelta):
            return
        for run_resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns',
            label_selector='{}={},{}=successful'.format(runtime.governor_label, self.name, runtime.runner_label)
        ).get('items', []):
            run_name = run_resource['metadata']['name']
            # If run has not posted a result longer ago than the interval then do not delete
            run_post_datetime = datetime.strptime(run_resource['spec']['runPostTimestamp'], '%Y-%m-%dT%H:%M:%SZ')
            if run_post_datetime + time_interval < datetime.utcnow():
                try:
                    runtime.custom_objects_api.delete_namespaced_custom_object(
                        runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns', run_name
                    )
                except kubernetes.client.rest.ApiException as e:
                    if e.status != 404:
                        raise

    def get_parameters(self, runtime, api, anarchy_subject, action_config):
        parameters = {}
        add_values(parameters, runtime, anarchy_subject.parameters)
        add_secret_values(parameters, runtime, anarchy_subject.parameter_secrets)
        add_values(parameters, runtime, api.parameters)
        add_secret_values(parameters, runtime, api.parameter_secrets)
        add_values(parameters, runtime, self.parameters)
        add_secret_values(parameters, runtime, self.parameter_secrets)
        add_values(parameters, runtime, action_config.request.parameters)
        add_secret_values(parameters, runtime, action_config.request.parameter_secrets)
        return parameters

    def refresh_from_resource(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))
        self.actions = {}
        for action_name, action_spec in self.spec.get('actions', {}).items():
            self.actions[action_name] = AnarchyGovernor.ActionConfig(action_name, action_spec, self)

    def run_ansible(self, runtime, run_config, run_vars, context, anarchy_subject, anarchy_action, event_name=None):
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

        collected_run_vars = {}
        for context_item in context:
            name, obj = context_item
            context_vars = runtime.get_vars(obj)
            context_spec = { 'vars': context_vars }
            if hasattr(obj, 'uid'):
                context_spec['apiVersion'] = runtime.api_group_version
                context_spec['uid'] = obj.uid
            for attr in ('kind', 'name', 'namespace'):
                if hasattr(obj, attr):
                    context_spec[attr] = getattr(obj, attr)
            run_spec[name] = context_spec
            collected_run_vars.update(context_vars)
        collected_run_vars.update(run_vars)
        run_spec['vars'] = collected_run_vars

        labels = {
            runtime.governor_label: self.name,
            runtime.runner_label: 'queued',
            runtime.subject_label: anarchy_subject.name,
        }
        if event_name:
            labels[runtime.event_label] = event_name

        if anarchy_action:
            if event_name:
                generate_name = '{}-{}-'.format(anarchy_action.name, event_name)
            else:
                generate_name = anarchy_action.name + '-'
            labels[runtime.action_label] = anarchy_action.name
            owner_reference = {
                'apiVersion': runtime.api_group_version,
                'controller': True,
                'kind': 'AnarchyAction',
                'name': anarchy_action.name,
                'uid': anarchy_action.uid
            }
        else:
            generate_name = '{}-{}-'.format(anarchy_subject.name, event_name)
            owner_reference = {
                'apiVersion': runtime.api_group_version,
                'controller': True,
                'kind': 'AnarchySubject',
                'name': anarchy_subject.name,
                'uid': anarchy_subject.uid
            }

        anarchy_run = runtime.custom_objects_api.create_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns',
            {
                'apiVersion': runtime.api_group_version,
                'kind': 'AnarchyRun',
                'metadata': {
                    'generateName': generate_name,
                    'labels': labels,
                    'namespace': runtime.operator_namespace,
                    'ownerReferences': [owner_reference]
                },
                'spec': run_spec
            }
        )
        anarchy_run_name = anarchy_run['metadata']['name']

        if anarchy_action:
            anarchy_action.add_run_to_status(anarchy_run, runtime)

        anarchy_subject.add_run_to_status(anarchy_run, runtime)

        if anarchy_subject.active_run_name == anarchy_run_name:
            anarchy_subject.set_active_run_to_pending(runtime)
        else:
            operator_logger.debug(
                'Not setting new AnarchyRun %s as pending, %s is active for AnarchySubject %s',
                anarchy_run_name, anarchy_subject.active_run_name, anarchy_subject.name
            )

    def subject_event_handler(self, name):
        if name in self.subject_event_handlers:
            return self.subject_event_handlers[name]
        elif '*' in self.subject_event_handlers:
            return self.subject_event_handlers['*']
        else:
            return None

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.api_group_version,
            kind = 'AnarchyGovernor',
            metadata=self.metadata,
            spec=self.spec
        )
