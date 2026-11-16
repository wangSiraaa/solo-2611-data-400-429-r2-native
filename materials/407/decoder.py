"""Original decoder flattens command outcomes and discards cycle readiness."""
import json
from wire import receive

def read_cycle(sock):
    rows=[];outcomes=[]
    while True:
        kind,payload=receive(sock)
        if kind=='D':rows.append(json.loads(payload))
        elif kind=='C':outcomes.append({'status':'success','rows':rows,'tag':payload.decode()});rows=[]
        elif kind=='E':outcomes.append({'status':'error','message':payload.decode()});rows=[]
        elif kind=='Z':return outcomes
