"""Prototype uses emitted cardinality as a proxy for nested-loop work."""
class Planner:
    def __init__(self,estimated_left_rows):self.estimate=estimated_left_rows;self.pairs=0;self.switched=False;self.decisions=[]
    def on_pair(self):self.pairs+=1
    def choose(self):
        if not self.switched and self.estimate>=4 and self.pairs>=2:
            self.switched=True;self.decisions.append({'strategy':'hash','observed_output_pairs':self.pairs});return 'hash'
        return 'nested_loop'
