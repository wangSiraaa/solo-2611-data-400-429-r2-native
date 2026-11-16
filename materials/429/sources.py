import sqlite3
class Source:
    def __init__(self,rows):
        self.connection=sqlite3.connect(':memory:');self.connection.execute('CREATE TABLE rows(id TEXT PRIMARY KEY,k TEXT,score INTEGER,cost INTEGER)')
        self.connection.executemany('INSERT INTO rows VALUES(:id,:key,:score,:cost)',rows)
        self.connection.execute('CREATE INDEX rank_order ON rows(score DESC,id)');self.connection.execute('CREATE INDEX key_order ON rows(k,score DESC,id)');self.calls=[]
    def top(self,limit):
        rows=self.connection.execute('SELECT id,k,score,cost FROM rows ORDER BY score DESC,id LIMIT ?',(limit,)).fetchall()
        self.calls.append({'kind':'sorted','limit':limit,'returned':len(rows)})
        return [dict(zip(('id','key','score','cost'),row)) for row in rows]
    def lookup(self,key):
        rows=self.connection.execute('SELECT id,k,score,cost FROM rows WHERE k=? ORDER BY score DESC,id',(key,)).fetchall()
        self.calls.append({'kind':'lookup','key':key,'returned':len(rows)})
        return [dict(zip(('id','key','score','cost'),row)) for row in rows]
