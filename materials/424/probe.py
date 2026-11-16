"""Smoke probe: the unsafe plan must now fail compilation even when only the
sales row is supplied; the safe plan compiles and yields 107 / 30."""

import json
from pathlib import Path

from contracts import CompileError
from pipeline import compile_plan, run_plan
from expressions import evaluate

root = Path(__file__).resolve().parent
unsafe = json.loads((root / 'unsafe.json').read_text())
safe = json.loads((root / 'safe.json').read_text())
rows = json.loads((root / 'rows.json').read_text())

report = {}

# 1. The unsafe plan is rejected at compile time, independently of the data.
try:
    compile_plan(unsafe)
    report['unsafe_compiler_accepted'] = True
except CompileError as error:
    report['unsafe_compiler_accepted'] = False
    report['unsafe_compile_error'] = error.detail

# 2. Even with only the harmless sales row, running must refuse to start.
sales_only = {'source': [rows['source'][0]]}
try:
    run_plan(unsafe, sales_only)
    report['unsafe_ran_on_sales_only'] = True
except CompileError as error:
    report['unsafe_ran_on_sales_only'] = False
    report['sales_only_refusal_code'] = error.code

# 3. The compiler's counterexample row is itself a legal adjustment row and
#    reproduces the original KeyError when fed to the raw expression.
counter = report.get('unsafe_compile_error', {}).get('origin', {}).get('counterexample_row')
report['counterexample_is_declared_adjustment'] = (
    counter == {'kind': 'adjustment', 'net': 1})
try:
    evaluate(unsafe['nodes'][1]['columns']['amount'], counter)
except (KeyError, TypeError) as error:
    report['counterexample_reproduces'] = repr(error)

# 4. The safe plan compiles for every declared variant and runs correctly.
compiled_safe = compile_plan(safe)
report['safe_compiler_accepted'] = True
report['safe_output_shapes'] = compiled_safe['output_contract']['shapes']
report['safe_runtime_output'] = run_plan(safe, rows)
report['oracle'] = {
    'unsafe_safe_for_all_declared_variants': False,
    'safe_safe_for_all_declared_variants': True,
    'safe_values': [107, 30],
}
print(json.dumps(report, ensure_ascii=False, indent=2))
