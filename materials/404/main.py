import json,sys
from session import Session
if __name__=='__main__':
    session=Session(sys.argv[1],sys.argv[3] if len(sys.argv)>3 else None);count=int(sys.argv[2]) if len(sys.argv)>2 else 1
    for _ in range(count):print(json.dumps(session.pump(),ensure_ascii=False))
