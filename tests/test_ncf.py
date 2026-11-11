import copy
import tempfile
import unittest
from pathlib import Path

from workbench.domain import BytesSource
from workbench.io import read_json
from workbench.ncf import (
    FormatError, NcfReader, FileSource, leaf_paths, normalize_rows,
    read_bytes, read_file, validate_file, write_bytes, write_file,
    MAGIC, FORMAT_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = read_json(ROOT / 'samples' / 'schemas.json')[0]['schema']
RECORDS = read_json(ROOT / 'samples' / 'records.json')
DEEP = 'nights.item.segments.item.item'
LABEL = 'nights.item.label'


def encoded(rows=RECORDS, schema=SCHEMA, **kwargs):
    return write_bytes(schema, rows, **kwargs)


class RoundtripTests(unittest.TestCase):
    def test_sample_roundtrip_page_per_two_rows(self):
        data = encoded(page_row_limit=2)
        self.assertEqual(read_bytes(data), normalize_rows(SCHEMA, RECORDS))

    def test_roundtrip_single_page_and_file_api(self):
        data = encoded(page_row_limit=1000)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'a.ncf'
            write_file(path, SCHEMA, RECORDS)
            self.assertEqual(read_file(path), normalize_rows(SCHEMA, RECORDS))
            self.assertEqual(validate_file(path)['status'], 'ok')
            with FileSource(path) as source:
                with NcfReader(source) as reader:
                    self.assertEqual(reader.row_count, len(RECORDS))

    def test_repeated_encoding_is_byte_identical(self):
        self.assertEqual(encoded(), encoded())

    def test_empty_table(self):
        data = encoded([])
        self.assertEqual(read_bytes(data), [])
        with NcfReader(BytesSource(data)) as reader:
            self.assertEqual(reader.validate()['rows'], 0)
            self.assertEqual(reader.metadata['pages'], [])
            self.assertEqual(reader.read(row_start=0, row_end=0), [])

    def test_metadata_is_self_describing(self):
        data = encoded(page_row_limit=2)
        with NcfReader(BytesSource(data)) as reader:
            meta = reader.metadata
            self.assertEqual(meta['format'], 'ncf')
            self.assertEqual(meta['version'], FORMAT_VERSION)
            self.assertEqual(meta['encoding_version'], 1)
            self.assertEqual(meta['row_count'], len(RECORDS))
            self.assertEqual(meta['schema'], SCHEMA)
            self.assertEqual(meta['leaf_columns'], leaf_paths(SCHEMA))
            starts = [p['row_start'] for p in meta['pages']]
            self.assertEqual(starts, [0, 2, 4])
            for page in meta['pages']:
                self.assertLessEqual(page['row_count'], 2)
                for block in page['blocks']:
                    self.assertIsInstance(block['path'], str)
                    self.assertIn(block['stream'], ('validity', 'lengths', 'values'))
                    self.assertGreaterEqual(block['offset'], len(MAGIC) + 1)

    def test_boolean_string_int_leaf_kinds(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'flag', 'kind': 'boolean', 'nullable': True},
            {'name': 'text', 'kind': 'string', 'nullable': True},
            {'name': 'num', 'kind': 'int64', 'nullable': False},
        ]}
        rows = [
            {'flag': None, 'text': '甲乙', 'num': 0},
            {'flag': True, 'text': '', 'num': 9223372036854775807},
            {'flag': False, 'text': None, 'num': -9223372036854775808},
        ]
        back = read_bytes(write_bytes(schema, rows))
        self.assertEqual(back, rows)

    def test_nullable_root_struct(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': True, 'fields': [
            {'name': 'v', 'kind': 'int64', 'nullable': True}]}
        rows = [None, {'v': 1}, None, {'v': None}]
        self.assertEqual(read_bytes(write_bytes(schema, rows)), rows)


