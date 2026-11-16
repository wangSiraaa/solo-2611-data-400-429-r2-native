import json
from pathlib import Path
class DatasetWriter:
    def __init__(self,connection,transactions,directory):
        self.connection=connection;self.transactions=transactions;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        connection.execute('CREATE TABLE IF NOT EXISTS facts(row_id TEXT PRIMARY KEY,dataset TEXT,value INTEGER)')
    def put(self,row_id,dataset,value):
        self.connection.execute('INSERT INTO facts VALUES(?,?,?) ON CONFLICT(row_id) DO UPDATE SET dataset=excluded.dataset,value=excluded.value',(row_id,dataset,value))
    def request_export(self,export_id,dataset):
        def publish():
            rows=self.connection.execute('SELECT row_id,value FROM facts WHERE dataset=? ORDER BY row_id',(dataset,)).fetchall()
            (self.directory/(export_id+'.json')).write_text(json.dumps({'dataset':dataset,'rows':rows}))
        self.transactions.on_commit(publish)
