from copy import deepcopy

class AnarchyRunObject:
    def __init__(self, definition):
        self.definition = definition

    def __str__(self):
        return f"{self.kind} {self.name}"

    @property
    def metadata(self):
        return self.definition['metadata']

    @property
    def name(self):
        return self.metadata['name']

    @property
    def spec(self):
        return self.definition['spec']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def export_for_inventory(self):
        """
        Convert to dictionary with redundant structure for compatibility.
        """
        ret = deepcopy(self.definition)
        for item in ('name', 'namespace', 'uid'):
            ret[item] = self.metadata[item]
        ret['vars'] = ret['spec'].get('vars', {})
        return ret
