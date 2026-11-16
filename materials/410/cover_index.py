"""按版本展开的历史覆盖二级索引。

每个行版本（含重插产生的新身份）在索引中对应一条不可变索引项：

    key   = (category, row_id 的 UTF-8 字节, version_id)   —— 全局有序
    value = (version_id, row_id, category, amount?, start_commit, end_commit)

可见性是每项自带的提交区间 [start_commit, end_commit)：
  * 新版本发布时开启区间；
  * 同一 row_id 的后继版本（put 或 delete 墓碑）发布时，把当前开放项的
    end_commit 关闭为后继提交号。
因此快照 s 上 key 命中的项，当且仅当 start_commit <= s < end_commit 时
是该 row_id 在 s 上的可见身份；被关闭的历史项不会被当作当时可见行。

为什么覆盖值可以直接返回：索引项在发布时完整复制了该版本身份的
category/amount，且该版本不可变，区间又精确刻画了它的可见提交范围；
命中且区间覆盖快照时，该项就是当时可见版本本身的值，无需回表。
需要回表（非覆盖投影/审计模式）时，按命中项携带的 version_id 身份读取，
绝不按 row_id 取最新行。
"""

from bisect import bisect_left, insort

# 墓碑版本不建普通索引项，用 end_commit 关闭旧项即可；end_commit=INF 表示仍开放。
INF = float('inf')
# 非覆盖模式下索引不携带 amount，用哨兵与真实的 NULL(None) 区分。
NOT_COVERED = object()


class Entry:
    __slots__ = ('category', 'rid_bytes', 'version_id', 'row_id',
                 'amount', 'start_commit', 'end_commit')

    def __init__(self, category, rid_bytes, version_id, row_id,
                 amount, start_commit, end_commit):
        self.category = category
        self.rid_bytes = rid_bytes
        self.version_id = version_id
        self.row_id = row_id
        self.amount = amount
        self.start_commit = start_commit
        self.end_commit = end_commit

    def key(self):
        return self.category, self.rid_bytes, self.version_id

    def __lt__(self, other):
        # other 既可能是另一个 Entry，也可能是 bisect 用的纯 key 元组。
        if isinstance(other, Entry):
            other = other.key()
        return self.key() < other

    def as_record(self):
        return dict(version_id=self.version_id, row_id=self.row_id,
                    category=self.category,
                    **({'amount': self.amount} if self.amount is not NOT_COVERED else {}),
                    start_commit=self.start_commit, end_commit=self.end_commit)


class CoverIndex:
    def __init__(self, covering=True):
        self.covering = covering
        self.entries = []                       # 始终按 key 有序
        self.open_by_rid = {}                   # row_id -> 当前开放 Entry
        # 审计计数：最近一次 range_scan 的实际访问量，以及累计量。
        self.examined = 0
        self.returned = 0
        self.total_examined = 0
        self.total_returned = 0

    def publish(self, commit, heap_versions, changes):
        """与行堆的同一个 commit 协同发布。

        heap_versions: Heap.publish 返回的 version_id 列表，与 changes 对齐。
        只在全部变更已通过校验后调用；先关闭旧区间再追加新项，
        读者固定旧快照时仍只看到旧区间。
        """
        for version_id, (rid, payload) in zip(heap_versions, changes):
            open_entry = self.open_by_rid.get(rid)
            if open_entry is not None:
                open_entry.end_commit = commit      # 旧身份在本提交起不再可见
            if payload is not None:                 # put：新身份开启新区间
                category, amount = payload
                entry = Entry(
                    category, rid.encode('utf-8'), version_id, rid,
                    amount if self.covering else NOT_COVERED,
                    commit, INF)
                # 同一提交内不同 row_id，且 key 含 version_id，insort 保持全局有序。
                insort(self.entries, entry)
                self.open_by_rid[rid] = entry
            else:                                   # delete：墓碑不建项
                self.open_by_rid.pop(rid, None)

    def range_scan(self, snapshot, lo, hi):
        """在指定快照上扫描 lo <= category < hi 的可见索引项。

        用 bisect 把候选压缩到 category 键区间内的索引项（真正通过二级索引
        缩小候选，而不是全量历史扫描），再逐项用提交区间核对快照可见性。
        返回项已按 (category, row_id UTF-8 字节序, version_id) 有序；
        每个 row_id 至多有一个区间覆盖快照，故结果天然无重复。
        """
        self.examined = 0
        self.returned = 0
        if lo >= hi:
            return []
        start = bisect_left(self.entries, (lo, b'', 0))
        # 任何 category == hi 的项其 key 为 (hi, rid_bytes>=b'', vid>=1)，
        # 必然大于 (hi, b'', 0)，故该位置正好是首个 category >= hi 的项。
        stop = bisect_left(self.entries, (hi, b'', 0))
        result = []
        for entry in self.entries[start:stop]:
            self.examined += 1
            if entry.start_commit <= snapshot < entry.end_commit:
                self.returned += 1
                result.append(entry)
        self.total_examined += self.examined
        self.total_returned += self.returned
        return result

    def entry_count(self):
        return len(self.entries)
