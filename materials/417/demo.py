"""End-to-end demonstration.

Stage 1: on the static corpus, the repaired engine's pruned top-1 equals the
independent full-scoring oracle.
Stage 2: the 20 common-word (beta) documents are deleted. With current
statistics B's real BM25 score overtakes A. The legacy frozen-bound engine
still skips B's block and wrongly returns A; the repaired engine exports fresh
bounds under the new N/df/avgdl, reads B's payload and returns the true top-1,
again matching the oracle. Every skip is emitted with its applicability basis.
"""
import json

from inverted import Index
from legacy import LegacyIndex, legacy_query
from oracle import oracle
from ranker import query
from inspect import skip_evidence

TERMS = ['alpha', 'beta']


def build_docs():
    docs = {'A': ['alpha'], 'B': ['beta'] * 10}
    docs.update({f'Z{i:02}': ['beta'] for i in range(20)})
    return docs


def main():
    out = {}

    # ---- Stage 1: static corpus ------------------------------------------
    docs = build_docs()
    index = Index(docs, block_size=1)
    rows, audit = query(index, TERMS, 1)
    oracle_rows, oracle_info = oracle(index, TERMS, 1)
    out['stage1_static'] = {
        'pruned_result': rows,
        'oracle_result': oracle_rows,
        'matches_oracle': rows == oracle_rows,
        'statistics_used': audit['statistics_used'],
        'scored': audit['scored'],
        'raw_document_accesses': audit['raw_document_accesses'],
        'note': '静态语料：块剪枝top1与逐文档全评分oracle完全一致。',
    }

    # ---- Stage 2: delete the 20 common beta documents --------------------
    for i in range(20):
        index.remove(f'Z{i:02}')

    # Repaired engine: fresh bound export under current revision/statistics.
    actual, audit2 = query(index, TERMS, 1)
    expected, oracle_info2 = oracle(index, TERMS, 1)

    # Legacy engine: same deletion, but frozen build-time bounds stay in use.
    legacy = LegacyIndex(build_docs(), block_size=1)
    for i in range(20):
        legacy.remove(f'Z{i:02}')
    legacy_rows, legacy_audit = legacy_query(legacy, TERMS, 1)

    # Why the legacy skip is invalid now: B's frozen bound vs its true score.
    b_current = expected[0][1] if expected[0][0] == 'B' else None
    out['stage2_after_deletes'] = {
        'current_statistics': audit2['statistics_used'],
        'repaired_result': actual,
        'oracle_result': expected,
        'repaired_matches_oracle': actual == expected,
        'repaired_scored': audit2['scored'],
        'repaired_raw_document_accesses': audit2['raw_document_accesses'],
        'repaired_skip_evidence': skip_evidence(audit2),
        'legacy_frozen_bound_engine': {
            'result': legacy_rows,
            'frozen_stats_used_for_bounds': legacy_audit[
                'frozen_revision_stats'],
            'skipped_blocks': legacy_audit['skipped'],
            'wrong': legacy_rows != expected,
            'observation': '旧bound在建库统计下冻结；删除20篇beta后idf/avgdl已变，'
                           '它仍把B所在块剪掉，返回A，漏掉真正第一名。',
        },
        'b_now_beats_a': actual[0][0] == 'B',
        'b_current_score': b_current,
        'a_current_score': dict(actual).get('A'),
        'conclusion': '修复引擎按当前revision的N/df/avgdl与本次参数即时导出上界，'
                      '每次跳过都附适用性依据；B真实分超过A后其块不再被剪，结果与oracle一致。',
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