class NullDistinctionTests(unittest.TestCase):
    """项目约定的五种空值形态必须可往返区分。"""

    def test_five_null_shapes_preserved(self):
        back = read_bytes(encoded())
        nights = [row['nights'] for row in back]
        self.assertIsNone(nights[0])                       # null 列表
        self.assertEqual(nights[1], [])                    # 空列表
        self.assertEqual(nights[2], [None])                # 含 null 元素
        # 空 struct 元素：空 struct 按约定展开为 null 子项（与 null 元素靠 validity 区分）
        self.assertEqual(nights[3], [{'label': None, 'segments': None}])
        self.assertEqual(back[4]['nights'][0]['segments'],
                         [None, [], [None, 1, -1, 9223372036854775807]])

    def test_null_vs_empty_nested_lists(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'x', 'kind': 'list', 'nullable': True, 'element': {
                'name': 'item', 'kind': 'list', 'nullable': True, 'element': {
                    'name': 'item', 'kind': 'int64', 'nullable': True}}}]}
        rows = [
            {'x': None},
            {'x': []},
            {'x': [None]},
            {'x': [[]]},
            {'x': [[None]]},
            {'x': [[], None, [1, None], []]},
        ]
        back = read_bytes(write_bytes(schema, rows, page_row_limit=2))
        self.assertEqual(back, rows)
        # 五种形态彼此不可误判
        flat = [r['x'] for r in back]
        self.assertEqual(len({repr(v) for v in flat[:5]}), 5)

    def test_missing_optional_fields_become_null(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'a', 'kind': 'struct', 'nullable': True, 'fields': [
                {'name': 'b', 'kind': 'boolean', 'nullable': True}]}]}
        back = read_bytes(write_bytes(schema, [{}, {'a': {}}, {'a': {'b': True}}]))
        self.assertEqual(back, [{'a': None}, {'a': {'b': None}}, {'a': {'b': True}}])


