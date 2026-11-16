import json,sys
from pipeline import run
if __name__=='__main__': print(json.dumps(run(sys.argv[1]),ensure_ascii=False))
