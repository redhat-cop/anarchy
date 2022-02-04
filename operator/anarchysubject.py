import copy
import hashlib
import json
import kopf
import kubernetes
import logging
import os
import threading
import time
import uuid

from anarchygovernor import AnarchyGovernor
from anarchyutil import deep_update
from base64 import b64decode, b64encode
from datetime import datetime

operator_logger = logging.getLogger('operator')

class AnarchySubject(object):
    """AnarchySubject class"""

    register_lock = threading.Lock()
    subjects = {}

    @staticmethod
    def get(name, anarchy_runtime):
        subject = AnarchySubject.get_from_cache(name)
        if subject:
            return subject
        return AnarchySubject.get_from_api(
            anarchy_runtime = anarchy_runtime,
            name = name,
        )

    @staticmethod
    def get_from_api(name, anarchy_runtime):
        '''
        Get AnarchyRun from api by name.
        '''
        resource_object = AnarchySubject.get_resource_from_api(
            anarchy_runtime = anarchy_runtime,
            name = name,
        )
        if resource_object:
            return AnarchySubject(resource_object=resource_object)

    @staticmethod
    def get_from_cache(name):
        return AnarchySubject.subjects.get(name)

    @staticmethod
    def get_resource_from_api(name, anarchy_runtime):
        try:
            return anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchysubjects', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def preload(anarchy_runtime):
        '''
        Load all AnarchySubjects

        This method is used during start-up to ensure that all AnarchyGovernor definitions are
        loaded before processing starts.
        '''
        operator_logger.info("Starting AnarchySubject preload")
        for resource_object in anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchysubjects'
        ).get('items', []):
            try:
                AnarchySubject.register(resource_object=resource_object)
            except:
                metadata = resource_object['metadata']
                operator_logger.exception(
                    f"Error preloading AnarchySubject {metadata['name']} in namespace {metadata['namespace']}"
                )

    @staticmethod
    def register(
        anarchy_runtime=None,
        annotations=None,
        labels=None,
        logger=None,
        meta=None,
        name=None,
        namespace=None,
        resource_object=None,
        spec=None,
        status=None,
        uid=None,
        **_
    ):
        with AnarchySubject.register_lock:
            if resource_object:
                name = resource_object['metadata']['name']
            else:
                resource_object = dict(
                    apiVersion = anarchy_runtime.api_group_version,
                    kind = 'AnarchySubject',
                    metadata = dict(
                        annotations = dict(annotations) if annotations else None,
                        creationTimestamp = meta["creationTimestamp"],
                        deletionTimestamp = meta.get("deletionTimestamp"),
                        labels = dict(labels) if labels else None,
                        name = name,
                        namespace = namespace,
                        resourceVersion = meta["resourceVersion"],
                        uid = uid,
                    ),
                    spec = dict(spec),
                    status = dict(status) if status else {},
                )
            subject = AnarchySubject.subjects.get(name)
            if subject:
                subject.__init__(logger=logger, resource_object=resource_object)
                subject.local_logger.debug("Refreshed AnarchySubject")
            else:
                subject = AnarchySubject(resource_object=resource_object, logger=logger)
                AnarchySubject.subjects[subject.name] = subject
                subject.local_logger.info("Registered AnarchySubject")
            return subject

    @staticmethod
    def unregister(name):
        with AnarchySubject.register_lock:
            if name in AnarchySubject.subjects:
                subject = AnarchySubject.subjects.pop(name)
                subject.local_logger.info("Unregistered AnarchySubject")

    @staticmethod
    def watch(anarchy_runtime):
        '''
        Watch AnarchySubjects and keep definitions synchronized

        This watch is independent of the kopf watch and is used to keep subject definitions updated
        even when the pod is not the active peer.
        '''
        while True:
            try:
                AnarchySubject.__watch(anarchy_runtime)
            except kubernetes.client.rest.ApiException as e:
                if e.status == 410:
                    # 410 Gone, simply reset watch
                    operator_logger.warning(
                        "Restarting AnarchySubject watch",
                        extra = dict(reason = str(e))
                    )
                else:
                    operator_logger.exception("ApiException in AnarchySubject watch")
                    time.sleep(5)
            except urllib3.exceptions.ProtocolError as e:
                operator_logger.warning(
                    "ProtocolError in AnarchySubject watch",
                    extra = dict(reason = str(e))
                )
                time.sleep(5)
            except Exception as e:
                operator_logger.exception("Exception in AnarchySubject watch")
                time.sleep(5)

    @staticmethod
    def __watch(anarchy_runtime):
        operator_logger.info("Starting AnarchySubject watch")
        for event in kubernetes.watch.Watch().stream(
            anarchy_runtime.custom_objects_api.list_namespaced_custom_object,
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchygovernors'
        ):
            obj = event.get('object')
            if not obj:
                continue

            if event['type'] == 'ERROR' \
            and obj['kind'] == 'Status':
                if obj['status'] == 'Failure':
                    if obj['reason'] in ('Expired', 'Gone'):
                        operator_logger.info(
                            "AnarchySubject watch restarting",
                            extra = dict(event = event)
                        )
                        return
                    else:
                        operator_logger.error(
                            "AnarchySubject watch error",
                            extra = dict(event = event)
                        )
                        time.sleep(5)
                        return

            if obj.get('apiVersion') == anarchy_runtime.api_group_version:
                if event['type'] == 'DELETED':
                    AnarchySubject.unregister(name=obj['metadata']['name'])
                else:
                    AnarchySubject.register(resource_object=obj)

    def __init__(self, resource_object, logger=None):
        self.api_version = resource_object['apiVersion']
        self.kind = resource_object['kind']
        self.metadata = resource_object['metadata']
        self.spec = resource_object['spec']
        self.status = resource_object.get('status', {})

        if not 'pendingActions' in self.status:
            self.status['pendingActions'] = []

        if not 'runs' in self.status:
            self.status['runs'] = {}
        if not 'active' in self.status['runs']:
            self.status['runs']['active'] = []

        self.local_logger = kopf.LocalObjectLogger(
            body = resource_object,
            settings = kopf.OperatorSettings(),
        )
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = self.local_logger

        if not hasattr(self, 'lock'):
            self.lock = threading.RLock()

        self.__sanity_check()

    def __sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    @property
    def active_action_name(self):
        ref = self.active_action_ref
        if ref:
            return ref['name']

    @property
    def active_action_ref(self):
        if not self.status:
            return None
        return self.status.get('activeAction')

    @property
    def active_run_name(self):
        ref = self.active_run_ref
        if ref:
            return ref['name']

    @property
    def active_run_ref(self):
        active_runs = self.active_runs
        if active_runs:
            return active_runs[0]

    @property
    def active_runs(self):
        if self.status:
            return self.status.get('runs', {}).get('active', [])
        else:
            return []

    @property
    def delete_started(self):
        return self.status and 'deleteHandlersStarted' in self.status

    @property
    def diff_base(self):
        diff_base_json = (
            self.status.get('diffBase') or
            self.metadata.get('annotations', {}).get('kopf.zalando.org/last-handled-configuration')
        )
        if diff_base_json:
            return json.loads(diff_base_json)

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def governor_reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = 'AnarchyGovernor',
            name = self.governor_name,
            namespace = self.namespace,
        )

    @property
    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def next_pending_action(self):
        ret = None
        for ref in self.pending_actions:
            if not ret or ret['after'] < ref['after']:
                ret = ref
        return ret

    @property
    def next_pending_action_name(self):
        ref = self.next_pending_action
        if ref:
            return ref['name']

    @property
    def owner_reference(self):
        return dict(
            apiVersion = self.api_version,
            controller = True,
            kind = self.kind,
            name = self.name,
            uid = self.uid,
        )

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def parameter_secrets(self):
        return self.spec.get('parameterSecrets', [])

    @property
    def pending_actions(self):
        if self.status:
            return self.status.get('pendingActions', [])
        else:
            return []

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
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def spec_sha256(self):
        return b64encode(hashlib.sha256(json.dumps(
            self.spec, sort_keys=True, separators=(',',':')
        ).encode('utf-8')).digest()).decode('utf-8')

    @property
    def supported_actions(self):
        '''
        Actions supported by governor for this subject.
        '''
        if self.status:
            return self.status.get('supportedActions', {})
        else:
            return {}

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def add_action_to_status(self, action, anarchy_runtime):
        '''
        Add AnarchyAction to AnarchySubject status
        '''
        while True:
            # Nothing to do if action already listed in status
            if self.active_action_name == action.name:
                return
            for pending_action_ref in self.pending_actions:
                if pending_action_ref['name'] == action.name:
                    return

            after_datetime = action.after_datetime
            action_reference_with_after = {
                "after": after_datetime.strftime('%FT%TZ'),
                **action.reference,
            }

            resource_object = self.to_dict()
            if self.active_action_name or after_datetime > datetime.utcnow():
                resource_object['status']['pendingActions'].append(action_reference_with_after)
            else:
                resource_object['status']['activeAction'] = action_reference_with_after

            try:
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    if not self.refresh_from_api(anarchy_runtime):
                        self.logger.error('Cannot add run to status, unable to refresh')
                        return
                else:
                    raise

        # Action was added as active, start run immediately.
        if self.active_action_name == action.name:
            action.start(anarchy_runtime=anarchy_runtime)


    def add_run_to_status(self, run, anarchy_runtime):
        '''
        Add AnarchyRun to AnarchySubject status.

        The list of active runs in the status is used to set runs from queued to pending when they reach the
        top of the list.
        '''
        set_run_pending = False
        while True:
            resource_object = self.to_dict()

            # Add run to status if not already listed
            # Run should transition to pending if it becomes the first listed run
            set_run_pending = True
            for run_ref in resource_object['status']['runs']['active']:
                set_run_pending = False
                if run_ref['name'] == run.name:
                    # No change required, AnarchyRun was already listed in status
                    return
            resource_object['status']['runs']['active'].append(run.reference)

            try:
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    if not self.refresh_from_api(anarchy_runtime):
                        self.logger.error('Cannot add run to status, unable to refresh')
                        return
                else:
                    raise

        if set_run_pending:
            run.set_to_pending(anarchy_runtime)

    def check_spec_changed(self, anarchy_runtime):
        spec_sha256_annotation = self.metadata.get('annotations', {}).get(anarchy_runtime.operator_domain + '/spec-sha256')
        return self.spec_sha256 != spec_sha256_annotation

    def delete(self, remove_finalizers, anarchy_runtime):
        result = anarchy_runtime.custom_objects_api.delete_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            anarchy_runtime.operator_namespace, 'anarchysubjects', self.name
        )
        if remove_finalizers:
            self.remove_finalizers(anarchy_runtime)
        return result

    def get_governor(self):
        return AnarchyGovernor.get(self.governor_name)

    def initialize_metadata(self, anarchy_runtime):
        '''
        Set subject finalizer and governor label.
        '''
        finalizers = self.metadata.get('finalizers', [])
        labels = self.metadata.get('labels', {})
        if anarchy_runtime.subject_label not in finalizers \
        or self.governor_name != labels.get(anarchy_runtime.governor_label):
            if anarchy_runtime.subject_label not in finalizers:
                finalizers.append(anarchy_runtime.subject_label)
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchysubjects', self.name,
                {
                    'metadata': {
                        'finalizers': finalizers,
                        'labels': {
                            anarchy_runtime.governor_label: self.governor_name
                        }
                    }
                }
            )
            self.__init__(resource_object)

        governor = self.get_governor()
        if not self.supported_actions == governor.supported_actions:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchysubjects', self.name,
                {
                    'status': {
                        'supportedActions': governor.supported_actions
                    }
                }
            )
            self.__init__(resource_object)

    def patch(self, patch, anarchy_runtime):
        '''
        Patch AnarchySubject resource and status.
        '''
        resource_patch = {}
        result = None

        if 'metadata' in patch or 'spec' in patch:
            if 'metadata' in patch:
                resource_patch['metadata'] = patch['metadata']
            if 'spec' in patch:
                resource_patch['spec'] = patch['spec']
                deep_update(self.spec, patch['spec'])
            if patch.get('skip_update_processing', False):
                # Set spec-sha256 annotation to indicate skip processing
                if 'metadata' not in resource_patch:
                    resource_patch['metadata'] = {}
                if 'annotations' not in resource_patch['metadata']:
                    resource_patch['metadata']['annotations'] = {}
                resource_patch['metadata']['annotations'][anarchy_runtime.operator_domain + '/spec-sha256'] = self.spec_sha256

            result = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchysubjects', self.name, resource_patch
            )
        if 'status' in patch:
            result = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchysubjects', self.name, {'status': patch['status']}
            )
        return result

    def process_subject_event_handlers(self, anarchy_runtime, event_name, old=None):
        governor = self.get_governor()
        if not governor:
            self.logger.warning(
                'Received event, but cannot find AnarchyGovernor',
                extra = dict(
                    event = event_name,
                    governor = dict(
                        apiVersion = anarchy_runtime.api_group_version,
                        kind = 'AnarchyGovernor',
                        name = self.governor_name,
                        namespace = self.namespace
                    )
                )
            )
            return

        handler = governor.subject_event_handler(event_name)
        if not handler:
            return

        context = (
            ('governor', governor),
            ('subject', self),
            ('handler', handler)
        )
        run_vars = {
            'anarchy_event_name': event_name,
        }
        if old:
            run_vars['anarchy_subject_previous_state'] = old

        governor.run_ansible(anarchy_runtime, handler, run_vars, context, self, None, event_name)
        return True

    def record_delete_started(self, anarchy_runtime):
        anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def refresh_from_api(self, anarchy_runtime):
        resource_object = AnarchySubject.get_resource_from_api(self.name, anarchy_runtime)
        if resource_object:
            self.__init__(resource_object)
            return True
        else:
            return False

    def remove_finalizers(self, anarchy_runtime):
        """Remove finalizers from AnarchySubject metadata to allow delete to complete.

        Parameters
        ----------
        anarchy_runtime : AnarchyRuntime
            Object with anarchy_runtime configuration and k8s APIs

        Raises
        ------
        kubernetes.client.rest.ApiException
            Exception communicating with the k8s API
        """
        try:
            return anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchysubjects', self.name,
                {
                    "metadata": {
                        "finalizers": [s for s in self.metadata.get('finalizers', []) if s != anarchy_runtime.subject_label]
                    }
                }
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def remove_action_from_status(self, action_name, anarchy_runtime):
        while True:
            resource_object = self.to_dict()
            if self.active_action_name == action_name:
                del resource_object['status']['activeAction']
            else:
                action_was_pending = False
                for i, pending_action_ref in enumerate(self.pending_actions):
                    if pending_action_ref['name'] == action_name:
                        del resource_object['status']['pendingActions'][i]
                        action_was_pending = True
                        break
                if not action_was_pending:
                    return
            try:
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(anarchy_runtime)
                else:
                    raise

    def remove_run_from_status(self, anarchy_runtime, run_name):
        set_next_run_to_pending = True
        next_run_resource_object = None
        remove_run_names = [run_name]
        while True:
            resource_object = self.to_dict()

            # Only set next run to pending if run remove was the first active run
            if resource_object['status']['runs']['active'] \
            and resource_object['status']['runs']['active'][0]['name'] == run_name:
                del resource_object['status']['runs']['active'][0]
            else:
                set_next_run_to_pending = False

            while set_next_run_to_pending \
            and not next_run_resource_object \
            and resource_object['status']['runs']['active']:
                next_run_name = resource_object['status']['runs']['active'][0]['name']
                try:
                    next_run_resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                        self.namespace, 'anarchyruns', next_run_name,
                        {
                            "metadata": {
                                "labels": {
                                    anarchy_runtime.runner_label: 'pending'
                                }
                            }
                        }
                    )
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 404:
                        del resource_object['status']['runs']['active'][0]
                        remove_run_names.append(next_run_name)
                    else:
                        raise

            resource_object['status']['runs']['active'] = [
                ref for ref in resource_object['status']['runs']['active'] if ref['name'] not in remove_run_names
            ]

            # No change to subject, simply return
            if resource_object['status']['runs']['active'] == self.status['runs']['active']:
                return

            try:
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(anarchy_runtime)
                else:
                    raise


    def reschedule_action_in_status(self, action, anarchy_runtime):
        while True:
            resource_object = self.to_dict()

            if self.active_action_name == action.name:
                if self.active_action_ref['after'] == action.after:
                    return
                else:
                    resource_object['status']['activeAction']['after'] = action.after
            else:
                for pending_action_ref in resource_objec['status']['pendingActions']:
                    if pending_action_ref['name'] == action.name:
                        if pending_action_ref['after'] == action.after:
                            return
                        else:
                            pending_action_ref['after'] = action.after

            try:
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    if not self.refresh_from_api(anarchy_runtime):
                        self.logger.error(
                            'Cannot reschedule AnarchyAction in status, unable to refresh',
                            extra = dict(action=action.reference)
                        )
                        return
                else:
                    raise

        if self.active_action_name == action.name \
        and action.after_datetime <= datetime.utcnow():
            action.start(anarchy_runtime=anarchy_runtime)


    def set_active_action(self, action, anarchy_runtime):
        """Attempt to set activeAction in AnarchySubject status.
        If a different action is already active then no change will be made.

        Parameters
        ----------
        action : AnarchyAction
            The action to set as active.
        anarchy_runtime : AnarchyRuntime
            Object with anarchy_runtime configuration and k8s APIs

        Raises
        ------
        kubernetes.client.rest.ApiException
            Exception communicating with the k8s API

        Returns
        -------
        bool
            Indication of whether active action was set successfully.
        """
        while True:
            if self.active_action_name == action.name:
                return True
            elif self.active_action_name:
                return False

            resource_object = self.to_dict()
            resource_object['status']['activeAction'] = action.reference
            resource_object['status']['activeAction']['after'] = action.after_datetime.strftime('%FT%TZ')
            resource_object['status']['pendingActions'] = [
                ref for ref in self.pending_actions if ref['name'] != action.name
            ]

            try:
                self.logger.info(
                    'Setting AnarchyAction to active',
                    extra = dict(
                        action = action.reference
                    )
                )
                resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchysubjects', self.name, resource_object
                )
                self.__init__(resource_object)
                return True
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    if not self.refresh_from_api(anarchy_runtime):
                        self.logger.error(
                            'Cannot set active AnarchyAction in status, unable to refresh',
                            extra = dict(action = action.refrence)
                        )
                        return False
                else:
                    raise

    def set_active_run_to_pending(self, anarchy_runtime):
        """Patch AnarchyRun listed as active in AnarchySubject to pending state.
        If AnarchyRun is not found then it is removed from the subject.

        Parameters
        ----------
        anarchy_runtime : AnarchyRuntime
            Object with anarchy_runtime configuration and k8s APIs

        Raises
        ------
        kubernetes.client.rest.ApiException
            Exception communicating with the k8s API
        """
        while True:
            run_ref = self.active_run_ref
            if not run_ref:
                return
            run_name = run_ref['name']
            run_namespace = run_ref['namespace']
            try:
                self.logger.info(
					'Setting AnarchyRun to pending',
					extra = dict(
						run = run_ref
					)
				)
                anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    run_namespace, 'anarchyruns', run_name,
                    {'metadata': {'labels': { anarchy_runtime.runner_label: 'pending' } } }
                )
                return
            except kubernetes.client.rest.ApiException as e:
                if e.status == 404:
                    self.logger.warning(
						'AnarchyRun was deleted before execution',
						extra = dict(
							run = run_ref
						)
					)
                    self.remove_run_from_status(
                        run_name = run_ref['name'],
                        anarchy_runtime = anarchy_runtime
                    )
                else:
                    raise

    def set_run_failure_in_status(self, anarchy_runtime, run):
        try:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchysubjects', self.name,
                {
                    'status': {
                        'runStatus': run.result_status,
                        'runStatusMessage': run.result_status_message,
                    }
                }
            )
            self.__init__(resource_object)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def to_dict(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            metadata = copy.deepcopy(self.metadata),
            spec = copy.deepcopy(self.spec),
            status = copy.deepcopy(self.status),
        )
