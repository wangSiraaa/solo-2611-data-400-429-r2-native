"""Pending append is volatile; force really writes/fsyncs the durable log prefix."""
import json
import os
from pathlib import Path


class Journal:
    def __init__(self, root):
        self.path = Path(root) / 'wal.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.pending = []
        self.audit = []

    def durable(self):
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def records(self):
        return self.durable() + list(self.pending)

    def durable_lsn(self):
        records = self.durable()
        return records[-1]['lsn'] if records else 0

    def append(self, record):
        records = self.records()
        entry = {'lsn': (records[-1]['lsn'] if records else 0) + 1, **record}
        self.pending.append(entry)
        return entry['lsn']

    def force(self, through_lsn):
        if through_lsn > (self.records()[-1]['lsn'] if self.records() else 0):
            raise ValueError('requested LSN has not been appended')
        if self.pending:
            with self.path.open('a') as output:
                for entry in self.pending:
                    output.write(json.dumps(entry, sort_keys=True) + '\n')
                output.flush()
                os.fsync(output.fileno())
            self.pending.clear()
        self.audit.append({'operation': 'force', 'requested_lsn': through_lsn,
                           'actual_durable_lsn': self.durable_lsn(),
                           'actual_file_bytes': self.path.stat().st_size})
        return self.durable_lsn()
