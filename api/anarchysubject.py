import hashlib
import json
import logging
import re

from asgi_tools import ResponseError
from base64 import b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from anarchy import Anarchy
from anarchyobject import AnarchyObject
from deep_merge import deep_merge
from random_string import random_string
from varsecret import VarSecretMixin

import anarchyaction
import anarchygovernor

def make_spec_sha256(spec):
    return b64encode(hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(',',':')
    ).encode('utf-8')).digest()).decode('utf-8')

class AnarchySubject(VarSecretMixin, AnarchyObject):
    kind = 'AnarchySubject'
    plural = 'anarchysubjects'

    @property
    def active_action_name(self):
        return self.status.get('activeAction', {}).get('name')

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def pending_action_names(self):
        return [ item['name'] for item in self.status.get('pendingActions', []) ]

    async def export(self, vars):
        """
        Export definition while reading all var secrets into vars.
        """
        ret = deepcopy(self.definition)
        ret['vars'] = deepcopy(vars)

        for var_secret in self.var_secrets:
            secret_data = await var_secret.get_data()
            if 'vars' in ret['spec']:
                ret['spec']['vars'].update(secret_data)
            else:
                ret['spec']['vars'] = secret_data
            ret['vars'].update(secret_data)
        return ret

    async def get_governor(self):
        return await anarchygovernor.AnarchyGovernor.get(self.governor_name)

    async def patch_request(self, patch, skip_update_processing):

        patch_metadata = patch.get('metadata', {})
        for k in patch_metadata.keys():
            if k not in ('annotations', 'labels'):
                raise ResponseError.BAD_REQUEST(f"Unable to set metadata.{k} for {anarchy_subject}")

        patch_metadata_annotations = patch_metadata.get('annotations', {})
        for k in patch_metadata_annotations.keys():
            if k.startswith(f"{Anarchy.domain}/"):
                raise ResponseError.BAD_REQUEST(f"Unable to set metadata.annotations.{k} for {anarchy_subject}")

        patch_metadata_labels = patch_metadata.get('labels', {})
        for k in patch_metadata_labels.keys():
            if k.startswith(f"{Anarchy.domain}/"):
                raise ResponseError.BAD_REQUEST(f"Unable to set metadata.labels.{k} for {anarchy_subject}")

        patch_spec = patch.get('spec', {})
        for k in patch_spec.keys():
            if k != 'vars':
                raise ResponseError.BAD_REQUEST(f"Unable to set spec.{k} for {anarchy_subject}")

        patch_vars = patch_spec.get('vars')

        patch_status = patch.get('status', {})
        for k in patch_status.keys():
            if k in (
                'activeAction', 'deleteHandlersStarted', 'diffBase', 'kopf',
                'pendingActions', 'runStatus', 'runStatusMessage', 'runs', 'supportedActions'
            ):
                raise ResponseError.BAD_REQUEST(f"Unable to set status.{k} for {anarchy_subject}")

        while True:
            updated_definition = deepcopy(self.definition)
            if patch_metadata_annotations:
                if 'annotations' in updated_definition['metadata']:
                    updated_definition['metadata']['annotations'].update(patch_metadata_annotations)
                else:
                    updated_definition['metadata']['annotations'] = patch_metadata_annotations

            if patch_metadata_labels:
                updated_definition['metadata']['labels'].update(patch_metadata_labels)

            if patch_vars:
                if 'vars' in updated_definition['spec']:
                    deep_merge(updated_definition['spec']['vars'], patch_vars)
                else:
                    updated_definition['spec']['vars'] = patch_vars

            if skip_update_processing:
                spec_sha256 = make_spec_sha256(updated_definition['spec'])
                if 'annotations' in updated_definition['metadata']:
                    updated_definition['metadata']['annotations'][Anarchy.spec_sha256_annotation] = spec_sha256
                else:
                    updated_definition['metadata']['annotations'] = {Anarchy.spec_sha256_annotation: spec_sha256}

            try:
                await self.replace(updated_definition)
                break
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    raise ResponseError.NOT_FOUND(f"{self} not found")
                elif e.status == 409:
                    await self.refresh()
                else:
                    raise

        if patch_status:
            await self.merge_patch_status(patch_status)

    async def schedule_action(self, action_name, action_vars, after_timestamp, cancel_actions, is_delete_handler=False):
        anarchy_governor = await self.get_governor()

        if after_timestamp and not re.match(r'\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ', after_timestamp):
            raise ResponseError.BAD_REQUEST("Invalid after timestamp format")
        if after_timestamp:
            after_datetime = datetime.strptime(after_timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        else:
            after_datetime = datetime.now(timezone.utc)

        fetch_action_names = [name for name in cancel_actions]
        if action_name and action_name not in fetch_action_names:
            fetch_action_names.append(action_name)

        anarchy_actions = await anarchyaction.AnarchyAction.list(
            label_selector =
                f"{Anarchy.subject_label}={self.name},"
                f"{Anarchy.action_label} in ({','.join(fetch_action_names)}),"
                f"!{Anarchy.canceled_label},"
                f"!{Anarchy.finished_label}"
        )
        reschedule_action = None
        for anarchy_action in anarchy_actions:
            if anarchy_action.action in cancel_actions:
                await anarchy_action.cancel()
            elif action_name \
            and anarchy_action.name == action_name \
            and anarchy_action.vars == action_vars \
            and anarchy_action.after_datetime > datetime.now(timezone.utc):
                reschedule_action = anarchy_action

        if not action_name:
            return

        if reschedule_action:
            # Only reschedule if it causes the action to run sooner.
            # If it is desired to run later then the action must be canceled
            # and a new action created.
            if reschedule_action.after_datetime > after_datetime:
                await reschedule_action.json_patch([{
                    "op": "add",
                    "path": "/spec/after",
                    "value": after_datetime.strftime('%FT%TZ'),
                }])
            return reschedule_action

        labels = {
            Anarchy.action_label: action_name,
            Anarchy.governor_label: anarchy_governor.name,
            Anarchy.subject_label: self.name,
        }
        if is_delete_handler:
            labels[Anarchy.delete_handler_label] = ''

        return await anarchyaction.AnarchyAction.create({
            "metadata": {
                "generateName": f"{self.name}-{action_name}-",
                "labels": labels,
                "ownerReferences": [self.as_owner_ref()],
            },
            "spec": {
                "action": action_name,
                "after": after_datetime.strftime('%FT%TZ'),
                "callbackToken": random_string(32),
                "governorRef": anarchy_governor.as_reference(),
                "subjectRef": self.as_reference(),
                "vars": action_vars,
            }
        })
