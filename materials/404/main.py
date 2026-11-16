"""Pump a fixture and print the published preview after each consumed frame.

    python3 main.py fixtures/single.json 1
    python3 main.py fixtures/null-bubble.json 3 [preview.sqlite]
"""

import json
import sys

from session import Session


def main(argv):
    if not argv:
        print('usage: main.py <fixture.json> [pumps] [db_path]')
        return 2
    path = argv[0]
    count = int(argv[1]) if len(argv) > 1 else 1
    db_path = argv[2] if len(argv) > 2 else None
    session = Session(path, db_path)
    for _ in range(count):
        print(json.dumps(session.pump(), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
