class Heap:
    def __init__(self):
        self.commit = 0
        self.versions = []

    def put(self, rid, category, amount):
        self.commit += 1
        self.versions.append(dict(row_id=rid, category=category, amount=amount,
                                  commit=self.commit, deleted=False))
        return self.versions[-1].copy()

    def delete(self, rid):
        self.commit += 1
        self.versions.append(dict(row_id=rid, commit=self.commit, deleted=True))
        return self.versions[-1].copy()

    def current(self, rid):
        candidates = [v for v in self.versions if v['row_id'] == rid]
        return candidates[-1].copy() if candidates else None

    def full_scan_at(self, snapshot):
        visible = {}
        for version in self.versions:
            if version['commit'] <= snapshot:
                visible[version['row_id']] = version
        return [v.copy() for v in visible.values() if not v['deleted']]
