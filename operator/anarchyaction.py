import copy
import kopf
import kubernetes
import threading
import time

from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from datetime import datetime, timedelta

class AnarchyAction(object):
    """
    AnarchyAction class
    """

    register_lock = threading.Lock()
    actions = {}

    @staticmethod
    def cancel_timers():
        with AnarchyAction.register_lock:
            for action in AnarchyAction.actions.values():
                if action.delete_timer:
                    action.delete_timer.cancel()
                if action.run_timer:
                    action.run_timer.cancel()

    @staticmethod
    def get_from_api(name, anarchy_runtime):
        '''
        Get AnarchyAction from api by name.
        '''
        resource = AnarchyAction.get_resource_from_api(name, anarchy_runtime)
        if resource:
            return AnarchyAction(resource)

    @staticmethod
    def get_from_cache(name):
        return AnarchyAction.actions.get(name)

    @staticmethod
    def get_resource_from_api(name, anarchy_runtime):
        try:
            return anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

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
        with AnarchyAction.register_lock:
            if resource_object:
                name = resource_object['metadata']['name']
            else:
                resource_object = dict(
                    apiVersion = anarchy_runtime.api_group_version,
                    kind = 'AnarchyAction',
                    metadata = dict(
                        annotations = dict(annotations) if annotations else {},
                        creationTimestamp = meta["creationTimestamp"],
                        deletionTimestamp = meta.get("deletionTimestamp"),
                        labels = dict(labels) if labels else {},
                        name = name,
                        namespace = namespace,
                        resourceVersion = meta["resourceVersion"],
                        uid = uid,
                    ),
                    spec = dict(spec),
                    status = dict(status) if status else {},
                )
            action = AnarchyAction.actions.get(name)
            if action:
                action.__init__(logger=logger, resource_object=resource_object)
                action.local_logger.debug("Refreshed AnarchyAction")
            else:
                action = AnarchyAction(resource_object=resource_object, logger=logger)
                AnarchyAction.actions[action.name] = action
                action.local_logger.info("Registered AnarchyAction")
            return action

    @staticmethod
    def unregister(name):
        with AnarchyAction.register_lock:
            if name in AnarchyAction.actions:
                action = AnarchyAction.actions.pop(name)
                if action.delete_timer:
                    action.delete_timer.cancel()
                if action.run_timer:
                    action.run_timer.cancel()
                action.logger.info("Unregistered AnarchyAction")
                return action

    def __init__(self, resource_object, logger=None):
        self.api_version = resource_object['apiVersion']
        self.kind = resource_object['kind']
        self.metadata = resource_object['metadata']
        self.spec = resource_object['spec']
        self.status = resource_object.get('status')

        if not hasattr(self, 'delete_timer'):
            self.delete_timer = None

        if not hasattr(self, 'run_timer'):
            self.run_timer = None

        self.local_logger = kopf.LocalObjectLogger(
            body = resource_object,
            settings = kopf.OperatorSettings(),
        )
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = self.local_logger

    @property
    def action(self):
        return self.spec['action']

    @property
    def after(self):
        return self.spec.get('after', '')

    @property
    def after_datetime(self):
        return datetime.strptime(
            self.spec['after'], '%Y-%m-%dT%H:%M:%SZ'
        ) if 'after' in self.spec else datetime.utcnow()

    @property
    def callback_token(self):
        return self.spec.get('callbackToken', '')

    @property
    def finished_datetime(self):
        timestamp = self.finished_timestamp
        if timestamp:
            return datetime.strptime(
                timestamp, '%Y-%m-%dT%H:%M:%SZ'
            )

    @property
    def finished_timestamp(self):
        return self.status.get('finishedTimestamp')

    @property
    def governor(self):
        return AnarchyGovernor.get(self.spec['governorRef']['name'])

    @property
    def governor_name(self):
        return self.spec['governorRef']['name']

    @property
    def has_owner(self):
        return True if self.metadata.get('ownerReferences')else False

    @property
    def has_run_scheduled(self):
        return True if self.status and 'runScheduled' in self.status else False

    @property
    def is_finished(self):
        return True if self.status and 'finishedTimestamp' in self.status else False

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

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
    def reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            name = self.name,
            namespace = self.namespace,
            uid = self.uid,
        )

    @property
    def subject_name(self):
        return self.spec['subjectRef']['name']

    @property
    def subject_reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = 'AnarchySubject',
            name = self.subject_name,
            namespace = self.namespace,
        )

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def check_callback_token(self, authorization_header):
        if not authorization_header.startswith('Bearer '):
            return false
        return self.callback_token == authorization_header[7:]

    def get_governor(self):
        return AnarchyGovernor.get(self.governor_name)

    def get_subject(self):
        return AnarchySubject.get(self.subject_name)

    def delete(self, anarchy_runtime):
        try:
            anarchy_runtime.custom_objects_api.delete_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyactions', self.name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def process_callback(self, anarchy_runtime, callback_name, callback_data):
        subject = self.get_subject()
        if not subject:
            self.logger.info(
                'Received callback but cannot find AnarchySubject',
                extra = dict(
                    subject = self.subject_reference,
                )
            )
            return

        governor = subject.get_governor()
        if not governor:
            self.logger.warning(
                'Received callback but cannot find AnarchyGovernor',
                extra = dict(
                    governor = governor.reference
                )
            )
            return

        action_config = governor.action_config(self.action)
        if not action_config:
            self.logger.warning(
                'Received callback for action which is not defined in governor',
                extra = dict(
                    action = self.action,
                    governor = governor.reference,
                )
            )
            return
        if not callback_name:
            name_parameter = action_config.callback_name_parameter or governor.callback_name_parameter
            callback_name = callback_data.get(name_parameter, None)
            if not callback_name:
                self.logger.warning(
                    'Name parameter not set in callback data, cannot process callback',
                    extra = dict(
                        nameParameter = name_parameter
                    )
                )
                return

        while True:
            if not self.status:
                self.status = {}
            if not 'callbackEvents' in self.status:
                self.status['callbackEvents'] = []

            self.status['callbackEvents'].append({
                'name': callback_name,
                'data': callback_data,
                'timestamp': datetime.utcnow().strftime('%FT%TZ')
            })

            try:
                resource = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                    anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                    self.namespace, 'anarchyactions', self.name, self.to_dict()
                )
                self.__init__(resource)
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 409:
                    # Conflict, refresh subject from api and retry
                    self.refresh_from_api(anarchy_runtime)
                else:
                    raise

        handler = action_config.callback_handler(callback_name)
        if not handler:
            self.logger.warning(
                'Callback handler not found',
                extra = dict(
                    action = action_config.name,
                    callback = callback_name,
                )
            )
            return

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config),
            ('action', self),
            ('handler', handler),
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name': callback_name,
            'anarchy_action_callback_data': callback_data,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': anarchy_runtime.action_callback_url(self.name)
        }

        governor.run_ansible(anarchy_runtime, handler, run_vars, context, subject, self, callback_name)

    def refresh_from_api(self, anarchy_runtime):
        resource = AnarchyAction.get_resource_from_api(self.name, anarchy_runtime)
        if resource:
            self.__init__(resource)
        else:
            raise Exception('Unable to find AnarchyAction {} to refresh'.format(self.name))

    def schedule_continuation(self, after, anarchy_runtime):
        try:
            anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                self.namespace, 'anarchyactions', self.name,
                {
                    "status": {
                        "runRef": None,
                        "runScheduled": None,
                    }
                }
            )
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.run_label: None
                        }
                    },
                    'spec': {
                        'after': after.strftime('%FT%TZ')
                    },
                }
            )
            self.__init__(resource_object)
        except kubernetes.client.rest.ApiException as e:
            # If error is 404, not found, then subject or action must have been deleted
            if e.status != 404:
                raise

    def set_finished(self, state, anarchy_runtime):
        try:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'status': {
                        'finishedTimestamp': datetime.utcnow().strftime('%FT%TZ'),
                        'state': state
                    }
                }
            )
            self.__init__(resource_object)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

        try:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.finished_label: state
                        }
                    }
                }
            )
            self.__init__(resource_object)
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise

    def set_owner_references(self, anarchy_runtime):
        '''
        Anarchy when using on.event when an anarchyaction is added and modified
        we will verify that an ownerReferences exist and created it.
        '''
        subject = self.get_subject()
        if not subject:
            raise kopf.TemporaryError('Cannot find AnarchySubject for AnarchyAction')
                
        governor = subject.get_governor()
        if not governor:
            raise kopf.TemporaryError('Cannot find AnarchyGovernor for AnarchyAction')

        if self.metadata['labels'].get(anarchy_runtime.action_label) != self.action \
        or self.metadata['labels'].get(anarchy_runtime.governor_label) != governor.name \
        or self.metadata['labels'].get(anarchy_runtime.subject_label) != subject.name \
        or self.metadata.get('ownerReferences') != [subject.owner_reference] \
        or self.spec.get('governorRef') != governor.reference \
        or self.spec.get('subjectRef') != subject.reference:
            resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyactions', self.name,
                {
                    'metadata': {
                        'labels': {
                            anarchy_runtime.action_label: self.action,
                            anarchy_runtime.governor_label: governor.name,
                            anarchy_runtime.subject_label: subject.name,
                        },
                        'ownerReferences': [subject.owner_reference]
                    },
                    'spec': {
                        'governorRef': governor.reference,
                        'subjectRef': subject.reference,
                    }
                }
            )
            self.__init__(resource_object)
            self.logger.info(
                "Set owner references for AnarchyAction",
                extra = dict(
                    governor = governor.reference,
                    subject = subject.reference,
                )
            )

    def start(self, anarchy_runtime):
        governor = self.get_governor()
        subject = self.get_subject()

        # Subject may have been deleted, abort run of the action in this case
        if not subject:
            self.logger.info(
                'Not starting AnarchyAction for deleted AnarchySubject',
                extra = dict(
                    subject = self.subject_reference,
                )
            )
            return

        # Attempt to set active action for subject
        if not subject.set_active_action(self, anarchy_runtime):
            # Some other action must have become active first
            return

        action_config = governor.action_config(self.action)
        if not action_config:
            self.logger.error(
                'Attempt to start AnarchyAction with action which is not defined in governor',
                extra = dict(
                    action = self.action,
                    governor = governor.reference,
                )
            )
            return

        # Mark that run is scheduled, though AnarchyRun reference is not yet known
        # The AnarchyRun reference will be set by the kopf.on.create handler for the AnarchyRun
        resource_object = anarchy_runtime.custom_objects_api.patch_namespaced_custom_object_status(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version,
            self.namespace, 'anarchyactions', self.name,
            {
                'status': {
                    'runRef': None,
                    'runScheduled': datetime.utcnow().strftime('%FT%TZ'),
                }
            }
        )
        self.__init__(resource_object)

        context = (
            ('governor', governor),
            ('subject', subject),
            ('actionConfig', action_config),
            ('action', self),
        )
        run_vars = {
            'anarchy_action_name': self.name,
            'anarchy_action_callback_name_parameter': action_config.callback_name_parameter or governor.callback_name_parameter,
            'anarchy_action_callback_token': self.callback_token,
            'anarchy_action_callback_url': anarchy_runtime.action_callback_url(self.name)
        }

        governor.run_ansible(anarchy_runtime, action_config, run_vars, context, subject, self)
        return True

    def to_dict(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            metadata = copy.deepcopy(self.metadata),
            spec = copy.deepcopy(self.spec),
            status = copy.deepcopy(self.status),
        )
