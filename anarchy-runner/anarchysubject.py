from copy import deepcopy

from anarchyrunobject import AnarchyRunObject

class AnarchySubject(AnarchyRunObject):
    kind = 'AnarchySubject'

    @property
    def vars(self):
        return self.definition.get('vars', {})

    def export_for_inventory(self):
        """
        Convert to dictionary with redundant structure for compatibility.
        Note that AnarchySubject vars are already provided in a "vars" field
        and may differ from the vars under "spec.vars". The content of "vars"
        is the state of the subject variables at the time the run was scheduled
        while the value of "spec.vars" is the value at the time of run
        processing.
        """
        ret = deepcopy(self.definition)
        for item in ('name', 'namespace', 'uid'):
            ret[item] = self.metadata[item]
        return ret
