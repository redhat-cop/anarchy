from anarchyrunobject import AnarchyRunObject

class AnarchyAction(AnarchyRunObject):
    kind = 'AnarchyAction'

    @property
    def action(self):
        return self.spec['action']

    @property
    def callback_token(self):
        return self.spec.get('callbackToken')

    @property
    def callback_url(self):
        return self.spec.get('callbackUrl')
