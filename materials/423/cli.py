"""Command line front end for the persistent compound multikey catalog.

Examples
--------
python3 cli.py build ./data indexes.json docs.json
python3 cli.py put ./data doc.json
python3 cli.py remove ./data d001
python3 cli.py query ./data query.json
python3 cli.py explain ./data query.json
python3 cli.py verify ./data
python3 cli.py log ./data

Input JSON for build may be either a list of path lists or a name->paths
object.  All output is JSON on stdout.
"""

import json
import sys
from pathlib import Path

from catalog import Catalog


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    command = argv.pop(0)

    if command == 'build':
        directory, indexes_path = argv[0], argv[1]
        documents = _load(argv[2]) if len(argv) > 2 else []
        catalog = Catalog.build(directory, _load(indexes_path), documents,
                                force='--force' in argv)
        result = catalog.verify()
        catalog.close()
        print(json.dumps(result, ensure_ascii=False))
        return 0

    with Catalog(argv[0]) as catalog:
        if command == 'put':
            catalog.put(_load(argv[1]))
            print(json.dumps(catalog.verify(), ensure_ascii=False))
        elif command == 'remove':
            removed = catalog.remove(argv[1])
            print(json.dumps({'removed': removed, 'verify': catalog.verify()},
                             ensure_ascii=False))
        elif command == 'query':
            print(json.dumps(catalog.query(_load(argv[1])), ensure_ascii=False))
        elif command == 'explain':
            print(json.dumps(catalog.explain(_load(argv[1])), ensure_ascii=False,
                             indent=2))
        elif command == 'verify':
            print(json.dumps(catalog.verify(), ensure_ascii=False))
        elif command == 'log':
            print(json.dumps(catalog.query_log(), ensure_ascii=False, indent=2))
        elif command == 'dump':
            print(json.dumps(catalog.dump_index(argv[1]), ensure_ascii=False,
                             indent=2))
        else:
            print('unknown command: %s' % command, file=sys.stderr)
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
