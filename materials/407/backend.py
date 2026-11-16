"""Local extended-query backend subset, using real SQLite SQL and sockets."""
import socket,sqlite3,threading,json
from wire import frame,receive

def start_backend():
    client,server=socket.socketpair();audit=[]
    def run():
        db=sqlite3.connect(':memory:',isolation_level=None);db.execute('CREATE TABLE counter(value INTEGER)');db.execute('INSERT INTO counter VALUES (10)')
        statements={};portals={};skipping=False;tx='I'
        def send(kind,payload=b''):
            packet=frame(kind,payload)
            for position in range(0,len(packet),3):server.sendall(packet[position:position+3])
        try:
            while True:
                kind,payload=receive(server)
                if kind=='X':break
                if kind=='S':
                    audit.append({'sync':True,'transaction':tx});skipping=False;send('Z',tx.encode());continue
                if skipping:audit.append({'skipped_frontend':kind});continue
                try:
                    if kind=='P':
                        name,sql=payload.decode().split('\0',1);statements[name]=sql;send('1')
                    elif kind=='B':
                        portal,statement=payload.decode().split('\0');portals[portal]=statements[statement];send('2')
                    elif kind=='E':
                        sql=portals[payload.decode()]
                        if tx=='E' and sql.upper()!='ROLLBACK':raise ValueError('25P02 transaction is aborted')
                        cursor=db.execute(sql);audit.append({'executed_sql':sql})
                        if sql.upper()=='BEGIN':tx='T'
                        elif sql.upper() in ('ROLLBACK','COMMIT'):tx='I'
                        if cursor.description:
                            send('T',json.dumps([c[0] for c in cursor.description]).encode())
                            for row in cursor:send('D',json.dumps(list(row)).encode())
                        send('C',sql.split()[0].upper().encode())
                    else:raise ValueError('unsupported frontend message')
                except Exception as error:
                    if tx=='T':tx='E'
                    skipping=True;audit.append({'error':str(error)});send('E',str(error).encode())
        except EOFError:pass
        finally:db.close();server.close()
    thread=threading.Thread(target=run);thread.start();return client,thread,audit
