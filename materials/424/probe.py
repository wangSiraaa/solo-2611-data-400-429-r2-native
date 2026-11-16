import copy,json
from pathlib import Path
from pipeline import compile_plan,run_plan
root=Path(__file__).resolve().parent
unsafe=json.loads((root/'unsafe.json').read_text());safe=json.loads((root/'safe.json').read_text());rows=json.loads((root/'rows.json').read_text())
report={'normal_rows':run_plan(unsafe,{'source':[rows['source'][0]]}),'unsafe_compiler_accepted':True}
compile_plan(unsafe)
try: run_plan(unsafe,rows)
except (KeyError,TypeError) as error: report['legal_variant_runtime_error']=str(error)
report['safe_runtime_output']=run_plan(safe,rows)
report['safe_compiler_accepted']=True
try: compile_plan(safe)
except ValueError as error: report['safe_compiler_accepted']=False;report['safe_compiler_error']=str(error)
report['oracle']={'unsafe_safe_for_all_declared_variants':False,'safe_safe_for_all_declared_variants':True,'safe_values':[107,30]}
print(json.dumps(report))
