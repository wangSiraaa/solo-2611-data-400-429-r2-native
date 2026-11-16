"""Command-line interface over the updatable BM25 index.

Examples
--------
python3 cli.py build --store ./idx --block-size 5 --docs docs.json
python3 cli.py put   --store ./idx --doc D1 --tokens '["alpha","beta"]'
python3 cli.py remove --store ./idx --doc D1
python3 cli.py query  --store ./idx --terms alpha beta --k 3 --k1 2 --b 0
python3 cli.py inspect --store ./idx [--terms alpha]

docs.json for build: {"doc_id": ["token", ...], ...}
The index is persisted after every mutating command and reopened read-only in
spirit for query/inspect; statistics currency is visible in inspect without
running any query first.
"""
import argparse
import json
import sys

from inverted import Index
from inspect import inspect as inspect_index, skip_evidence
from ranker import query


def _load_json_arg(value):
    if value.startswith('@'):
        with open(value[1:], encoding='utf-8') as handle:
            return json.load(handle)
    return json.loads(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Updatable BM25 block index')
    sub = parser.add_subparsers(dest='command', required=True)

    p_build = sub.add_parser('build')
    p_build.add_argument('--store', required=True)
    p_build.add_argument('--docs', required=True,
                        help='JSON file: {doc_id: [tokens...]}')
    p_build.add_argument('--block-size', type=int, default=1)

    p_put = sub.add_parser('put')
    p_put.add_argument('--store', required=True)
    p_put.add_argument('--doc', required=True)
    p_put.add_argument('--tokens', required=True,
                       help='JSON array, or @file containing one')

    p_remove = sub.add_parser('remove')
    p_remove.add_argument('--store', required=True)
    p_remove.add_argument('--doc', required=True)

    p_query = sub.add_parser('query')
    p_query.add_argument('--store', required=True)
    p_query.add_argument('--terms', nargs='+', required=True)
    p_query.add_argument('--k', type=int, required=True)
    p_query.add_argument('--k1', type=float, default=1.2)
    p_query.add_argument('--b', type=float, default=0.75)
    p_query.add_argument('--evidence', action='store_true',
                        help='include per-skip proofs')

    p_inspect = sub.add_parser('inspect')
    p_inspect.add_argument('--store', required=True)
    p_inspect.add_argument('--terms', nargs='*')
    p_inspect.add_argument('--k1', type=float, default=1.2)
    p_inspect.add_argument('--b', type=float, default=0.75)

    args = parser.parse_args(argv)

    if args.command == 'build':
        with open(args.docs, encoding='utf-8') as handle:
            docs = json.load(handle)
        index = Index(docs, block_size=args.block_size,
                      store_path=args.store)
        index.save()
        print(json.dumps({'built': True, 'store': args.store,
                          **_brief(index)}, ensure_ascii=False))
        return

    index = Index.open(args.store)

    if args.command == 'put':
        tokens = _load_json_arg(args.tokens)
        block = index.put(args.doc, tokens)
        index.save()
        print(json.dumps({'put': args.doc, 'block': block,
                          **_brief(index)}, ensure_ascii=False))
    elif args.command == 'remove':
        block = index.remove(args.doc)
        index.save()
        print(json.dumps({'removed': args.doc, 'block': block,
                          **_brief(index)}, ensure_ascii=False))
    elif args.command == 'query':
        rows, audit = query(index, args.terms, args.k,
                            k1=args.k1, b=args.b)
        index.save()   # persist the appended query log
        payload = {
            'rows': rows,
            'statistics_used': audit['statistics_used'],
            'parameters': audit['parameters'],
            'revision': audit['revision'],
            'scored': audit['scored'],
            'raw_document_accesses': audit['raw_document_accesses'],
            'metadata_accesses': audit['metadata_accesses'],
            'skipped': audit['skipped'],
            'skipped_nonempty_blocks': audit['skipped_nonempty_blocks'],
        }
        if args.evidence:
            payload['skip_evidence'] = skip_evidence(audit)
        print(json.dumps(payload, ensure_ascii=False))
    elif args.command == 'inspect':
        report = inspect_index(index, terms=args.terms,
                               k1=args.k1, b=args.b)
        print(json.dumps(report, ensure_ascii=False))


def _brief(index):
    stats = index.current_stats()
    return {'revision': stats['revision'], 'n': stats['n'],
            'avgdl': stats['avgdl'], 'total_length': stats['total_length'],
            'blocks': len(index.blocks)}


if __name__ == '__main__':
    main()
