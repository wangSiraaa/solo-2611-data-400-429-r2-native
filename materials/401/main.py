import json,sys
from executor import Executor
if __name__=='__main__':
    executor=Executor(sys.argv[1]);first=executor.next();startup=executor.inspect();rows=[] if first is None else [first]
    while True:
        row=executor.next()
        if row is None:break
        rows.append(row)
    print(json.dumps({'rows':rows,'startup':startup,'final':executor.inspect()},ensure_ascii=False))
