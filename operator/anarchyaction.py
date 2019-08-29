from datetime import datetime
import logging
import os

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject

class AnarchyAction(object):
    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', {})
        self.sanity_check()

    def sanity_check(self):
        # FIXME
        pass

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
    def callback_url(self, event_name = None):
        # FIXME - ensure that callback base url is set
        callback_url = '{}/event/{}/{}'.format(
            os.environ['CALLBACK_BASE_URL'],
            self.namespace,
            self.name
        )
        if event_name:
            return callback_url + '/' + event_name
        else:
            return callback_url

    @property
    def governor(self):
        return AnarchyGovernor.get(self.spec['governorRef']['name'])

    @property
    def governor_name(self):
        return self.spec['governorRef']['name']

    @property
    def has_started(self):
        return True if self.status else False

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
    def uid(self):
        return self.metadata['uid']

    @property
    def subject_name(self):
        return self.spec['subjectRef']['name']

    @property
    def subject_namespace(self):
        return self.spec['subjectRef']['namespace']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def check_callback_token(self, authorization_header):
        if not authorization_header.startswith('Bearer '):
            return false
        return self.callback_token == authorization_header[7:]

    def create(self, runtime):
        logger.debug('Creating action...')
        resource = runtime.custom_objects_api.create_namespaced_custom_object(
            runtime.operator_domain, 'v1', self.subject_namespace, 'anarchyactions',
            {
                "apiVersion": runtime.operator_domain + "/v1",
                "kind": "AnarchyAction",
                "metadata": self.metadata,
                "spec": self.spec
            }
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        logger.debug('Created action %s', self.namespace_name)

    def get_subject(self, runtime):
        resource = runtime.custom_objects_api.get_namespaced_custom_object(
            runtime.operator_domain, 'v1', self.subject_namespace,
            'anarchysubjects', self.subject_name
        )
        return AnarchySubject(resource) if resource else None

    def patch_status(self, runtime, patch):
        resource = runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain,
            'v1',
            self.namespace,
            'anarchyactions',
            self.name,
            {"status": patch}
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

    def status_event_log(self, runtime, event_name, event_data):
        events = self.status.get('events', [])
        events.append({
            "name": event_name,
            "data": event_data,
            "timestamp": datetime.utcnow().strftime('%FT%TZ')
        })
        self.patch_status(runtime, {
            "events": events
        })

    def process_event(self, runtime, event_data, event_name=None):
        subject = self.get_subject(runtime)
        subject.process_action_event_handlers(runtime, self, event_data, event_name)

    def start(self, runtime):
        subject = self.get_subject(runtime)
        if subject:
            subject.start_action(runtime, self)
        else:
            runtime.logger.warning(
                "Unable to find AnarchySubject %s for action %s!",
                self.subject_name, self.name
            )
