#!/usr/bin/env python3

import os
import yaml

def add_object_from_file(template, path):
    resource_yaml = open(path).read()
    resource_yaml = resource_yaml.replace('anarchy.gpte.redhat.com', '${ANARCHY_DOMAIN}')
    template['objects'].append(yaml.safe_load(resource_yaml))

def print_resources_as_template(manifest_dir):
    template = {
        "apiVersion": "template.openshift.io/v1",
        "kind": "Template",
        "metadata": {
            "name": "anarchy-install",
            "annotations": {
                "description": "Anarchy Install"
            },
        },
        "parameters": [{
            "name": "ANARCHY_DOMAIN",
            "description": "Anarchy operator custom resource definition domain",
            "displayName": "Anarchy operator CRD domain",
            "value": "anarchy.gpte.redhat.com"
        }],
        "objects": []
    }

    for root, dirs, files in os.walk(manifest_dir):
        for filename in files:
            if filename.endswith('.yaml'):
                add_object_from_file(template, os.path.join(root, filename))

    print(yaml.safe_dump(template))

def main():
    install_dir = os.path.join(os.path.dirname(__file__), '../install')
    print_resources_as_template(install_dir)

if __name__ == '__main__':
    main()
