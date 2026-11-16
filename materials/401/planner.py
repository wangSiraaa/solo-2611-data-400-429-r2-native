"""Runtime physical-plan chooser for a streaming LEFT JOIN.

The planner is deliberately *not* told anything about fixture names or expected
rows.  Its only inputs are:

* the fixture's initial ``estimated_left_rows`` (an inaccurate seed), and
* observations produced by the executor from real source consumption: completed
  left rows, right rows consumed by nested-loop scans, and match counts.

The unit of nested-loop work is a source right-row visit, not an emitted output
row.  That distinction is the whole point of the sparse case: few/none of the
probes produce output, yet each left row still pays for a full right scan.
Index probes are charged separately as cheap in-memory logical work.
"""


class Planner:
    def __init__(self, estimated_left_rows):
        self.estimate = max(int(estimated_left_rows), 0)
        self.switched = False
        self.decisions = []

    @staticmethod
    def _num(value):
        # Average right visits per completed left row; guard the empty case.
        return float(value)

    def observe(self, observation):
        """Return the strategy to use for the next left row.

        ``observation`` is supplied by the executor at a left-row boundary.  If
        the planner returns ``'hash'`` exactly once, the executor builds the
        right-side index and uses it for all remaining left rows.
        """
        if self.switched:
            return 'hash'

        completed = int(observation['left_rows_completed'])
        more_left = bool(observation['more_left'])
        right_reads = int(observation['right_reads'])
        last_scan = int(observation['right_rows_last_scan'])
        matched_lefts = int(observation.get('matched_left_rows', 0))
        unmatched_lefts = int(observation.get('unmatched_left_rows', 0))
        pairs = int(observation.get('pairs_emitted', 0))

        # Nested-loop cost actually observed per completed left row.  Use the
        # measured source visits, not pairs produced.
        if completed:
            nested_unit = self._num(right_reads) / completed
        else:
            nested_unit = self._num(last_scan)

        # A hash build needs one full pass over the immutable right source.
        # ``last_scan`` is the size of the full scan just completed, so it is a
        # measured (not guessed) build cost.
        build_cost = float(last_scan)

        # Project the amount of left work still to do.  The initial estimate is
        # only a seed: ``completed`` from the real left stream moves it, and if
        # the seed turns out to be exhausted while rows remain we use completed
        # work as a floor instead of pinning ourselves to nested loop forever.
        remaining = max(self.estimate - completed, 0)
        if more_left:
            remaining = max(remaining, 1)
            if completed >= self.estimate:
                remaining = max(remaining, completed)

        # In-memory index probes are logical work, but not source row visits.
        probe_unit = 1.0
        nested_projected = remaining * nested_unit
        hash_projected = build_cost + remaining * probe_unit

        chosen = 'nested_loop'
        reason = 'no_remaining_work' if not more_left else 'nested_loop_cheaper'
        if more_left and remaining > 0 and nested_projected > hash_projected:
            chosen = 'hash'
            reason = 'projected_nested_scan_visits_exceed_build_plus_probes'
            self.switched = True

        decision = {
            'trigger': 'left_row_boundary',
            'chosen': chosen,
            'reason': reason,
            'handoff': 'row_boundary' if chosen == 'hash' else None,
            'observation': {
                'estimated_left_rows': self.estimate,
                'left_rows_completed': completed,
                'more_left': more_left,
                'right_rows_last_scan': last_scan,
                'right_reads_total': right_reads,
                'pairs_emitted': pairs,
                'matched_left_rows': matched_lefts,
                'unmatched_left_rows': unmatched_lefts,
            },
            'cost_model': {
                'remaining_left_projection': remaining,
                'nested_right_rows_per_left': nested_unit,
                'nested_projected_right_visits': nested_projected,
                'hash_build_right_rows': build_cost,
                'hash_probe_unit': probe_unit,
                'hash_projected_logical_work': hash_projected,
            },
        }
        self.decisions.append(decision)
        return chosen
