import argparse
import json
import sys
from pathlib import Path
from .io import inspect_bundle
from .catalog import Catalog
from .ncf import (
    FormatError, NcfReader, FileSource, write_file, normalize_rows,
)


def _load_schema(path, schema_id=None):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(raw, dict):
        return raw
    entries = list(raw)
    if schema_id is None:
        if len(entries) != 1:
            raise ValueError('schema 集合含多个条目，需用 --schema-id 指定')
        return entries[0]['schema']
    for entry in entries:
        if entry['id'] == schema_id:
            return entry['schema']
    raise ValueError(f'找不到 schema id: {schema_id}')


def main():
    parser = argparse.ArgumentParser(description='业务资料读取与本地存档')
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('inspect'); p.add_argument('directory')
    p = subs.add_parser('ingest'); p.add_argument('directory'); p.add_argument('database')
    p = subs.add_parser('catalog'); p.add_argument('database')
    p = subs.add_parser('encode')
    p.add_argument('schema'); p.add_argument('rows'); p.add_argument('output')
    p.add_argument('--schema-id'); p.add_argument('--page-row-limit', type=int, default=1000)
    p = subs.add_parser('decode'); p.add_argument('file')
    p.add_argument('--columns', nargs='*'); p.add_argument('--row-start', type=int, default=0)
    p.add_argument('--row-end', type=int)
    p = subs.add_parser('validate-ncf'); p.add_argument('file')
    p = subs.add_parser('project'); p.add_argument('file'); p.add_argument('columns', nargs='+')
    p.add_argument('--row-start', type=int, default=0); p.add_argument('--row-end', type=int)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            result = inspect_bundle(args.directory)
        elif args.command == 'ingest':
            result = {'import_id': Catalog(args.database).ingest(args.directory)}
        elif args.command == 'catalog':
            result = Catalog(args.database).summary()
        elif args.command == 'encode':
            schema = _load_schema(args.schema, args.schema_id)
            rows = json.loads(Path(args.rows).read_text(encoding='utf-8'))
            write_file(args.output, schema, rows, page_row_limit=args.page_row_limit)
            result = {'written': args.output,
                      'rows': len(normalize_rows(schema, rows))}
        elif args.command in ('decode', 'project'):
            with NcfReader(FileSource(args.file)) as reader:
                result = reader.read(columns=args.columns, row_start=args.row_start,
                                     row_end=args.row_end)
        else:
            with NcfReader(FileSource(args.file)) as reader:
                result = reader.validate()
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (ValueError, OSError, KeyError) as exc:
        detail = f': {exc}' if isinstance(exc, FormatError) else ''
        print('资料读取失败，请核对文件与字段约定' + detail, file=sys.stderr); return 2
if __name__ == '__main__': raise SystemExit(main())
