from anarchyutil import parse_time_interval
from datetime import datetime, timedelta
import copy
import jinja2
import json
import kopf
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
            governor.logger.debug("Starting cleanup")
            governor.cleanup_subjects(runtime)
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
            AnarchyGovernor.register(resource, runtime)

    @staticmethod
    def register(resource, runtime):
        name = resource['metadata']['name']
        governor = AnarchyGovernor.cache.get(name)
        if governor:
            governor.__init__(resource, runtime)
            governor.logger.info("Refreshed")
        else:
            governor = AnarchyGovernor(resource, runtime)
            AnarchyGovernor.cache[name] = governor
            governor.logger.info("Registered")
        return governor

    @staticmethod
    def unregister(governor):
        name = governor.name if isinstance(governor, AnarchyGovernor) else governor
        if name in AnarchyGovernor.cache:
            governor = AnarchyGovernor.cache.pop(name)
            governor.logger.info("Unregistered")

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
                    AnarchyGovernor.register(obj, runtime)
                elif event['type'] == 'DELETED':
                    AnarchyGovernor.unregister(obj['metadata']['name'])

    def __init__(self, resource, runtime, logger=None):
        self.api_version = runtime.api_version
        self.api_group = runtime.operator_domain
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.logger = logger or kopf.LocalObjectLogger(
            body = resource,
            settings = kopf.OperatorSettings(),
        )

        self.set_subject_event_handlers(self.spec.get('subjectEventHandlers',{}))

        self.actions = {}
        for action_name, action_spec in self.spec.get('actions', {}).items():
            self.actions[action_name] = AnarchyGovernor.ActionConfig(action_name, action_spec, self)

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
    def api_group_version(self):
        return f"{self.api_group}/{self.api_version}"

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
    def ref(self):
        return dict(
            apiVersion = f"{self.api_group}/{self.api_version}",
            kind = 'AnarchyGovernor',
            name = self.metadata['name'],
            namespace = self.metadata['namespace'],
            uid = self.metadata['uid'],
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

    def cleanup_subjects(self, runtime):
        """
        Cleanup AnarchySubjects, removing references to AnarchyActions or AnarchyRuns that do not exist.
        """
        for subject_resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchysubjects',
            label_selector='{}={}'.format(runtime.governor_label, self.name)
        ).get('items', []):
            subject_meta = subject_resource['metadata']
            subject_name = subject_meta['name']
            subject_namespace = subject_meta['namespace']
            subject_ref = dict(
                apiVersion = runtime.api_version,
                kind = 'AnarchySubject',
                name = subject_name,
                namespace = subject_namespace,
                uid = subject_meta['uid'],
            )
            subject_update_required = False

            active_action_ref = subject_resource.get('status', {}).get('activeAction')
            if active_action_ref:
                action_name = active_action_ref['name']
                action_namespace = active_action_ref['namespace']
                try:
                    runtime.custom_objects_api.get_namespaced_custom_object(
                        runtime.operator_domain, runtime.api_version, action_namespace, 'anarchyactions', action_name
                    )
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 404:
                        self.logger.info(
                            'Clearing deleted AnarchyAction from AnarchySubject status.activeAction',
                            extra = dict(
                                action = active_action_ref,
                                subject = subject_ref,
                            )
                        )
                        subject_resource['status']['activeAction'] = None
                        subject_update_required = True
                    else:
                        raise

            lost_first_active_run = False
            active_runs = []
            for i, active_run_ref in enumerate(subject_resource.get('status', {}).get('runs', {}).get('active', [])):
                run_name = active_run_ref['name']
                run_namespace = active_run_ref['namespace']
                try:
                    runtime.custom_objects_api.get_namespaced_custom_object(
                        runtime.operator_domain, runtime.api_version, run_namespace, 'anarchyruns', run_name
                    )
                    active_runs.append(active_run_ref)
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 404:
                        self.logger.info(
                            'Removing deleted AnarchyRun from AnarchySubject status.runs.active',
                            extra = dict(
                                run = active_run_ref,
                                subject = subject_ref,
                            )
                        )
                        subject_update_required = True
                        lost_first_active_run = i == 0
                    else:
                        raise

            if subject_update_required:
                try:
                    subject_resource['status']['runs']['active'] = active_runs
                    resource = runtime.custom_objects_api.replace_namespaced_custom_object_status(
                        runtime.operator_domain, runtime.api_version, subject_namespace, 'anarchysubjects', subject_name, subject_resource
                    )
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 404 \
                    or e.status == 409:
                        pass
                    else:
                        raise

                if lost_first_active_run and len(active_runs) > 0:
                    run_name = active_runs[0]['name']
                    run_namespace = active_runs[0]['namespace']
                    runtime.custom_objects_api.patch_namespaced_custom_object(
                        runtime.operator_domain, runtime.api_version, run_namespace, 'anarchyruns', run_name,
                        {'metadata': {'labels': { runtime.runner_label: 'pending' } } }
                    )

    def cleanup_actions(self, runtime):
        """
        Delete AnarchyActions that have finished a while ago, configured by spec.removeFinishedActions.after
        """
        time_interval = self.remove_finished_actions_after
        if not isinstance(time_interval, timedelta):
            return
        for action_resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions',
            label_selector='{}={},{}'.format(runtime.governor_label, self.name, runtime.finished_label)
        ).get('items', []):
            action_name = action_resource['metadata']['name']
            # If action has no finishedTimestamp than do not delete
            finished_timestamp = action_resource.get('status', {}).get('finishedTimestamp')
            if not finished_timestamp:
                continue

            # If action has not finished longer ago than the interval then do not delete
            finished_datetime = datetime.strptime(finished_timestamp, '%Y-%m-%dT%H:%M:%SZ')
            if finished_datetime + time_interval > datetime.utcnow():
                continue

            try:
                runtime.custom_objects_api.delete_namespaced_custom_object(
                    runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyactions', action_name
                )
            except kubernetes.client.rest.ApiException as e:
                if e.status != 404:
                    raise

    def cleanup_runs(self, runtime):
        """
        Delete AnarchyRuns that have posted results a while ago, configured by spec.removeSuccessfulRuns.after
        """
        time_interval = self.remove_successful_runs_after
        if not isinstance(time_interval, timedelta):
            return
        for run_resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, runtime.api_version, runtime.operator_namespace, 'anarchyruns',
            label_selector='{}={},{}=successful'.format(runtime.governor_label, self.name, runtime.runner_label)
        ).get('items', []):
            run_name = run_resource['metadata']['name']
            # If run has no runPostTimestamp than do not delete
            run_post_timestamp = run_resource.get('spec', {}).get('runPostTimestamp')
            if not run_post_timestamp:
                continue

            # If run has not posted a result longer ago than the interval then do not delete
            run_post_datetime = datetime.strptime(run_post_timestamp, '%Y-%m-%dT%H:%M:%SZ')
            if run_post_datetime + time_interval > datetime.utcnow():
                continue

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
        anarchy_run_meta = anarchy_run['metadata']
        anarchy_run_name = anarchy_run_meta['name']

        if anarchy_action:
            anarchy_action.add_run_to_status(anarchy_run, runtime)

        anarchy_subject.add_run_to_status(anarchy_run, runtime)

        if anarchy_subject.active_run_name == anarchy_run_name:
            anarchy_subject.set_active_run_to_pending(runtime)
        else:
            self.logger.debug(
                'Not setting new AnarchyRun as pending, AnarchySubject already has active run',
                extra = dict(
                    activeRun = anarchy_subject.active_run_ref,
                    newRun = dict(
                        apiVersion = runtime.api_group_version,
                        kind = 'AnarchyRun',
                        name = anarchy_run_name,
                        namespace = anarchy_run_meta['namespace'],
                        uid = anarchy_run_meta['uid']
                    ),
                    subject = dict(
                        apiVersion  = runtime.api_group_version,
                        kind = 'AnarchySubject',
                        name = anarchy_subject.name,
                        namespace = anarchy_subject.namespace,
                        uid = anarchy_subject.uid,
                    )
                )
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
            metadata = self.metadata,
            spec = self.spec
        )
