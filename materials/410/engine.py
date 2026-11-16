"""行堆 + 历史覆盖索引协同的 MVCC 引擎。

提交协议（行堆与索引共同遵守）：
  * 一次成功的单项 put/delete 或一个非空 apply 批，恰好分配一个递增 commit；
  * 所有变化先在*私有暂存态*完成校验与折叠，校验全过后才以同一 commit 同时
    发布到行堆与索引，任何非法输入都在发布前抛错，已有状态原样保留；
  * 已取得的快照只对应已发布 commit，apply 进行中不存在可被观察的中间进度。

读取协议：
  * query 先经二级索引的 category 键区间（bisect）缩小候选，再按索引项自带的
    提交区间核对快照可见性；一次查询只使用这一代索引与固定快照，不混用；
  * 覆盖模式下命中项即当时版本身份的值快照，可直接投影；
  * 非覆盖模式按命中项的 version_id 身份回表，绝不按 row_id 取最新行。
"""

from heap import Heap
from cover_index import CoverIndex

CATEGORY_MIN, CATEGORY_MAX = -1000, 1000
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
MAX_BATCH = 100
_PROJECTABLE = ('row_id', 'category', 'amount')


class Engine:
    def __init__(self, covering=True):
        self.heap = Heap()
        self.index = CoverIndex(covering=covering)
        self.covering = covering
        self._taken_snapshots = {0}     # 已产生的快照；空库快照 0 天然有效
        self._live_rids = set()         # 当前已提交的存活 row_id
        # 最近一次 query 的审计（堆版本读取数另含按身份回表次数）。
        self.audit = dict(index_entries_examined=0, index_entries_returned=0,
                          heap_versions_read=0, covering_reads=0,
                          heap_fetches=0)

    # ---------------------------------------------------------------- 写入
    def put(self, rid, category, amount):
        """单项完整替换，一次成功写入取得一个 commit。"""
        self._validate_put(rid, category, amount)
        self._commit([(rid, (category, amount))])

    def delete(self, rid):
        """单项删除，形成墓碑版本并取得一个 commit。"""
        if not isinstance(rid, str):
            raise ValueError('INVALID_ROW_ID')
        self._commit([(rid, None)])

    def apply(self, operations):
        """把最多 100 项有序 put/delete 作为一个业务提交原子发布。

        同一 row_id 可在批内反复改动，以最后状态为准；空批不分配 commit。
        非法类型、越界值、未知操作或删除私有状态中不存在的身份，都会让整批
        失败，行堆、索引、commit 与已取得快照保持原状。
        """
        if not isinstance(operations, list) or len(operations) > MAX_BATCH:
            raise ValueError('INVALID_BATCH')
        if len(operations) == 0:
            # 空批不分配 commit，只返回当前已产生快照。
            return self.snapshot()

        # 私有暂存态：仅本批可见。private_live 是批当前存活身份集合
        # （含本批早先 put 出的新身份），final 折叠每个 row_id 的批末状态。
        private_live = set(self._live_rids)
        final = {}
        for item in operations:
            if not isinstance(item, dict):
                raise ValueError('INVALID_OPERATION')
            op = item.get('op')
            rid = item.get('row_id')
            if not isinstance(rid, str) or rid == '':
                raise ValueError('INVALID_ROW_ID')
            if op == 'put':
                category, amount = item.get('category'), item.get('amount')
                self._validate_put(rid, category, amount)
                final[rid] = (category, amount)
                private_live.add(rid)
            elif op == 'delete':
                # 必须引用这批当前私有状态中存在的身份；批内删过一次即移出，
                # 重复删除、删除不存在身份都会让整批失败。
                if rid not in private_live:
                    raise ValueError('DELETE_UNKNOWN_IDENTITY')
                final[rid] = None
                private_live.discard(rid)
            else:
                raise ValueError('UNKNOWN_OPERATION')

        # 新身份在同批内先插后删时净效果为空：不为它制造悬空墓碑版本；
        # 但非空成功批仍按协议分配一个 commit（见 _commit 的空 changes 处理）。
        changes = [(rid, payload) for rid, payload in final.items()
                   if payload is not None or rid in self._live_rids]
        self._commit(changes)
        # 返回已产生、可立即用于 query 的快照（装载方契约同 engine.snapshot()）。
        return self.snapshot()

    def _validate_put(self, rid, category, amount):
        if not isinstance(rid, str) or rid == '':
            raise ValueError('INVALID_ROW_ID')
        if isinstance(category, bool) or not isinstance(category, int) \
                or not (CATEGORY_MIN <= category <= CATEGORY_MAX):
            raise ValueError('CATEGORY_OUT_OF_RANGE')
        if amount is not None and (
                isinstance(amount, bool) or not isinstance(amount, int)
                or not (INT64_MIN <= amount <= INT64_MAX)):
            raise ValueError('AMOUNT_OUT_OF_RANGE')

    def _commit(self, changes):
        """校验完成后的唯一发布点：行堆与索引同属一个 commit，对外原子可见。

        changes 为空（非空批折叠后无净变化）时仍分配一个 commit，只是不产生版本。
        """
        commit = self.heap.commit + 1
        version_ids = self.heap.publish(commit, changes)
        self.index.publish(commit, version_ids, changes)
        for rid, payload in changes:
            if payload is None:
                self._live_rids.discard(rid)
            else:
                self._live_rids.add(rid)
        return commit

    # ---------------------------------------------------------------- 快照
    def snapshot(self):
        snap = self.heap.commit
        self._taken_snapshots.add(snap)
        return snap

    # ---------------------------------------------------------------- 查询
    def query(self, snapshot, lo, hi, projection):
        if isinstance(snapshot, bool) or not isinstance(snapshot, int):
            raise ValueError('INVALID_SNAPSHOT')
        if snapshot not in self._taken_snapshots:
            raise ValueError('SNAPSHOT_NEVER_TAKEN')
        if snapshot > self.heap.commit:
            raise ValueError('SNAPSHOT_AFTER_CURRENT_COMMIT')
        if isinstance(lo, bool) or isinstance(hi, bool) \
                or not isinstance(lo, int) or not isinstance(hi, int):
            raise ValueError('INVALID_RANGE')
        if not isinstance(projection, (list, tuple)) or len(projection) == 0 \
                or any(col not in _PROJECTABLE for col in projection):
            raise ValueError('INVALID_PROJECTION')

        entries = self.index.range_scan(snapshot, lo, hi)
        rows = []
        heap_reads = covering_reads = 0
        for entry in entries:
            if self.covering:
                # 覆盖读取：entry 是该版本身份发布时的不可变值副本，且其提交
                # 区间覆盖本快照，可直接投影，无需回表。
                record = entry.as_record()
                covering_reads += 1
            else:
                # 回表读取：严格按命中索引项的版本身份读取，不按 row_id 取最新。
                record = self.heap.read_version(entry.version_id)
                heap_reads += 1
                if record['deleted'] or record['commit'] > snapshot:
                    raise RuntimeError('INDEX_HEAP_VISIBILITY_MISMATCH')
            rows.append([record[col] for col in projection])

        self.audit = dict(
            index_entries_examined=self.index.examined,
            index_entries_returned=self.index.returned,
            heap_versions_read=heap_reads,
            covering_reads=covering_reads,
            heap_fetches=heap_reads)
        return rows

    def debug_state(self):
        """失败批前后对比用的完整状态指纹。"""
        return dict(commit=self.heap.commit,
                    heap_versions=self.heap.history_length(),
                    index_entries=self.index.entry_count(),
                    live_rids=sorted(self._live_rids),
                    taken_snapshots=sorted(self._taken_snapshots))
