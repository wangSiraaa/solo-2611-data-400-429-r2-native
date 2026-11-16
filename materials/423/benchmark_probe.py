"""Fixed selectivity feasibility only, not the general multikey implementation."""
import json
import sqlite3
import tempfile
from pathlib import Path


def run():
    with tempfile.TemporaryDirectory() as directory:
        connection = sqlite3.connect(str(Path(directory) / 'index.sqlite'))
        connection.executescript('''
            PRAGMA cache_size=-1024;
            CREATE TABLE documents(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE keys(tenant TEXT, sku TEXT, id TEXT);
            CREATE INDEX lookup ON keys(tenant,sku,id);
        ''')
        for number in range(500):
            document = {'_id': 'd%03d' % number, 'tenant': 't', 'enabled': True,
                        'items': [{'sku': 'wanted' if number < 10 else 'other', 'qty': number}]}
            connection.execute('INSERT INTO documents VALUES (?,?)',
                               (document['_id'], json.dumps(document)))
            connection.execute('INSERT INTO keys VALUES (?,?,?)',
                               ('t', document['items'][0]['sku'], document['_id']))
        candidates = [row[0] for row in connection.execute(
            'SELECT DISTINCT id FROM keys WHERE tenant=? AND sku=?', ('t', 'wanted'))]
        documents_fetched = 0
        actual = []
        for identity in candidates:
            document = json.loads(connection.execute(
                'SELECT payload FROM documents WHERE id=?', (identity,)).fetchone()[0])
            documents_fetched += 1
            if document['tenant'] == 't' and any(item['sku'] == 'wanted' for item in document['items']):
                actual.append(document['_id'])
        expected = ['d%03d' % number for number in range(10)]
        assert actual == expected
        plan = [list(row) for row in connection.execute(
            'EXPLAIN QUERY PLAN SELECT DISTINCT id FROM keys WHERE tenant=? AND sku=?', ('t', 'wanted'))]
        connection.close()
        return {'scope': 'Fixed tenant/sku selectivity case only; no general index planner or updates.',
                'source_documents': 500, 'expected_matches': 10,
                'actual_matches': actual, 'actual_documents_fetched': documents_fetched,
                'required_fetch_limit': 30, 'query_plan': plan, 'sqlite_cache_kib': 1024}


if __name__ == '__main__':
    print(json.dumps(run()))
