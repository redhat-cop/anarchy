import logging
import os
import requests
import requests.auth
import tempfile

logger = logging.getLogger('anarchy')

class AnarchyAPI(object):
    """API for Anarchy Governor"""

    apis = {}

    @classmethod
    def register(_class, resource):
        api = _class(resource)
        logger.info("Registered api %s (%s)", api.name, api.resource_version)
        AnarchyAPI.apis[api.name] = api
        return api

    @classmethod
    def unregister(_class, api):
        if isinstance(api, AnarchyAPI):
            del AnarchyAPI.apis[api.name]
        else:
            del AnarchyAPI.apis[api]

    @classmethod
    def get(_class, name):
        return AnarchyAPI.apis.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.sanity_check()

    def sanity_check(self):
        assert 'baseUrl' in self.spec, \
            'spec must include baseUrl'
        if self.is_https:
            assert 'caCertificate' in self.spec, \
                'spec must include caCertificate'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def base_url(self):
        return self.spec['baseUrl']

    @property
    def basic_auth(self):
        return self.spec.get('basicAuth', None)

    @property
    def ca_certificate(self):
        return self.spec.get('caCertificate', None)

    @property
    def ca_certificate_file(self):
        if hasattr(self, '_ca_certificate_file') \
        and os.path.isfile(self._ca_certificate_file):
            return self._ca_certificate_file

        if not self.ca_certificate:
            logger.warning('Disabling TLS certificate verification for %s', self.name)
            return False

        cert = tempfile.NamedTemporaryFile(delete=False, mode='w')
        cert.write(self.ca_certificate)
        cert.close()
        self._ca_certificate_file = cert.name
        return cert.name

    @property
    def callback_event_name_parameter(self):
        return self.spec.get('callbackEventNameParameter', None)

    @property
    def callback_token_parameter(self):
        return self.spec.get('callbackTokenParameter', None)

    @property
    def callback_url_parameter(self):
        return self.spec.get('callbackUrlParameter', None)

    @property
    def data(self):
        return self.spec.get('data', None)

    @property
    def digest_auth(self):
        return self.spec.get('digestAuth', None)

    @property
    def headers(self):
        return self.spec.get('headers', [])

    @property
    def is_https(self):
        return self.spec['baseUrl'].startswith('https://')

    @property
    def method(self):
        return self.spec.get('method', 'POST')

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def parameter_secrets(self):
        return self.spec.get('parameterSecrets', [])

    @property
    def path(self):
        return self.spec.get('path', '/')

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def auth(self, runtime):
        if self.basic_auth:
            secret_data = runtime.get_secret_data(self.basic_auth['secretName'])
            return requests.auth.HTTPBasicAuth(secret_data['user'], secret_data['password'])
        if self.digest_auth:
            secret_data = runtime.get_secret_data(self.digest_auth['secretName'])
            return requests.auth.HTTPDigestAuth(secret_data['user'], secret_data['password'])
        else:
            return None

    def call(self, runtime, path, method, headers, data):
        method = method or self.method
        path = path or self.path
        url = self.base_url + path

        logger.debug("%s to %s", method, url)
        logger.debug(headers)

        resp = None
        if method == 'GET':
            resp = requests.get(
                url,
                auth=self.auth(runtime),
                headers=headers,
                params=data,
                verify=self.ca_certificate_file
            )
        elif method == 'DELETE':
            resp = requests.delete(
                url,
                auth=self.auth(runtime),
                headers=headers,
                params=data,
                verify=self.ca_certificate_file
            )
        elif method == 'POST':
            resp = requests.post(
                url,
                auth=self.auth(runtime),
                headers=headers,
                data=data,
                verify=self.ca_certificate_file
            )
        elif method == 'PUT':
            resp = requests.put(
                url,
                auth=self.auth(runtime),
                headers=headers,
                data=data,
                verify=self.ca_certificate_file
            )
        else:
            raise Exception('unknown request method ' + method)

        return resp, url
