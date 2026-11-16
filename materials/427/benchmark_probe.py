"""Fixed three-population shared-scan feasibility, not the general quality engine."""
import json
import sqlite3
import tempfile
from pathlib import Path
from source import Source


def run():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / 'source.jsonl'
        with path.open('w') as output:
            for number in range(1000):
                output.write(json.dumps({'row_id': '%04d' % number, 'team': 'ABC'[number % 3],
                                         'code': number % 41,
                                         'email': None if number % 17 == 0 else 'email-' + str(number)}) + '\n')
        db = sqlite3.connect(str(root / 'state.sqlite'))
        db.executescript('''
            PRAGMA cache_size=-1024;
            CREATE TABLE population_b(row_id TEXT PRIMARY KEY, code INTEGER);
            CREATE TABLE population_c(row_id TEXT PRIMARY KEY, code INTEGER);
            CREATE TABLE target_a(code INTEGER PRIMARY KEY);
        ''')
        counts = {'A': 0, 'B': 0, 'C': 0}
        not_null_bad = 0
        source = Source(path)
        maximum_row_bytes = 0
        for row in source.scan():
            maximum_row_bytes = max(maximum_row_bytes, len(json.dumps(row).encode()))
            counts[row['team']] += 1
            if row['team'] == 'A':
                not_null_bad += row['email'] is None
                db.execute('INSERT OR IGNORE INTO target_a VALUES (?)', (row['code'],))
            elif row['team'] == 'B':
                db.execute('INSERT INTO population_b VALUES (?,?)', (row['row_id'], row['code']))
            else:
                db.execute('INSERT INTO population_c VALUES (?,?)', (row['row_id'], row['code']))
        del row
        unique_bad = db.execute('''SELECT coalesce(sum(n),0) FROM
            (SELECT count(*) AS n FROM population_b GROUP BY code HAVING count(*)>1)''').fetchone()[0]
        reference_bad = db.execute('''SELECT count(*) FROM population_c c
            WHERE NOT EXISTS (SELECT 1 FROM target_a a WHERE a.code=c.code)''').fetchone()[0]
        actual = {'A_not_null': not_null_bad, 'B_unique': unique_bad, 'C_reference_A': reference_bad}
        # Separate generation-formula oracle, no second decode of the source file.
        expected_a = sum(1 for n in range(1000) if n % 3 == 0 and n % 17 == 0)
        expected_b = 333  # Every residue appears at least eight times in team B.
        expected = {'A_not_null': expected_a, 'B_unique': expected_b, 'C_reference_A': 0}
        assert actual == expected
        db.commit()
        state_bytes = (root / 'state.sqlite').stat().st_size
        db.close()
        return {'scope': 'Fixed A not-null, B unique, C reference-to-A populations only; '
                         'general predicate compiler, NULL/type modes, witnesses and incomplete states remain task work.',
                'source_rows': 1000, 'actual_source_decodes': source.decoded,
                'actual_source_scans': source.scans, 'required_decode_limit': 2000,
                'population_counts': counts, 'actual_violations': actual,
                'independent_expected_violations': expected,
                'maximum_source_row_bytes': maximum_row_bytes,
                'conservative_peak_complete_payload_rows': 4,
                'sqlite_cache_kib': 1024, 'actual_state_file_bytes': state_bytes,
                'state_io_accounting': 'File size is measured, not claimed as SQLite physical IO; '
                                       'the general task must separately report actual state reads/writes.'}


if __name__ == '__main__':
    print(json.dumps(run()))
