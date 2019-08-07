import datetime
import logging
import os
import threading
import uuid

logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchySubject(object):
    """AnarchySubject class"""

    subjects_lock = threading.RLock()
    subjects = {}

    @classmethod
    def register(_class, resource):
        _class.subjects_lock.acquire()

        subject = AnarchySubject.get(
            resource['metadata']['namespace'],
            resource['metadata']['name']
        )

        if subject:
            # Subject must not be new, because it is already registered
            subject.is_new = False
            if subject.resource_version() == resource['metadata']['resourceVersion']:
                logger.debug("Subject at same resource version %s (%s)",
                    subject.namespace_name(),
                    subject.resource_version()
                )
            else:
                logger.debug("Updating subject %s (%s)",
                    subject.namespace_name(),
                    subject.resource_version()
                )
                subject.is_updated = subject.spec != resource['spec']
                subject.metadata = resource['metadata']
                subject.spec = resource['spec']
                subject.status = resource.get('status', None)
        else:
            subject = _class(resource)
            logger.info("Registered subject %s (%s)",
                subject.namespace_name(),
                subject.resource_version()
            )
            AnarchySubject.subjects[subject.namespace_name()] = subject

        _class.subjects_lock.release()
        return subject

    @classmethod
    def unregister(_class, subject):
        _class.subjects_lock.acquire()
        del AnarchySubject.subjects[subject.namespace_name()]
        _class.subjects_lock.release()

    @classmethod
    def get(_class, namespace, name):
        return AnarchySubject.subjects.get(namespace + '/' + name, None)

    @classmethod
    def start_subject_actions(_class, runtime):
        logger.debug("Acquiring subjects_lock in start_subject_actions")
        _class.subjects_lock.acquire()
        for subject in AnarchySubject.subjects.values():
            subject.start_actions(runtime)
        _class.subjects_lock.release()

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.is_new = 'status' not in resource
        self.is_updated = False
        self.action_queue = []
        self.action_queue_lock = threading.RLock()
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

    def delete_complete(self):
        delete_finalizer_condition = self.governor().delete_finalizer_condition()
        if delete_finalizer_condition:
            return delete_finalizer_condition.check(self)
        else:
            return self.delete_started()

    def delete_started(self):
        return 'deleteHandlersStarted' in self.status

    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

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

    def lock_action_queue(self):
        self.action_queue_lock.acquire()

    def unlock_action_queue(self):
        self.action_queue_lock.release()

    def queue_action(self, action):
        self.lock_action_queue()
        if not action.has_started():
            logger.info("Queue action %s on %s", action.name(), self.namespace_name())
            self.action_queue.append(action)
        self.unlock_action_queue()

    def requeue_action(self, action):
        self.lock_action_queue()
        if not action.has_started():
            for i in range(len(self.action_queue)):
                if action.uid == self.action_queue[i].uid:
                    logger.warn("Requeuing action %s", action.namespace_name())
                    self.action_queue[i] = action
                    self.unlock_action_queue()
                    return
            logger.warn("Requeuing action %s, but was not found in queue?", action.namespace_name())
            self.action_queue.append(action)
        self.unlock_action_queue()

    def dequeue_action(self, action):
        self.lock_action_queue()
        for i in range(len(self.action_queue)):
            if action.uid == self.action_queue[i].uid:
                logger.warn("Dequeuing action %s", action.namespace_name())
                del self.action_queue[i]
        self.unlock_action_queue()

    def start_actions(self, runtime):
        logger.debug("Starting actions that are due on %s", self.namespace_name())
        due_actions = []

        self.lock_action_queue()
        for action in self.action_queue[:]:
            logger.debug(action)
            logger.debug(action.is_due())
            if action.is_due():
                due_actions.append(action)
                self.action_queue.remove(action)
        self.unlock_action_queue()
        logger.debug(due_actions)

        for action in due_actions:
            self.governor().start_action(runtime, self, action)

        if self.is_pending_delete() and self.delete_complete():
            self.remove_finalizer(runtime)

    def process_subject_event_handlers(self, runtime, event_name):
        return self.governor().process_subject_event_handlers(runtime, self, event_name)

    def add_finalizer(self, runtime):
        finalizer_id = runtime.crd_domain + '/anarchy'
        finalizers = self.metadata.get('finalizers', [])
        if finalizer_id in finalizers:
            return
        self.patch(runtime, {
            'metadata': {
                'finalizers': finalizers + [finalizer_id]
            }
        })

    def remove_finalizer(self, runtime):
        finalizer_id = runtime.crd_domain + '/anarchy'
        finalizers = self.metadata.get('finalizers', [])
        if finalizer_id not in finalizers:
            return
        self.patch(runtime, {
            'metadata': {
                'finalizers': [s for s in finalizers if s != finalizer_id]
            }
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
        self.status = resource.get('status', None)

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
