import json
from pathlib import Path
from backend import start_backend
from wire import frame
from decoder import read_cycle

def run(schedule):
    groups=json.loads(Path(schedule).read_text())['cycles'];sock,thread,audit=start_backend();commands=[];outcomes=[]
    try:
        for cycle in groups:
            for command in cycle:
                name=command['id'];commands.append(command)
                sock.sendall(frame('P',(name+'\0'+command['sql']).encode()))
                sock.sendall(frame('B',('portal-'+name+'\0'+name).encode()))
                sock.sendall(frame('E',('portal-'+name).encode()))
            sock.sendall(frame('S'));outcomes.extend(read_cycle(sock))
        return {'results':[{'id':command['id'],**outcome} for command,outcome in zip(commands,outcomes)],'unanswered':[c['id'] for c in commands[len(outcomes):]],'backend_audit':audit}
    finally:sock.sendall(frame('X'));sock.close();thread.join(3)
