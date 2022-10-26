import kubernetes_asyncio
import logging
import os
import socket

from asgi_tools import ResponseError
from datetime import datetime, timezone

from anarchy import Anarchy
from anarchyobject import AnarchyObject
from varsecret import VarSecretMixin

import anarchygovernor
import anarchyrun
import anarchysubject

class AnarchyAction(VarSecretMixin, AnarchyObject):
    kind = 'AnarchyAction'
    plural = 'anarchyactions'
    callback_base_url = os.environ.get('CALLBACK_BASE_URL')

    @classmethod
    async def on_startup(cls):
        await cls.set_callback_base_url()

    @classmethod
    async def set_callback_base_url(cls):
        if cls.callback_base_url and len(cls.callback_base_url) > 8:
            return

        if Anarchy.running_all_in_one:
            cls.callback_base_url = f"http://{socket.gethostbyname(os.environ['HOSTNAME'])}:5000"
            logging.info(f"Using {cls.callback_base_url} as all-in-one callback base url")
            return

        try:
            route = await Anarchy.custom_objects_api.get_namespaced_custom_object(
                group = 'route.openshift.io',
                name = 'anarchy',
                namespace = Anarchy.namespace,
                plural = 'routes',
                version = 'v1',
            )
            route_spec = route.get('spec', {})
            route_host = route_spec['host']
            if route_spec.get('tls', None):
                cls.callback_base_url = f"https://{route_host}"
            else:
                cls.callback_base_url = f"http://{route_host}"
            logging.info(f"Set callback base url from OpenShift route: {cls.callback_base_url}")

        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status == 404:
                await cls.create_callback_route()
            else:
                operator_logger.warning("Unable to determine a callback url. Callbacks will not function.")
                cls.callback_base_url = None

    @classmethod
    async def create_callback_route(cls):
        try:
            service = await Anarchy.core_v1_api.read_namespaced_service(name="anarchy", namespace=Anarchy.namespace)
            route = await Anarchy.custom_objects_api.create_namespaced_custom_object(
                group = 'route.openshift.io',
                namespace = Anarchy.namespace,
                plural = 'routes',
                version = 'v1',
                body = {
                    "apiVersion": "route.openshift.io/v1",
                    "kind": "Route",
                    "metadata": {
                        "name": "anarchy",
                        "namespace": Anarchy.namespace,
                        "ownerReferences": [{
                            "apiVersion": "v1",
                            "controller": True,
                            "kind": "Service",
                            "name": service.metadata.name,
                            "uid": service.metadata.uid,
                        }]
                    },
                    "spec": {
                        "port": {
                            "targetPort": "api",
                        },
                        "tls": {
                            "termination": "edge",
                        },
                        "to": {
                            "kind": "Service",
                            "name": service.metadata.name,
                        }
                    }
                }
            )
            cls.callback_base_url = f"https://{route['spec']['host']}"
            logging.info(f"Created OpenShift route and set callback base url: {cls.callback_base_url}")
        except kubernetes_asyncio.client.rest.ApiException as e:
            logging.exception("Failed to create anarchy route")

    @property
    def action(self):
        return self.spec['action']

    @property
    def after(self):
        return self.spec.get('after')

    @property
    def after_datetime(self):
        after_timestamp = self.after
        if after_timestamp:
            return datetime.strptime(
                after_timestamp, '%Y-%m-%dT%H:%M:%SZ'
            ).replace(tzinfo=timezone.utc)
        else:
            return datetime.now(timezone.utc)

    @property
    def callback_token(self):
        return self.spec.get('callbackToken')

    @property
    def callback_url(self):
        if self.callback_base_url:
            return f"{self.callback_base_url}/action/{self.name}"

    @property
    def is_finished(self):
        return Anarchy.finished_label in self.labels

    @property
    def governor_name(self):
        return self.spec['governorRef']['name']

    @property
    def subject_name(self):
        return self.spec['subjectRef']['name']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def check_callback_auth(self, request):
        auth_header = request.headers.get('Authorization')
        if not self.callback_token \
        or auth_header != f"Bearer {self.callback_token}":
            raise ResponseError(f"{self} invalid auth token")

    async def cancel(self):
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.canceled_label.replace('/', '~1')}",
            "value": ""
        }])

    async def export(self):
        ret = await super().export()
        ret['spec']['callbackUrl'] = self.callback_url
        return ret

    async def get_action_config(self):
        anarchy_governor = await self.get_governor()
        return anarchy_governor.get_action_config(self.action)

    async def get_callback_handler(self, callback_name):
        anarchy_governor = await self.get_governor()
        return anarchy_governor.get_action_callback_handler(self.action, callback_name)

    async def get_governor(self):
        return await anarchygovernor.AnarchyGovernor.get(self.governor_name)

    async def get_subject(self):
        return await anarchysubject.AnarchySubject.get(self.subject_name)

    async def handle_callback(self, callback_data, callback_name):
        if not callback_name:
            action_config = await self.get_action_config()
            callback_name_parameter = action_config.callback_name_parameter
            callback_name = callback_data.get(callback_name_parameter or 'event')
            if not callback_name:
                raise ResponseError.NOT_FOUND(f"{self} could not determine callback")

        callback_handler = await self.get_callback_handler(callback_name)
        if not callback_handler:
            raise ResponseError.NOT_FOUND(f"{self} has no handler for {callback_name} callback")

        anarchy_run = await anarchyrun.AnarchyRun.create(
            anarchy_action = self,
            handler = {
                "name": callback_name,
                "type": "actionCallback",
                "vars": {
                    "anarchy_action_callback_data": callback_data,
                }
            }
        )
        logging.info("Created {anarchy_run} to process {self} {callback_name} callback")
