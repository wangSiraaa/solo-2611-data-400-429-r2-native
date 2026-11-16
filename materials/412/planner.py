import json


def expression_key(expr):
    return json.dumps(expr, sort_keys=True, separators=(',', ':'))


def project_plan(columns):
    return [(name, expression, expression_key(expression)) for name, expression in columns]
