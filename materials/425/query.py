import argparse,json
from reader import ChunkSource
from asof import execute
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('left');p.add_argument('right');a=p.parse_args()
    left=ChunkSource(a.left);right=ChunkSource(a.right)
    print(json.dumps({'rows':execute(left,right),'source_reads':left.rows_read+right.rows_read}))
