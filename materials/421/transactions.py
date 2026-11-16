from contextlib import contextmanager
class Transactions:
    def __init__(self,connection): self.connection=connection;self.scopes=[]
    @contextmanager
    def scope(self):
        depth=len(self.scopes);name='scope_'+str(depth);callbacks=[]
        self.connection.execute('BEGIN' if depth==0 else 'SAVEPOINT '+name)
        self.scopes.append(callbacks)
        try:
            yield self
        except Exception:
            self.connection.execute('ROLLBACK' if depth==0 else 'ROLLBACK TO '+name)
            if depth: self.connection.execute('RELEASE '+name)
            raise
        else:
            self.connection.execute('COMMIT' if depth==0 else 'RELEASE '+name)
            for callback in callbacks: callback()
        finally:self.scopes.pop()
    def on_commit(self,callback): self.scopes[-1].append(callback)
