"""公开小基准：240 条互异分类上的单分类旧快照查询。

装载 benchmark.json 的 seed（r000..r239，category 0..239 互异），取快照后
把 r017 迁到 category 1000，再在*旧快照*上查询单分类区间 [17,18)。

约束：该查询真正经二级索引缩小候选，检查的堆版本数 < 24（见
benchmark.json: max_heap_versions_exclusive）。覆盖模式不回表（堆版本数 0）；
回表模式只为每个命中索引项按其版本身份读一次堆版本，因此等于命中数。
两个模式都给出实际访问计数与结果正确性断言。
"""

import json
from pathlib import Path
from engine import Engine

CASE = json.loads(Path(__file__).with_name('benchmark.json').read_text())
LIMIT = CASE['max_heap_versions_exclusive']


def run(covering):
    engine = Engine(covering=covering)
    for row in CASE['seed']:
        engine.put(row['row_id'], row['category'], row['amount'])
    old_snapshot = engine.snapshot()          # 报表固定的旧快照

    for op in CASE['after_snapshot']:         # 事后迁移：r017 17 -> 1000
        engine.put(op['row_id'], op['category'], op['amount'])
    engine.snapshot()

    q = CASE['query']
    result = engine.query(old_snapshot, q['lo'], q['hi'], q['projection'])
    audit = dict(engine.audit)
    audit['index_total_entries'] = engine.index.entry_count()
    audit['heap_total_versions'] = engine.heap.history_length()
    return result, audit


def main():
    report = {}
    for mode, covering in (('covering', True), ('heap_fetch_by_identity', False)):
        result, audit = run(covering)
        assert result == CASE['expected'], (mode, result, CASE['expected'])
        assert audit['heap_versions_read'] < LIMIT, (mode, audit)
        # 索引实际检查项也必须远小于全量历史（240 初始项 + 迁移项）。
        assert audit['index_entries_examined'] < LIMIT, (mode, audit)
        report[mode] = dict(result=result, audit=audit,
                            heap_version_limit=LIMIT,
                            within_limit=audit['heap_versions_read'] < LIMIT)

    report['note'] = (
        '旧快照经索引 category 键区间 bisect 定位，仅检查区间内索引项并按其'
        '提交区间核对可见性；覆盖模式零回表，回表模式按命中项 version_id 身份读堆。')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
