#!/usr/bin/env python

import kubernetes
import sys

kubernetes.config.load_kube_config()

custom_objects_api = kubernetes.client.CustomObjectsApi()

if len(sys.argv) == 4:
    action = sys.argv[1]
    subject_name = sys.argv[2]
    namespace = sys.argv[3]
elif len(sys.argv) == 3:
    action = sys.argv[1]
    subject_name = sys.argv[2]
    namespace = kubernetes.config.list_kube_config_contexts()[1]['context']['namespace']
else:
    sys.stderr.write("Usage: {0} <action> <subject> [namespace]\n".format(sys.argv[0]))
    sys.exit(1)

anarchy_subject = custom_objects_api.get_namespaced_custom_object(
    'anarchy.gpte.redhat.com', 'v1', namespace, 'anarchysubjects', subject_name
)
anarchy_governor = custom_objects_api.get_namespaced_custom_object(
    'anarchy.gpte.redhat.com', 'v1', namespace, 'anarchygovernors', anarchy_subject['spec']['governor']
)

anarchy_action = custom_objects_api.create_namespaced_custom_object(
    'anarchy.gpte.redhat.com', 'v1', namespace, 'anarchyactions',
    {
        "apiVersion": "anarchy.gpte.redhat.com/v1",
        "kind": "AnarchyAction",
        "metadata": {
            "generateName": "{0}-{1}-".format(subject_name, action),
            "labels": {
                "anarchy.gpte.redhat.com/action": action,
                "anarchy.gpte.redhat.com/governor": anarchy_subject['spec']['governor'],
                "anarchy.gpte.redhat.com/subject": subject_name
            },
            "namespace": namespace,
            "ownerReferences": [{
                "apiVersion": "anarchy.gpte.redhat.com/v1",
                "controller": True,
                "kind": "AnarchySubject",
                "name": subject_name,
                "uid": anarchy_subject['metadata']['uid']
            }],
        },
        "spec": {
            "action": action,
            #"after": "2020-04-08T18:18:13Z",
            #"callbackToken": "...",
            "governorRef": {
                "apiVersion": "anarchy.gpte.redhat.com/v1",
                "kind": "AnarchyGovernor",
                "name": anarchy_governor['metadata']['name'],
                "namespace": namespace,
                "uid": anarchy_governor['metadata']['uid']
            },
            "subjectRef": {
                "apiVersion": "anarchy.gpte.redhat.com/v1",
                "kind": "AnarchySubject",
                "name": subject_name,
                "namespace": namespace,
                "uid": anarchy_subject['metadata']['uid']
            }
        }
    }
)