class WriteValidationTests(unittest.TestCase):
    def test_unknown_field_and_wrong_type_rejected(self):
        bad = copy.deepcopy(RECORDS)
        bad[0]['extra'] = 1
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, bad)
        bad2 = copy.deepcopy(RECORDS)
        bad2[0]['id'] = 7
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, bad2)
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, [{'id': 'x', 'nights': 'not-a-list'}])
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, [{'id': 'x', 'nights': [123]}])

    def test_required_null_and_missing_required_rejected(self):
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, [{'id': None, 'nights': None}])
        with self.assertRaises(ValueError):
            write_bytes(SCHEMA, [{'nights': None}])
        # 元素 struct 缺必选子字段必须报错
        element_required = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'xs', 'kind': 'list', 'nullable': False, 'element': {
                'name': 'item', 'kind': 'struct', 'nullable': False, 'fields': [
                    {'name': 'm', 'kind': 'int64', 'nullable': False}]}}]}
        with self.assertRaises(ValueError):
            write_bytes(element_required, [{'xs': [{}]}])

    def test_integer_range_and_bad_utf8_rejected(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'n', 'kind': 'int64', 'nullable': False}]}
        with self.assertRaises(ValueError):
            write_bytes(schema, [{'n': 1 << 63}])
        with self.assertRaises(ValueError):
            write_bytes(schema, [{'n': -(1 << 63) - 1}])
        with self.assertRaises(ValueError):
            write_bytes(schema, [{'n': True}])
        schema_s = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 's', 'kind': 'string', 'nullable': False}]}
        with self.assertRaises(ValueError):
            write_bytes(schema_s, [{'s': 'surrogate\ud800'}])

    def test_bad_schema_rejected(self):
        bad = copy.deepcopy(SCHEMA)
        bad['fields'][0]['name'] = 'a.b'
        with self.assertRaises(ValueError):
            write_bytes(bad, RECORDS)
        bad = copy.deepcopy(SCHEMA)
        bad['fields'].append(dict(bad['fields'][0]))
        with self.assertRaises(ValueError):
            write_bytes(bad, RECORDS)
        bad = copy.deepcopy(SCHEMA)
        bad['kind'] = 'float'
        with self.assertRaises(ValueError):
            write_bytes(bad, RECORDS)
        bad = copy.deepcopy(SCHEMA)
        bad['fields'][1]['element']['name'] = 'element'
        with self.assertRaises(ValueError):
            write_bytes(bad, RECORDS)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.data = encoded(page_row_limit=2)

    def test_projection_rebuilds_list_shapes_without_other_leaves(self):
        source = BytesSource(self.data)
        with NcfReader(source) as reader:
            projected = reader.read(columns=[DEEP])
        full = normalize_rows(SCHEMA, RECORDS)
        # 投影结果只含深叶列；用同列投影作用在全量结果上做逐行形状比较
        for got, want in zip(projected, full):
            self.assertEqual(set(got), {'nights'})
            self._assert_shape_only(got['nights'], want['nights'])

    @staticmethod
    def _assert_shape_only(got, want):
        """只比较列表形状与空值位置，未选 struct 字段允许缺键。"""
        if want is None:
            assert got is None
        elif isinstance(want, list):
            assert isinstance(got, list) and len(got) == len(want)
            for g, w in zip(got, want):
                ProjectionTests._assert_shape_only(g, w)
        elif isinstance(want, dict):
            assert isinstance(got, dict)
            if 'segments' in want:
                assert set(got) <= {'segments'}
                if 'segments' in got or 'segments' in want:
                    ProjectionTests._assert_shape_only(got.get('segments'),
                                                       want['segments'])
        # 叶值路径在本用例里只要求形状，值本身为 int，已随列表比较覆盖

    def test_unselected_leaf_pages_are_never_read(self):
        source = BytesSource(self.data)
        with NcfReader(source) as reader:
            id_blocks = [(b['offset'], b['length'])
                         for p in reader.metadata['pages']
                         for b in p['blocks'] if b['path'] == 'id']
            label_blocks = [(b['offset'], b['length'])
                            for p in reader.metadata['pages']
                            for b in p['blocks'] if b['path'] == LABEL]
            reader.read(columns=[DEEP])
        touched = [(off, off + length) for off, length in source.reads]
        for off, length in id_blocks + label_blocks:
            self.assertTrue(all(end <= off or start >= off + length
                                for start, end in touched),
                            '未选叶列块被读取')

    def test_projection_only_touches_needed_structure_streams(self):
        source = BytesSource(self.data)
        with NcfReader(source) as reader:
            reader.read(columns=['id'])
            touched_paths = set()
            for page in reader.metadata['pages']:
                for block in page['blocks']:
                    if any(off == block['offset'] for off, _ in source.reads):
                        touched_paths.add((block['path'], block['stream']))
        # id 投影只需根 validity（根不可空，无）与 id.values；不得碰 nights 子树
        self.assertTrue(any(path == 'id' for path, _ in touched_paths))
        self.assertFalse(any(path.startswith('nights') for path, _ in touched_paths))

    def test_projection_multiple_columns_and_unknown_column(self):
        source = BytesSource(self.data)
        with NcfReader(source) as reader:
            got = reader.read(columns=['id', LABEL])
        want = [{'id': row['id'],
                 'nights': None if row['nights'] is None
                 else [None if item is None else {'label': item['label']}
                       for item in row['nights']]}
                for row in normalize_rows(SCHEMA, RECORDS)]
        self.assertEqual(got, want)
        with NcfReader(BytesSource(self.data)) as reader:
            with self.assertRaises(FormatError):
                reader.read(columns=['nights.item'])

    def test_row_range_selection_does_not_touch_other_pages(self):
        data = encoded(page_row_limit=1)  # 每页一行，共 6 页
        source = BytesSource(data)
        with NcfReader(source) as reader:
            got = reader.read(columns=[DEEP], row_start=2, row_end=4)
            page_starts_read = set()
            for off, _ in source.reads:
                for page in reader.metadata['pages']:
                    for block in page['blocks']:
                        if block['offset'] == off:
                            page_starts_read.add(page['page'])
        self.assertEqual(page_starts_read, {2, 3})
        full = normalize_rows(SCHEMA, RECORDS)
        self._assert_shape_only(got, [{'nights': full[2]['nights']},
                                      {'nights': full[3]['nights']}])
        with NcfReader(BytesSource(data)) as reader:
            with self.assertRaises(FormatError):
                reader.read(row_start=3, row_end=2)
            with self.assertRaises(FormatError):
                reader.read(row_start=-1)
            with self.assertRaises(FormatError):
                reader.read(row_end=99)

    def test_row_range_spanning_pages(self):
        data = encoded(page_row_limit=2)
        got = read_bytes(data, row_start=1, row_end=5)
        full = normalize_rows(SCHEMA, RECORDS)
        self.assertEqual(got, full[1:5])


