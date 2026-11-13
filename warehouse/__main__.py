import argparse
import json
from pathlib import Path

from . import dwd, ods, reconcile as reconcile_mod
from .catalog import connect, load_file
from .domain import overview
from .store import ObjectStore


def main():
    parser = argparse.ArgumentParser(description='本地数据集合装载与查询')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('import', 'inspect', 'export', 'ingest', 'normalize', 'reconcile',
                 'quarantine', 'reprocess', 'inspect-nested', 'rebuild'):
        commands.add_parser(name)
    for name in ('import', 'ingest'):
        commands.choices[name].add_argument('--input', required=True)
        commands.choices[name].add_argument('--batch', required=True)
    commands.choices['ingest'].add_argument('--kind', choices=['csv', 'jsonl'])
    commands.choices['export'].add_argument('--objects', required=True)
    commands.choices['reprocess'].add_argument('--qid', type=int, required=True)
    commands.choices['reprocess'].add_argument('--input', required=True)
    commands.choices['rebuild'].add_argument('--order', required=True)
    for name in commands.choices:
        commands.choices[name].add_argument('--db', required=True)
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    exit_code = 0
    try:
        if args.command == 'import':
            result = {'changed': load_file(connection, args.input, args.batch)}
        elif args.command == 'inspect':
            result = overview(connection)
        elif args.command == 'export':
            records = overview(connection)
            result = {'object': ObjectStore(args.objects).put(records), 'rows': len(records)}
        elif args.command == 'ingest':
            result = ods.ingest_file(connection, args.input, args.batch, args.kind)
        elif args.command == 'normalize':
            result = dwd.normalize(connection)
        elif args.command == 'reconcile':
            result = reconcile_mod.reconcile(connection)
            if result['diffs'] or result['quarantine_mismatches']:
                exit_code = 1
        elif args.command == 'quarantine':
            result = [dict(row) for row in connection.execute('SELECT * FROM quarantine ORDER BY qid')]
        elif args.command == 'reprocess':
            result = dwd.reprocess(connection, args.qid, args.input)
        elif args.command == 'inspect-nested':
            result = dwd.overview_nested(connection)
        else:
            result = reconcile_mod.rebuild(connection, args.order)
            if result is None:
                raise ValueError(f'订单不存在: {args.order}')
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    finally:
        connection.close()
    raise SystemExit(exit_code)


if __name__ == '__main__':
    main()
