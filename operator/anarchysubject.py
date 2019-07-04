import datetime
import logging
import os

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchySubject(object):
    """AnarchySubject class"""

    subjects = {}

    @classmethod
    def register(_class, resource):
        subject = AnarchySubject.get(
            resource['metadata']['namespace'],
            resource['metadata']['name']
        )

        if subject \
        and subject.resource_version() == resource['metadata']['resourceVersion']:
            logger.debug("Ignoring subject at same resource version %s (%s)",
                subject.namespace_name(),
                subject.resource_version()
            )
            return None

        subject = _class(resource)
        logger.info("Registered subject %s (%s)",
            subject.namespace_name(),
            subject.resource_version()
        )
        AnarchySubject.subjects[subject.namespace_name()] = subject

        return subject

    @classmethod
    def unregister(_class, subject):
        if isinstance(subject, AnarchySubject):
            del AnarchySubject.subjects[subject.name()]
        else:
            del AnarchySubject.subjects[subject]

    @classmethod
    def get(_class, namespace, name):
        return AnarchySubject.subjects.get(namespace + '/' + name, None)

    @classmethod
    def start_subject_actions(_class, runtime):
        for subject in AnarchySubject.subjects.values():
            subject.start_actions(runtime)

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.action_queue = []
        self.sanity_check()

    def sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    def uid(self):
        return self.metadata['uid']

    def name(self):
        return self.metadata['name']

    def namespace(self):
        return self.metadata['namespace']

    def namespace_name(self):
        return self.metadata['namespace'] + '/' + self.metadata['name']

    def is_new(self):
        return not self.status

    def governor(self):
        return AnarchyGovernor.get(self.spec['governor'])

    def governor_name(self):
        return self.spec['governor']

    def parameters(self):
        return self.spec.get('parameters', {})

    def _vars(self):
        return self.spec.get('vars', {})

    def resource_version(self):
        return self.metadata['resourceVersion']

    def queue_action(self, action):
        logger.debug("Queued action %s on %s",
            action.name(),
            self.namespace_name()
        )
        self.action_queue.append(action)

    def start_actions(self, runtime):
        logger.debug("Starting actions that are due on %s", self.namespace_name())
        for action in self.action_queue[:]:
            if action.is_due():
                self.governor().start_action(runtime, self, action)
                self.action_queue.remove(action)

    def process_subject_event_handlers(self, runtime, event_name):
        return self.governor().process_subject_event_handlers(runtime, self, event_name)

    def schedule_action(self, runtime, action_name, after_seconds):
        after = (
            datetime.datetime.utcnow() +
            datetime.timedelta(0, after_seconds)
        ).strftime('%FT%TZ')

        logger.info("Scheduling action %s for %s governed by %s to run after %s",
            action_name,
            self.namespace_name(),
            self.governor_name(),
            after
        )

        # Late import to avoid circular import at start
        import anarchyaction
        action = anarchyaction.AnarchyAction({
            "metadata": {
                "generateName": self.name() + '-' + action_name + '-',
                "labels": {
                    runtime.crd_domain + "/anarchy-subject": self.name(),
                    runtime.crd_domain + "/anarchy-governor": self.governor_name(),
                },
                "ownerReferences": [{
                    "apiVersion": runtime.crd_domain + "/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": self.name(),
                    "uid": self.uid()
                }]
            },
            "spec": {
                "action": action_name,
                "after": after,
                "governorRef": {
                    "apiVersion": runtime.crd_domain + "/v1",
                    "kind": "AnarchyGovernor",
                    "name": self.governor_name(),
                    "namespace": runtime.namespace,
                    "uid": self.governor().uid()
                },
                "subjectRef": {
                    "apiVersion": runtime.crd_domain + "/v1",
                    "kind": "AnarchySubject",
                    "name": self.name(),
                    "namespace": self.namespace(),
                    "uid": self.uid()
                }
            }
        })
        action.create(runtime)
        self.patch_status(runtime, {
            "currentAction": action.name()
        })

    def patch(self, runtime, patch):
        resource = runtime.kube_custom_objects.patch_namespaced_custom_object(
            runtime.crd_domain,
            'v1',
            self.namespace(),
            'anarchysubjects',
            self.name(),
            patch
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']

    def patch_status(self, runtime, patch):
        resource = runtime.kube_custom_objects.patch_namespaced_custom_object_status(
            runtime.crd_domain,
            'v1',
            self.namespace(),
            'anarchysubjects',
            self.name(),
            {"status": patch}
        )
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource['status']
