import logging
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
        logger.info("Registered api %s (%s)", api.name(), api.resource_version())
        AnarchyAPI.apis[api.name()] = api
        return api

    @classmethod
    def unregister(_class, api):
        if isinstance(api, AnarchyAPI):
            del AnarchyAPI.apis[api.name()]
        else:
            del AnarchyAPI.apis[api]

    @classmethod
    def get(_class, name):
        return AnarchyAPI.apis.get(name, None)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.sanity_check()
        self._ca_certificate_file = None

    def sanity_check(self):
        assert 'baseUrl' in self.spec, \
            'spec must include baseUrl'
        if self.is_https():
            assert 'caCertificate' in self.spec, \
                'spec must include caCertificate'

    def name(self):
        return self.metadata['name']

    def headers(self):
        return self.spec.get('headers', [])

    def parameters(self):
        return self.spec.get('parameters', {})

    def resource_version(self):
        return self.metadata['resourceVersion']

    def base_url(self):
        return self.spec['baseUrl']

    def is_https(self):
        return self.spec['baseUrl'].startswith('https://')

    def auth(self, runtime):
        if 'digestAuth' in self.spec:
            secret_data = runtime.get_secret_data(self.spec['digestAuth']['secretName'])
            return requests.auth.HTTPDigestAuth(secret_data['user'], secret_data['password'])
        elif 'basicAuth' in self.spec:
            secret_data = runtime.get_secret_data(self.spec['basicAuth']['secretName'])
            return requests.auth.HTTPBasicAuth(secret_data['user'], secret_data['password'])
        else:
            return None

    def ca_certificate(self):
        return self.spec.get('caCertificate', None)

    def ca_certificate_file(self):
        if self._ca_certificate_file:
            return self._ca_certificate_file
        ca_certificate_text = self.spec['caCertificate']

        if not ca_certificate_text:
            logger.warning('Disabling TLS certificate verification for %s', self.name())
            return False

        cert = tempfile.NamedTemporaryFile(delete=False, mode='w')
        cert.write(self.spec['caCertificate'])
        cert.close()
        self._ca_certificate_file = cert.name
        return cert.name

    def call(self, runtime, path, method, headers, data):
        url = self.base_url() + path

        logger.debug("%s to %s", method, url)
        logger.debug(headers)

        resp = None
        if method == 'GET':
            resp = requests.get(
                url,
                auth=self.auth(runtime),
                headers=headers,
                params=data,
                verify=self.ca_certificate_file()
            )
        elif method == 'DELETE':
            resp = requests.delete(
                url,
                auth=self.auth(runtime),
                headers=headers,
                params=data,
                verify=self.ca_certificate_file()
            )
        elif method == 'POST':
            resp = requests.post(
                url,
                auth=self.auth(runtime),
                headers=headers,
                data=data,
                verify=self.ca_certificate_file()
            )
        elif method == 'PUT':
            resp = requests.put(
                url,
                auth=self.auth(runtime),
                headers=headers,
                data=data,
                verify=self.ca_certificate_file()
            )
        else:
            raise Exception('unknown request method ' + method)

        return resp, url
