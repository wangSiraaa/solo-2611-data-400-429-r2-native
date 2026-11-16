"""Streaming feasibility for one generated strictly increasing group, not general ASOF."""
import json
import tempfile
from pathlib import Path


def run():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifests = {}
        for side in ('left', 'right'):
            paths = []
            for start in range(0, 600, 7):
                rows = [{'row_id': side[0] + '%04d' % n, 'group': ['x'],
                         'ts': 4 * n + (2 if side == 'left' else 0), 'value': 'payload-' + str(n)}
                        for n in range(start, min(start + 7, 600))]
                path = root / ('%s-%04d.json' % (side, start))
                path.write_text(json.dumps(rows))
                paths.append(path)
                if start % 35 == 0:
                    empty = root / ('%s-empty-%04d.json' % (side, start))
                    empty.write_text('[]')
                    paths.append(empty)
            manifests[side] = paths
        receipts = []
        for direction in ('backward', 'forward', 'nearest'):
            reads = {'left': 0, 'right': 0}
            loaded = {'left': 0, 'right': 0}
            peak = [0]

            def source(side):
                previous = None
                for path in manifests[side]:
                    encoded = path.read_text()
                    rows = json.loads(encoded)
                    # At decoding peak both encoded and decoded chunk payloads count.
                    # Four extra row slots conservatively cover left, predecessor,
                    # successor, and the currently yielded output.
                    peak[0] = max(peak[0], sum(loaded.values()) + 2 * len(rows) + 4)
                    del encoded
                    loaded[side] = len(rows)
                    for row in rows:
                        key = (row['group'], row['ts'], row['row_id'])
                        assert previous is None or previous < key
                        previous = key
                        reads[side] += 1
                        yield row
                    loaded[side] = 0
                    del rows

            right = iter(source('right'))
            following = next(right, None)
            preceding = None
            matched = 0
            emitted = 0
            for number, left in enumerate(source('left')):
                while following is not None and following['ts'] < left['ts']:
                    preceding = following
                    following = next(right, None)
                candidates = ([preceding] if direction == 'backward' else
                              [following] if direction == 'forward' else [preceding, following])
                candidates = [row for row in candidates if row is not None]
                winner = min(candidates, key=lambda row: (abs(row['ts']-left['ts']), row['ts'], row['row_id'])) if candidates else None
                expected = number if direction != 'forward' else number + 1
                expected_id = 'r%04d' % expected if expected < 600 else None
                assert (winner['row_id'] if winner else None) == expected_id
                emitted += 1
                matched += winner is not None
            for _ in right:
                pass
            assert reads == {'left': 600, 'right': 600}
            assert peak[0] <= 32
            receipts.append({'direction': direction, 'source_reads': reads,
                             'actual_total_source_reads': sum(reads.values()),
                             'required_read_limit': 4 * 1200, 'left_outputs': emitted,
                             'matched_outputs': matched, 'conservative_peak_payload_rows': peak[0]})
        return {'scope': 'Fixed one-key, strictly increasing, no-exact-time benchmark only; '
                         'global ordering is checked across all generated chunks. '
                         'The general group/NULL/tie/tolerance/parser implementation remains task work.',
                'rows_per_side': 600, 'maximum_chunk_rows': 7, 'memory_rows': 32,
                'temporary_index_bytes': 0, 'directions': receipts}


if __name__ == '__main__':
    print(json.dumps(run()))
