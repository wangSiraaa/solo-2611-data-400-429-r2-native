import json
from pathlib import Path
import tempfile
from page_writer import encode,rename_first
from page_reader import decode_all,seek
from page_file import save,load

rows=[(b'aa0',b'v0'),(b'aa1',b'v1'),(b'aa2',b'v2')]
page=encode(rows)
normal=decode_all(page)==rows and seek(page,b'aa1')==b'v1'
rename_first(page,b'aA0')
with tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'page.json'
    save(page,path)
    actual=[(k.decode(),v.decode()) for k,v in decode_all(load(path))]
expected=[('aA0','v0'),('aa1','v1'),('aa2','v2')]
print(json.dumps(dict(normal_path_ok=normal,
    normal_observation='原压缩页顺序解码与restart seek aa1均正确。',
    counterexample_detected=actual!=expected,actual=actual,expected=expected,
    failure_observation='只改首条完整键后，保存重新打开将后继aa1/aa2误解为aA1/aA2。',
    oracle_basis='对独立未压缩键值列表只替换aa0并字节排序，其余键必须保持。'),ensure_ascii=False))
