from planner import project_plan


def execute(plan, evaluator):
    if plan['op'] == 'scan':
        return [dict(row) for row in plan['rows']]
    if plan['op'] == 'project':
        compiled = project_plan(plan['columns'])
        return [evaluator.project(compiled,row) for row in execute(plan['input'],evaluator)]
    if plan['op'] == 'union_all':
        return execute(plan['left'],evaluator) + execute(plan['right'],evaluator)
    raise ValueError('relation not yet supported: ' + plan['op'])
