import datetime
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

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def action(self):
        return self.spec['action']

    def callback_url(self, event_name = None):
        # FIXME - ensure that callback base url is set
        callback_url = '{}/event/{}/{}'.format(
            os.environ['CALLBACK_BASE_URL'],
            self.namespace(),
            self.name()
        )
        if event_name:
            return callback_url + '/' + event_name
        else:
            return callback_url

    def has_started(self):
        return len(self.status) > 0

    def is_due(self):
        after_datetime = self.spec.get('after','')
        return after_datetime < datetime.datetime.utcnow().strftime('%FT%TZ')

    def governor(self):
        return AnarchyGovernor.get(self.spec['governorRef']['name'])

    def governor_name(self):
        return self.spec['governorRef']['name']

    def subject(self):
        return AnarchySubject.get(
            self.spec['subjectRef']['namespace'],
            self.spec['subjectRef']['name']
        )

    def subject_name(self):
        return self.spec['subjectRef']['name']

    def subject_namespace(self):
        return self.spec['subjectRef']['namespace']

    def start(self):
        logger.debug('Starting action %s', self.name())
        self.subject().start_action(self)

    def create(self, runtime):
        resource = runtime.kube_custom_objects.create_namespaced_custom_object(
            runtime.crd_domain, 'v1', self.subject_namespace(), 'anarchyactions',
            {
                "apiVersion": runtime.crd_domain + "/v1",
                "kind": "AnarchyAction",
                "metadata": self.metadata,
                "spec": self.spec
            }
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']

    def patch_status(self, runtime, patch):
        resource = runtime.kube_custom_objects.patch_namespaced_custom_object_status(
            runtime.crd_domain,
            'v1',
            self.namespace(),
            'anarchyactions',
            self.name(),
            {"status": patch}
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

    def status_event_log(self, runtime, event_name, event_data):
        events = self.status.get('events', [])
        events.append({
            "name": event_name,
            "data": event_data
        })
        self.patch_status(runtime, {
            "events": events
        })


    def process_event(self, runtime, event_data, event_name=None):
        subject = self.subject()
        governor = subject.governor()
        governor.process_action_event_handlers(runtime, subject, self, event_data, event_name)
