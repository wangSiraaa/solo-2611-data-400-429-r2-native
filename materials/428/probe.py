import itertools
import json
import tempfile
from pathlib import Path
from allocator import Allocator
from dispatcher import Dispatcher


def legal_placements(nodes, demands):
    result = []
    for assignment in itertools.product(*(member['allowed_nodes'] for member in demands)):
        if all(sum(member['resources'].get(resource, 0)
                   for member, placed in zip(demands, assignment) if placed == node) <= capacity
               for node, capacities in nodes.items() for resource, capacity in capacities.items()):
            result.append(list(assignment))
    return result


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    nodes = {'scarce': {'cpu': 1, 'io': 1}, 'other': {'cpu': 1, 'io': 1}}
    replicas = {}
    for node in nodes:
        folder = root / node
        folder.mkdir()
        path = folder / 'partition.json'
        path.write_text(json.dumps([{'row_id': 'r1', 'value': 7}]))
        replicas[node] = path
    allocator = Allocator(nodes)
    dispatcher = Dispatcher(allocator, root / 'normal-output')
    try:
        normal_ready = dispatcher.reserve_one('normal', 0, replicas, ['other'], {'cpu': 1, 'io': 1})
        normal_output = dispatcher.execute_group('normal')
        normal_data = json.loads((root / 'normal-output/normal-0.json').read_text())
    finally:
        dispatcher.cleanup()
    allocator = Allocator(nodes)
    dispatcher = Dispatcher(allocator, root / 'first-fit-output')
    members = [{'allowed_nodes': ['scarce', 'other'], 'resources': {'cpu': 1, 'io': 1}},
               {'allowed_nodes': ['scarce'], 'resources': {'cpu': 1, 'io': 1}}]
    try:
        actual = [dispatcher.reserve_one('join', index, replicas, **member)
                  for index, member in enumerate(members)]
        counterexample = {'member_ready': actual, 'held_placements': dict(allocator.held),
                          'remaining_by_node': allocator.available(),
                          'independent_complete_placements': legal_placements(nodes, members),
                          'waiting_real_processes': [owner for owner, child in dispatcher.children.items()
                                                     if child.poll() is None]}
    finally:
        dispatcher.cleanup()
    impossible = [{'allowed_nodes': ['scarce'], 'resources': {'cpu': 1, 'io': 1}}] * 2
    print(json.dumps({'normal': {'ready': normal_ready, 'worker_results': normal_output,
                                 'actual_file_rows': normal_data},
                      'first_fit_counterexample': counterexample,
                      'global_sum_is_sufficient_but_locality_is_not': {
                          'total_capacity': {'cpu': 2, 'io': 2},
                          'total_demand': {'cpu': 2, 'io': 2},
                          'independent_complete_placements': legal_placements(nodes, impossible)},
                      'all_children_reaped': all(child.poll() is not None for child in dispatcher.children.values()),
                      'held_after_cleanup': allocator.held,
                      'scope': 'Actual first-fit/READY baseline and independent finite placement oracle; '
                               'no target gang allocator or DAG dispatcher implementation.'}))
