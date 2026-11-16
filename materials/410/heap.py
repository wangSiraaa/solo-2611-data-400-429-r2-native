"""多版本行堆（append-only）。

每次写入都追加一个*不可变版本*，版本由单调递增的 version_id 标识身份；
row_id 删除后重新出现会得到新的版本身份，旧身份版本永远不会被新值覆盖。
delete 产生 deleted=True 的墓碑版本。

一个 row_id 在快照 s 上的可见行，是其所有 start_commit <= s 的版本中
身份最新（version_id 最大）的那一个；墓碑版本表示该快照上缺行（不同于 amount=NULL）。
"""


class Heap:
    def __init__(self):
        self.commit = 0
        self.versions = []          # 按下标即 version_id 排列，version_id == 列表下标+1

    def publish(self, commit, changes):
        """把一个已提交业务事务的最终变化原子追加到行堆。

        changes: [(row_id, None(删除) | (category, amount)), ...]
                 每个 row_id 在同一提交中至多出现一次（批内反复改动已由 Engine 折叠）。
        返回每个变化对应的新版本身份 version_id（顺序与 changes 一致）。
        """
        version_ids = []
        for rid, payload in changes:
            version_id = len(self.versions) + 1
            if payload is None:
                record = dict(version_id=version_id, row_id=rid,
                              commit=commit, deleted=True)
            else:
                category, amount = payload
                record = dict(version_id=version_id, row_id=rid,
                              category=category, amount=amount,
                              commit=commit, deleted=False)
            self.versions.append(record)
            version_ids.append(version_id)
        self.commit = commit
        return version_ids

    def read_version(self, version_id):
        """按*版本身份*回表读取（不是按 row_id 取最新行）。"""
        return self.versions[version_id - 1].copy()

    def current(self, rid):
        for record in reversed(self.versions):
            if record['row_id'] == rid:
                return record.copy()
        return None

    def full_scan_at(self, snapshot):
        """独立全表重放：仅供 oracle/demo 对照，查询路径不允许使用。"""
        visible = {}
        for version in self.versions:
            if version['commit'] <= snapshot:
                visible[version['row_id']] = version
        return [v.copy() for v in visible.values() if not v['deleted']]

    def history_length(self):
        return len(self.versions)
