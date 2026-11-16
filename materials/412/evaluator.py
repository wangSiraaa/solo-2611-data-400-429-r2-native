class Evaluator:
    def __init__(self, run_value):
        self.run_value = run_value
        self.ticket = 0
        self.calls = []

    def call(self, name, args):
        self.calls.append(name)
        if name == 'abs':
            return abs(args[0])
        if name == 'run_value':
            return self.run_value
        if name == 'next_ticket':
            self.ticket += 1
            return self.ticket
        raise ValueError('unknown function')

    def eval(self, expr, row):
        if 'column' in expr:
            return row[expr['column']]
        if 'literal' in expr:
            return expr['literal']
        return self.call(expr['call'], [self.eval(a,row) for a in expr.get('args',[])])

    def project(self, plan, row):
        memo, output = {}, {}
        for name, expression, key in plan:
            if key not in memo:
                memo[key] = self.eval(expression, row)
            output[name] = memo[key]
        return output
