"""Run 段与 manifest 管理。

一个 run 是一个不可变 JSON 文件，manifest.json 保存当前有效 run 的 id 顺序。
meta.json 记录该库曾经导入过的最大 seq（新导入必须严格更大），
journal.json 记录每次 compact 的回收/保留依据，供 inspect 检查。
"""
import json
from pathlib import Path

KEY_MIN = -10 ** 9
KEY_MAX = 10 ** 9
MAX_RUNS = 16
MAX_PHYSICAL_RECORDS = 1000


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / 'manifest.json'
        self.meta_path = self.root / 'meta.json'
        self.journal_path = self.root / 'journal.json'
        if not self.manifest.exists():
            self.manifest.write_text('[]')
        if not self.meta_path.exists():
            self.meta_path.write_text(json.dumps({'max_seq': 0}))
        if not self.journal_path.exists():
            self.journal_path.write_text('[]')

    # ---- 基础存取 ----
    def ids(self):
        return json.loads(self.manifest.read_text())

    def read(self, run_id):
        if run_id not in self.ids():
            raise KeyError(run_id)
        return json.loads((self.root / (run_id + '.json')).read_text())

    def all_records(self):
        for run_id in self.ids():
            for rec in self.read(run_id):
                yield rec

    def physical_record_count(self):
        return sum(len(self.read(r)) for r in self.ids())

    def max_seq(self):
        return json.loads(self.meta_path.read_text())['max_seq']

    def journal(self):
        return json.loads(self.journal_path.read_text())

    def append_journal(self, entry):
        entries = self.journal()
        entries.append(entry)
        self.journal_path.write_text(json.dumps(entries, ensure_ascii=False))

    # ---- 校验 ----
    @staticmethod
    def _valid_run_id(run_id):
        return isinstance(run_id, str) and len(run_id) > 0 and run_id.isascii() and run_id.isalnum()

    @staticmethod
    def _is_int(x):
        return isinstance(x, int) and not isinstance(x, bool)

    def _validate_records(self, records, known_seqs, prior_max_seq):
        if not isinstance(records, list) or not records:
            raise ValueError('records must be a non-empty list')
        batch_seen = set()
        batch_max = prior_max_seq
        for r in records:
            if not isinstance(r, dict) or 'kind' not in r or 'seq' not in r:
                raise ValueError('record shape')
            seq = r['seq']
            if not self._is_int(seq) or seq <= 0:
                raise ValueError('seq must be a positive integer')
            if seq in known_seqs or seq in batch_seen:
                raise ValueError('duplicate seq: %r' % seq)
            if seq <= prior_max_seq:
                raise ValueError('new seq %r must exceed all previously imported seqs (<= %r)'
                                 % (seq, prior_max_seq))
            batch_seen.add(seq)
            batch_max = max(batch_max, seq)
            if r['kind'] == 'put':
                key = r.get('key')
                value = r.get('value')
                if not self._is_int(key) or not KEY_MIN <= key <= KEY_MAX:
                    raise ValueError('put key out of range')
                if not isinstance(value, str) or len(value) == 0:
                    raise ValueError('put value must be non-empty UTF-8 text')
            elif r['kind'] == 'erase':
                lo, hi = r.get('lo'), r.get('hi')
                if not (self._is_int(lo) and self._is_int(hi)):
                    raise ValueError('erase bounds must be integers')
                if not (KEY_MIN <= lo < hi <= KEY_MAX):
                    raise ValueError('erase requires %d <= lo < hi <= %d' % (KEY_MIN, KEY_MAX))
            else:
                raise ValueError('unknown record kind: %r' % r.get('kind'))
        return batch_max

    # ---- 变更 ----
    def add(self, run_id, records):
        if not self._valid_run_id(run_id) or run_id in self.ids():
            raise ValueError('run id')
        ids = self.ids()
        if len(ids) >= MAX_RUNS:
            raise ValueError('at most %d active runs' % MAX_RUNS)
        known = {r['seq'] for r in self.all_records()}
        prior_max = self.max_seq()
        batch_max = self._validate_records(records, known, prior_max)
        new_total = self.physical_record_count() + len(records)
        if new_total > MAX_PHYSICAL_RECORDS:
            raise ValueError('at most %d physical records' % MAX_PHYSICAL_RECORDS)
        (self.root / (run_id + '.json')).write_text(
            json.dumps(records, ensure_ascii=False))
        self.manifest.write_text(json.dumps(ids + [run_id]))
        self.meta_path.write_text(json.dumps({'max_seq': max(prior_max, batch_max)}))
        return {'runs': self.ids(), 'records': len(records)}

    def replace(self, selected, output_id, records, journal_entry=None):
        """用新 run 原子（进程内）替换选中 run，并撤下旧 run 文件。"""
        ids = self.ids()
        selected_set = set(selected)
        if not selected or not selected_set <= set(ids):
            raise ValueError('selection must be a non-empty subset of active runs')
        if not self._valid_run_id(output_id) or output_id in ids:
            raise ValueError('output run id')
        if not isinstance(records, list):
            raise ValueError('output records')
        remaining = len(ids) - len(selected_set) + 1
        if remaining > MAX_RUNS:
            raise ValueError('at most %d active runs' % MAX_RUNS)
        if self.physical_record_count() - sum(
                len(self.read(r)) for r in selected_set) + len(records) > MAX_PHYSICAL_RECORDS:
            raise ValueError('at most %d physical records' % MAX_PHYSICAL_RECORDS)
        (self.root / (output_id + '.json')).write_text(
            json.dumps(records, ensure_ascii=False))
        new_ids = [x for x in ids if x not in selected_set] + [output_id]
        self.manifest.write_text(json.dumps(new_ids))
        for old in selected_set:
            stale = self.root / (old + '.json')
            if stale.exists():
                stale.unlink()
        if journal_entry is not None:
            self.append_journal(journal_entry)
        return new_ids