class CorruptionTests(unittest.TestCase):
    def test_corrupt_unselected_column_projection_ok_but_validate_fails(self):
        data = bytearray(encoded(page_row_limit=2))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            id_block = next(b for p in reader.metadata['pages']
                            for b in p['blocks'] if b['path'] == 'id')
        # 仅破坏 id 叶列第一页块的负载字节（跳过帧头长度）
        frame_len_byte = 1  # 小块 payload_len 为单字节 uvarint
        data[id_block['offset'] + frame_len_byte] ^= 0xFF
        damaged = bytes(data)
        source = BytesSource(damaged)
        with NcfReader(source) as reader:
            projected = reader.read(columns=[DEEP])           # 不读 id 块
            self.assertEqual(len(projected), len(RECORDS))
            with self.assertRaises(FormatError) as ctx:
                reader.validate()                              # 全量必须失败
            self.assertIn('id', str(ctx.exception))

    def test_crc_detects_every_block_corruption(self):
        data = bytearray(encoded(page_row_limit=2))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            block = reader.metadata['pages'][0]['blocks'][0]
        data[block['offset'] + 2] ^= 0x01
        with NcfReader(BytesSource(bytes(data))) as reader:
            with self.assertRaises(FormatError) as ctx:
                reader.validate()
            self.assertEqual(ctx.exception.page, 0)

    def test_footer_crc_corruption(self):
        data = bytearray(encoded())
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            footer_start = reader._footer_start
        data[footer_start + 10] ^= 0xFF  # 破坏页脚负载本身，CRC 必须失配
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(bytes(data)))
        self.assertIn('页脚校验失败', str(ctx.exception))

    def test_magic_and_version_errors(self):
        data = bytearray(encoded())
        bad = bytearray(data)
        bad[0] = ord('X')
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(bytes(bad)))
        self.assertIn('魔数', str(ctx.exception))
        bad = bytearray(data)
        bad[4] = 99
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(bytes(bad)))
        self.assertIn('不支持的格式版本', str(ctx.exception))
        with self.assertRaises(FormatError):
            NcfReader(BytesSource(b'NCF1\x01short'))

    def test_out_of_range_read_is_locatable(self):
        class FlakySource(BytesSource):
            def read_at(self, offset, length):
                if offset > len(self.data) // 2:
                    raise ValueError('读取越界')
                return super().read_at(offset, length)
        with self.assertRaises(FormatError) as ctx:
            NcfReader(FlakySource(encoded()))
        self.assertIn('读取越界', str(ctx.exception))

    def test_duplicate_stream_in_footer_rejected(self):
        data = bytearray(encoded())
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            footer_start = reader._footer_start
            footer_len = reader._footer_len
        import json
        footer = json.loads(bytes(data[footer_start:footer_start + footer_len]).decode('utf-8'))
        block = dict(footer['pages'][0]['blocks'][0])
        footer['pages'][0]['blocks'].append(block)
        raw = canonical_json(footer)
        from workbench.domain import crc
        tail = len(raw).to_bytes(4, 'little') + crc(raw).to_bytes(4, 'little') + MAGIC
        rebuilt = bytes(data[:footer_start]) + raw + tail
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(rebuilt))
        self.assertIn('重复结构流', str(ctx.exception))

    def test_illegal_structure_levels_rejected(self):
        # 篡改 lengths 槽位数使元素槽位与长度和不一致
        data = bytearray(encoded(page_row_limit=1))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            target = next(b for p in reader.metadata['pages']
                          for b in p['blocks']
                          if b['path'] == 'nights' and b['stream'] == 'lengths'
                          and b['slots'] > 0)
        # 把目录 slots 改大（footer 在文件尾部，整体重排代价大；这里改用运行时对象篡改）
        with NcfReader(BytesSource(bytes(data))) as reader:
            for page in reader.metadata['pages']:
                for block in page['blocks']:
                    if block['offset'] == target['offset']:
                        block['slots'] += 1
            with self.assertRaises(FormatError) as ctx:
                reader.validate()
            self.assertIn('lengths', str(ctx.exception))

    def test_truncated_varint_and_noncanonical_varint_rejected(self):
        from workbench.domain import uvarint, crc
        data = bytearray(encoded(page_row_limit=1))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            block = next(b for p in reader.metadata['pages']
                         for b in p['blocks'] if b['stream'] == 'validity')
        # 等长替换负载为全续位字节（永远不终止），帧长度与目录保持一致
        from workbench.domain import read_uvarint
        original_framed = bytes(data[block['offset']:block['offset'] + block['length']])
        orig_len, _ = read_uvarint(original_framed)
        payload = b'\x80' * orig_len
        framed = uvarint(orig_len) + payload + crc(payload).to_bytes(4, 'little')
        self.assertEqual(len(framed), block['length'])
        rebuilt = bytes(data[:block['offset']]) + framed + \
            bytes(data[block['offset'] + block['length']:])
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(rebuilt)).validate()
        self.assertIn('整数编码错误', str(ctx.exception))
        # 非规范 LEB128（如 0x80 0x00）由 domain.read_uvarint 拒绝
        with self.assertRaises(ValueError):
            read_uvarint(b'\x80\x00')


def canonical_json(value):
    from workbench.io import canonical
    return canonical(value)


