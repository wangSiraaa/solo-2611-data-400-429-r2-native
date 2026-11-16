"""Append-only multi-version storage for the local optimistic transaction system.

保留提交历史与快照（题目要求 storage.py 保留提交历史和快照）：
  * ``history[rev]`` 是 revision=rev 时刻的完整表（不可变快照）；
  * ``last_write[key]`` 是键 key 最后一次被修改的 revision，供写写检测；
  * ``commits[rev]`` 记录该 revision 是哪个事务、用什么增量发布的，
    冲突证据（validation.py）据此指出“哪个已提交变化使我的读取失效”。

没有任何读锁：snapshot 只读取不可变 dict，事务在 begin 之后的 get/scan
完全不经过全局互斥。锁仅在 publish 的原子发布窗口内使用，不跨事务持有。
"""

import threading


class Store:
    def __init__(self, rows):
        self.revision = 0
        # row_id -> row，每个 revision 一份完整快照
        self.history = {0: {r['row_id']: r.copy() for r in rows}}
        # row_id -> 最后写入该键的 revision
        self.last_write = {r['row_id']: 0 for r in rows}
        # revision -> {'owner': tx 标签, 'delta': {key: old->new}}，用于冲突证据
        self.commits = {}
        # 只保护 publish 的原子发布；读路径不获取它
        self._publish_lock = threading.Lock()

    def snapshot(self, revision):
        """返回某 revision 的完整表副本（只读事务自由取，无锁）。"""
        return {k: v.copy() for k, v in self.history[revision].items()}

    def head(self):
        """当前最新已提交快照的副本。"""
        return self.snapshot(self.revision)

    def begin(self):
        """begin：取得当前快照的 revision。不拿任何锁、不登记互斥。"""
        return self.revision

    def publish(self, writes, owner=None):
        """原子发布一个事务的全部私有变化。

        writes: {row_id: row 或 None(删除)}。要么整批进入新 revision，
        要么（无写）不产生 revision——不存在半批状态。返回新 revision。
        """
        with self._publish_lock:
            rows = self.snapshot(self.revision)
            delta = {}
            for key, value in writes.items():
                old = rows.get(key)
                if value is None:
                    rows.pop(key, None)
                else:
                    rows[key] = value.copy()
                delta[key] = (old.copy() if old else None,
                              value.copy() if value is not None else None)
            if not writes:
                # 纯只读/纯保存点回滚事务：没有可发布的变化，不制造空 revision
                return self.revision
            self.revision += 1
            self.history[self.revision] = rows
            for key in writes:
                self.last_write[key] = self.revision
            self.commits[self.revision] = {'owner': owner, 'delta': delta}
            return self.revision

    def revisions_after(self, revision):
        """begin 之后所有已发布 revision 的升序列表（供冲突证据汇总）。"""
        return [r for r in sorted(self.commits) if r > revision]
