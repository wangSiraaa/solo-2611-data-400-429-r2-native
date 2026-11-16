"""Existing first-fit per-member placement; ordinary one-worker stages work."""
class Allocator:
    def __init__(self, nodes):
        self.nodes = {name: dict(capacity) for name, capacity in nodes.items()}
        self.held = {}

    def available(self):
        return {node: {resource: total - sum(
            hold['resources'].get(resource, 0) for hold in self.held.values()
            if hold['node'] == node) for resource, total in capacity.items()}
            for node, capacity in self.nodes.items()}

    def place_one(self, owner, resources, allowed_nodes):
        if owner in self.held:
            raise ValueError('owner is already reserved')
        free = self.available()
        for node in allowed_nodes:
            if node not in free:
                raise ValueError('unknown execution node')
            if all(amount <= free[node].get(resource, 0)
                   for resource, amount in resources.items()):
                self.held[owner] = {'node': node, 'resources': dict(resources)}
                return node
        return None

    def release(self, owner):
        self.held.pop(owner, None)
