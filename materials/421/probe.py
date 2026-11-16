import json,sqlite3,tempfile
from pathlib import Path
from transactions import Transactions
from writer import DatasetWriter
with tempfile.TemporaryDirectory() as folder:
    connection=sqlite3.connect(':memory:',isolation_level=None);tx=Transactions(connection);writer=DatasetWriter(connection,tx,folder)
    with tx.scope():
        writer.put('normal','normal',7);writer.request_export('normal','normal')
    normal=json.loads((Path(folder)/'normal.json').read_text())
    try:
        with tx.scope():
            with tx.scope():
                writer.put('bad','aborted',9);writer.request_export('orphan','aborted')
            raise RuntimeError('outer business validation failed')
    except RuntimeError: pass
    print(json.dumps({'normal_export':normal,'aborted_database_rows':connection.execute("SELECT * FROM facts WHERE dataset='aborted'").fetchall(),
                      'orphan_export_exists':(Path(folder)/'orphan.json').exists(),'orphan_export':json.loads((Path(folder)/'orphan.json').read_text()),
                      'independent_oracle':{'aborted_database_rows':[],'orphan_export_exists':False},
                      'feasibility':'SQLite savepoint release does not commit the outer transaction; deferred export intent can remain rollback-owned until its enclosing root commit.'}))