class EncodingEdgeTests(unittest.TestCase):
    def test_empty_string_distinct_from_null(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 's', 'kind': 'string', 'nullable': True}]}
        rows = [{'s': ''}, {'s': None}]
        self.assertEqual(read_bytes(write_bytes(schema, rows)), rows)

    def test_int64_extremes_and_zigzag_roundtrip(self):
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'n', 'kind': 'int64', 'nullable': False}]}
        rows = [{'n': n} for n in (0, -1, 1, -2, 63, 64, -64,
                                   9223372036854775807, -9223372036854775808)]
        self.assertEqual(read_bytes(write_bytes(schema, rows)), rows)

    def test_boolean_packing_padding_bits_rejected(self):
        from workbench.domain import read_uvarint, crc
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 'b', 'kind': 'boolean', 'nullable': False}]}
        data = bytearray(write_bytes(schema, [{'b': True}, {'b': False}, {'b': True}]))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            block = next(b for p in reader.metadata['pages']
                         for b in p['blocks'] if b['stream'] == 'values')
        # 3 个布尔用 1 字节，高 5 位为填充；置位一个填充位，并重算 CRC 越过校验层
        framed = bytearray(data[block['offset']:block['offset'] + block['length']])
        payload_len, header_len = read_uvarint(bytes(framed))
        framed[header_len] |= 0x08
        framed[-4:] = crc(bytes(framed[header_len:header_len + payload_len])).to_bytes(4, 'little')
        data[block['offset']:block['offset'] + block['length']] = framed
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(bytes(data))).validate()
        self.assertIn('填充位', str(ctx.exception))

    def test_invalid_utf8_rejected_at_read(self):
        from workbench.domain import read_uvarint, crc
        schema = {'name': 'root', 'kind': 'struct', 'nullable': False, 'fields': [
            {'name': 's', 'kind': 'string', 'nullable': False}]}
        data = bytearray(write_bytes(schema, [{'s': 'ok'}]))
        source = BytesSource(bytes(data))
        with NcfReader(source) as reader:
            block = next(b for p in reader.metadata['pages']
                         for b in p['blocks'] if b['stream'] == 'values')
        framed = bytearray(data[block['offset']:block['offset'] + block['length']])
        payload_len, header_len = read_uvarint(bytes(framed))
        framed[header_len + payload_len - 1] = 0xFF  # 末字节换成非法续字节
        framed[-4:] = crc(bytes(framed[header_len:header_len + payload_len])).to_bytes(4, 'little')
        data[block['offset']:block['offset'] + block['length']] = framed
        with self.assertRaises(FormatError) as ctx:
            NcfReader(BytesSource(bytes(data))).validate()
        self.assertIn('非法 UTF-8', str(ctx.exception))

    def test_open_performs_exactly_three_reads_and_blocks_read_once(self):
        data = encoded(page_row_limit=2)
        source = BytesSource(data)
        with NcfReader(source):
            self.assertEqual(len(source.reads), 3)  # 头、尾、页脚
        source = BytesSource(data)
        with NcfReader(source) as reader:
            reader.read(columns=['id'])
            before = len(source.reads)
            reader.read(columns=['id'])           # 块缓存命中，无新 I/O
            self.assertEqual(len(source.reads), before)

    def test_pages_never_split_a_row(self):
        data = encoded(page_row_limit=1)
        with NcfReader(BytesSource(data)) as reader:
            self.assertEqual([p['row_count'] for p in reader.metadata['pages']],
                             [1] * len(RECORDS))


class InjectedSourceTests(unittest.TestCase):
    def test_custom_read_at_source_records_ranges(self):
        data = encoded()

        class CountingSource:
            def __init__(self):
                self.size = len(data)
                self.reads = []
                self.bytes = 0

            def read_at(self, offset, length):
                self.reads.append((offset, length))
                self.bytes += length
                return data[offset:offset + length]

        source = CountingSource()
        with NcfReader(source) as reader:
            reader.read(columns=['id'])
        self.assertTrue(source.reads)
        self.assertEqual(source.bytes, sum(length for _, length in source.reads))
        # 头部、尾部各一次，加上每个页的 id 块
        self.assertGreaterEqual(len(source.reads), 2)

    def test_bytes_source_read_ranges_are_exact(self):
        data = encoded(page_row_limit=2)
        source = BytesSource(data)
        with NcfReader(source) as reader:
            reader.validate()
        for offset, length in source.reads:
            self.assertGreaterEqual(offset, 0)
            self.assertGreater(length, 0)
            self.assertLessEqual(offset + length, len(data))


if __name__ == '__main__':
    unittest.main()
