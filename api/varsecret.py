import json
import kubernetes_asyncio

from base64 import b64decode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from deep_merge import deep_merge

from anarchy import Anarchy

class VarSecret:
    cache = {}

    def __init__(self, definition):
        self.name = definition.get('name')
        self.namespace = definition.get('namespace', Anarchy.namespace)
        self.var = definition.get('var')

    async def get_data(self):
        secret, secret_fetch_datetime = self.cache.get((self.name, self.namespace), (None, None))
        if not secret or datetime.now(timezone.utc) - secret_fetch_datetime > timedelta(minutes=1):
            secret = await Anarchy.core_v1_api.read_namespaced_secret(name=self.name, namespace=self.namespace)
            self.cache[(self.namespace, self.name)] = (secret, datetime.now(timezone.utc))

        data = { k: b64decode(v).decode('utf-8') for (k, v) in secret.data.items() }

        # Attempt to evaluate secret data values as JSON
        for k, v in data.items():
            try:
                data[k] = json.loads(v)
            except json.decoder.JSONDecodeError:
                pass

        return data

class VarSecretMixin:
    @property
    def has_var_secrets(self):
        return 'varSecrets' in self.spec

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return [
            VarSecret(item) for item in self.spec.get('varSecrets', [])
        ]

    async def export(self):
        """
        Export definition while reading all var secrets into vars.
        """
        ret = deepcopy(self.definition)
        if self.has_var_secrets:
            ret['spec']['vars'] = await self.get_vars()
        return ret

    async def get_vars(self):
        ret = deepcopy(self.vars)
        for var_secret in self.var_secrets:
            try:
                secret_data = await var_secret.get_data()
                if var_secret.var:
                    deep_merge(ret, {var_secret.var: secret_data})
                else:
                    deep_merge(ret, secret_data)
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    raise kopf.TemporaryError(
                        f"{self} missing var sercret {var_secret.name} in namespace {var_secret.namespace}",
                        delay = 60,
                    )
                else:
                    raise
        return ret
