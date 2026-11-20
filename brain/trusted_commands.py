"""Route explicit review commands outside the model using the existing voice UI."""


class TrustedCommands:
    def __init__(self, controllers):
        self.controllers = tuple(controllers)

    def handle(self, text, *, confirm, still_current):
        for controller in self.controllers:
            result = controller.handle(text, confirm=confirm, still_current=still_current)
            if result is not None:
                # Switching action families invalidates a prior review in another
                # controller; only the selected review remains ready to confirm.
                for other in self.controllers:
                    if other is not controller:
                        other.clear()
                return result
        return None

    def clear(self):
        for controller in self.controllers:
            controller.clear()


def configured_trusted_commands(hub, identity):
    if not isinstance(identity, dict) or identity.get('role') not in {'adult', 'elder'}:
        return None
    from brain.message_review import configured_message_review
    from brain.commerce_review import configured_commerce_review
    from brain.business_review import configured_business_review
    controllers = [controller for factory in (configured_message_review, configured_commerce_review, configured_business_review)
                   if (controller := factory(hub, identity)) is not None]
    return TrustedCommands(controllers) if controllers else None
