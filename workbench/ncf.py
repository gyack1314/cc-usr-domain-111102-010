"""NCF：嵌套列式文件格式（Nested Columnar Format）。

结构流模型（等价于定义级别/重复级别，见 docs/ncf-format.md）：
- 每个可空节点一段 validity 位图（定义级别：该槽位是否为空）；
- 每个 list 节点一段 lengths 长度流（重复级别：显式给出每个非空列表的元素数，
  行边界不依赖叶值数量）；
- 每个叶节点一段 values 流，只编码非空叶值。

块、页、页脚与校验规则见 docs/ncf-format.md。本模块只依赖标准库与 .domain 原语。
"""
import json
import os
import tempfile
from pathlib import Path

from .domain import uvarint, read_uvarint, zigzag, unzigzag, crc, BytesSource
from .io import canonical

MAGIC = b'NCF1'
FORMAT_VERSION = 1          # 文件布局版本
ENCODING_VERSION = 1        # 叶编码版本
SUPPORTED_VERSIONS = (1,)
TAIL_SIZE = 12              # u32 footer_len + u32 crc + magic4
CRC_SIZE = 4
LEAF_KINDS = ('int64', 'boolean', 'string')
NODE_KINDS = ('struct', 'list') + LEAF_KINDS


class FormatError(ValueError):
    """可定位的格式错误。page/path/stream/offset 至少给出可用定位信息。"""

    def __init__(self, message, *, page=None, path=None, stream=None, offset=None):
        self.page = page
        self.path = path
        self.stream = stream
        self.offset = offset
        location = []
        if page is not None:
            location.append(f'page={page}')
        if path is not None:
            location.append(f'path={path or "<root>"}')
        if stream is not None:
            location.append(f'stream={stream}')
        if offset is not None:
            location.append(f'offset={offset}')
        text = message + ((' @ ' + ' '.join(location)) if location else '')
        super().__init__(text)


# ----------------------------------------------------------------  schema 校验

def validate_schema(node, _top=True):
    if not isinstance(node, dict):
        raise ValueError('schema 节点必须是对象')
    name = node.get('name')
    if not isinstance(name, str) or not name:
        raise ValueError('schema 节点 name 必须是非空字符串')
    if not _top and '.' in name:
        raise ValueError(f'字段名不能含点: {name}')
    if not isinstance(node.get('nullable'), bool):
        raise ValueError(f'节点 {name} 的 nullable 必须是布尔值')
    kind = node.get('kind')
    if kind not in NODE_KINDS:
        raise ValueError(f'节点 {name} 的 kind 非法: {kind!r}')
    if kind == 'struct':
        fields = node.get('fields')
        if not isinstance(fields, list):
            raise ValueError(f'struct 节点 {name} 必须含 fields 列表')
        seen = set()
        for field in fields:
            validate_schema(field, _top=False)
            if field['name'] in seen:
                raise ValueError(f'struct {name} 下字段重复: {field["name"]}')
            seen.add(field['name'])
    elif kind == 'list':
        element = node.get('element')
        if not isinstance(element, dict):
            raise ValueError(f'list 节点 {name} 必须含 element')
        validate_schema(element, _top=False)
        if element['name'] != 'item':
            raise ValueError(f'list 节点 {name} 的 element.name 必须为 item')
    return node


def iter_nodes(node, path=()):
    """前序遍历，产出 (node, path_tuple)；根节点路径为空元组。"""
    yield node, path
    kind = node['kind']
    if kind == 'struct':
        for field in node['fields']:
            yield from iter_nodes(field, path + (field['name'],))
    elif kind == 'list':
        yield from iter_nodes(node['element'], path + ('item',))


def leaf_paths(schema):
    return ['.'.join(path) for node, path in iter_nodes(schema) if node['kind'] in LEAF_KINDS]


# ----------------------------------------------------  写入前校验与规范化

def normalize(node, value):
    """按 schema 校验并规范化一个值；缺失可空字段规范成 None，非法输入直接报错。"""
    kind = node['kind']
    if value is None:
        if not node['nullable']:
            raise ValueError(f'必选节点 {node["name"]} 不能为 null')
        return None
    if kind == 'struct':
        if not isinstance(value, dict):
            raise ValueError(f'节点 {node["name"]} 应为 struct 对象')
        known = {f['name'] for f in node['fields']}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f'struct {node["name"]} 含未知字段: {sorted(unknown)}')
        out = {}
        for field in node['fields']:
            if field['name'] in value:
                out[field['name']] = normalize(field, value[field['name']])
            elif field['nullable']:
                out[field['name']] = None
            else:
                raise ValueError(f'struct {node["name"]} 缺必选字段 {field["name"]}')
        return out
    if kind == 'list':
        if not isinstance(value, list):
            raise ValueError(f'节点 {node["name"]} 应为列表')
        return [normalize(node['element'], item) for item in value]
    if kind == 'int64':
        if type(value) is not int:
            raise ValueError(f'节点 {node["name"]} 应为 int64 整数')
        zigzag(value)  # 复用其 64 位有符号范围检查
        return value
    if kind == 'boolean':
        if type(value) is not bool:
            raise ValueError(f'节点 {node["name"]} 应为布尔值')
        return value
    if kind == 'string':
        if type(value) is not str:
            raise ValueError(f'节点 {node["name"]} 应为 UTF-8 字符串')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ValueError(f'节点 {node["name"]} 含非法 Unicode（代理项）') from exc
        return value
    raise ValueError(f'未知 kind: {kind}')


def normalize_rows(schema, rows):
    validate_schema(schema)
    if not isinstance(rows, list):
        raise ValueError('rows 必须是顶层行列表')
    return [normalize(schema, row) for row in rows]


# ----------------------------------------------------------------  基础编码

def pack_bits(bits):
    """LSB-first 布尔打包；bits 为 bool/int 序列。"""
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def unpack_bits(buf, count, location):
    if len(buf) != (count + 7) // 8:
        raise FormatError('位图字节数与槽位数不符', **location)
    if count & 7 and buf[-1] >> (count & 7):
        raise FormatError('位图填充位非零', **location)
    return [(buf[i >> 3] >> (i & 7)) & 1 for i in range(count)]


class _Cursor:
    """带绝对偏移的负载解析器，任何截断都转成可定位 FormatError。"""

    def __init__(self, data, base, location):
        self.data = data
        self.base = base
        self.pos = 0
        self.location = location

    def uvarint(self):
        try:
            value, new_pos = read_uvarint(self.data, self.pos)
        except ValueError as exc:
            raise FormatError(f'整数编码错误: {exc}', offset=self.base + self.pos,
                              **self.location) from exc
        self.pos = new_pos
        return value

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError('负载截断', offset=self.base + self.pos, **self.location)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u32le(self):
        return int.from_bytes(self.take(4), 'little', signed=False)

    def finish(self):
        if self.pos != len(self.data):
            raise FormatError('负载存在多余字节', offset=self.base + self.pos, **self.location)


# ----------------------------------------------------------------  结构流负载

def _validity_payload(values):
    bits = [v is not None for v in values]
    return uvarint(len(bits)) + pack_bits(bits), len(bits)


def _lengths_payload(values):
    """values 为 list 或 None 的槽位序列；只对非空列表给长度（含 0 长度空列表）。"""
    lengths = [len(v) for v in values if v is not None]
    return uvarint(len(lengths)) + b''.join(uvarint(n) for n in lengths), len(lengths)


def _leaf_payload(node, values):
    """values 为叶槽位序列（含 None）；只编码非空值。"""
    kind = node['kind']
    present = [v for v in values if v is not None]
    if kind == 'int64':
        payload = b''.join(uvarint(zigzag(v)) for v in present)
    elif kind == 'boolean':
        payload = pack_bits(present)
    else:
        chunks = []
        for v in present:
            raw = v.encode('utf-8')
            chunks.append(uvarint(len(raw)))
            chunks.append(raw)
        payload = b''.join(chunks)
    return payload, len(present)


def _build_node(node, path, values, blocks):
    """前序递归把一个页内的节点值编码成 (path,stream,payload,slots) 块列表。"""
    kind = node['kind']
    if node['nullable']:
        payload, slots = _validity_payload(values)
        blocks.append((path, 'validity', payload, slots))
    if kind == 'struct':
        defined = [v if v is not None else {} for v in values]
        for field in node['fields']:
            child_values = [row.get(field['name']) for row in defined]
            _build_node(field, path + (field['name'],), child_values, blocks)
    elif kind == 'list':
        payload, slots = _lengths_payload(values)
        blocks.append((path, 'lengths', payload, slots))
        element_values = [item for seq in values if seq is not None for item in seq]
        _build_node(node['element'], path + ('item',), element_values, blocks)
    else:
        payload, slots = _leaf_payload(node, values)
        blocks.append((path, 'values', payload, slots))


def _frame(payload):
    """块帧 = uvarint(payload_len) || payload || u32le crc32(payload)。"""
    return uvarint(len(payload)) + payload + crc(payload).to_bytes(4, 'little')


# ----------------------------------------------------------------  写入

def write_bytes(schema, rows, *, page_row_limit=1000):
    """编码为 NCF 字节串。rows 会先整体校验规范化，非法输入在写字节前报错。"""
    validate_schema(schema)
    rows = normalize_rows(schema, rows)
    if type(page_row_limit) is not int or page_row_limit <= 0:
        raise ValueError('page_row_limit 必须是正整数')
    out = bytearray(MAGIC + uvarint(FORMAT_VERSION))
    pages = []
    for start in range(0, len(rows), page_row_limit):
        page_rows = rows[start:start + page_row_limit]
        raw_blocks = []
        _build_node(schema, (), page_rows, raw_blocks)
        directory_blocks = []
        for path_tuple, stream, payload, slots in raw_blocks:
            framed = _frame(payload)
            offset = len(out)
            out += framed
            directory_blocks.append({'path': '.'.join(path_tuple), 'stream': stream,
                                     'offset': offset, 'length': len(framed),
                                     'slots': slots})
        pages.append({'page': len(pages), 'row_start': start,
                      'row_count': len(page_rows), 'blocks': directory_blocks})
    footer = {
        'format': 'ncf',
        'version': FORMAT_VERSION,
        'encoding_version': ENCODING_VERSION,
        'row_count': len(rows),
        'page_row_limit': page_row_limit,
        'schema': schema,
        'leaf_columns': leaf_paths(schema),
        'pages': pages,
    }
    footer_bytes = canonical(footer)
    out += footer_bytes
    out += len(footer_bytes).to_bytes(4, 'little')
    out += crc(footer_bytes).to_bytes(4, 'little')
    out += MAGIC
    return bytes(out)


def write_file(path, schema, rows, **kwargs):
    data = write_bytes(schema, rows, **kwargs)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.writing-ncf-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


# ----------------------------------------------------------------  读取端源

class FileSource:
    """文件 read_at 源；reads 记录与 BytesSource 一致，便于观察 I/O 范围。"""

    def __init__(self, path):
        self.path = Path(path)
        self._stream = open(self.path, 'rb')
        self._stream.seek(0, os.SEEK_END)
        self.size = self._stream.tell()
        self.reads = []

    def read_at(self, offset, length):
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0:
            raise ValueError('读取参数非法')
        self._stream.seek(offset)
        data = self._stream.read(length)
        if len(data) != length:
            raise ValueError('读取越界')
        self.reads.append((offset, length))
        return data

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _as_source(source):
    if isinstance(source, (str, Path)):
        return FileSource(source), True
    if isinstance(source, FileSource):
        return source, True
    if isinstance(source, BytesSource):
        source.size = len(source.data)
        return source, False
    if hasattr(source, 'read_at'):
        if not hasattr(source, 'size'):
            raise ValueError('read_at 源必须提供 size 属性')
        return source, False
    raise ValueError('source 必须是路径、BytesSource 或带 read_at/size 的对象')


class NcfReader:
    def __init__(self, source):
        self._source, self._owns = _as_source(source)
        self._cache = {}
        self.metadata = self._open_footer()
        self.schema = self.metadata['schema']
        self.row_count = self.metadata['row_count']
        self._footer_start = self.metadata['_footer_start']
        self._footer_len = self.metadata['_footer_len']

    def close(self):
        if self._owns:
            self._source.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 页脚打开
    def _read(self, offset, length, location):
        try:
            data = self._source.read_at(offset, length)
        except ValueError as exc:
            raise FormatError(f'读取越界: {exc}', offset=offset, **location) from exc
        if len(data) != length:
            raise FormatError('读取返回长度不足', offset=offset, **location)
        return data

    def _open_footer(self):
        size = self._source.size
        if size < len(MAGIC) + 1 + TAIL_SIZE:
            raise FormatError('文件过小，不含完整头尾')
        head = self._read(0, len(MAGIC) + 1, {'stream': 'header'})
        if head[:len(MAGIC)] != MAGIC:
            raise FormatError('魔数不匹配，不是 NCF 文件', offset=0)
        try:
            version, _ = read_uvarint(head, len(MAGIC))
        except ValueError as exc:
            raise FormatError(f'版本编码错误: {exc}', offset=len(MAGIC)) from exc
        if version not in SUPPORTED_VERSIONS:
            raise FormatError(f'不支持的格式版本 {version}，支持版本 {list(SUPPORTED_VERSIONS)}',
                              offset=len(MAGIC))
        tail = self._read(size - TAIL_SIZE, TAIL_SIZE, {'stream': 'footer-tail'})
        if tail[-4:] != MAGIC:
            raise FormatError('尾部魔数不匹配', offset=size - 4)
        footer_crc = int.from_bytes(tail[4:8], 'little')
        footer_len = int.from_bytes(tail[:4], 'little')
        data_start = len(MAGIC) + 1
        if footer_len == 0 or footer_len + TAIL_SIZE > size - data_start:
            raise FormatError('页脚长度越界', offset=size - TAIL_SIZE)
        footer_start = size - TAIL_SIZE - footer_len
        raw = self._read(footer_start, footer_len, {'stream': 'footer'})
        if crc(raw) != footer_crc:
            raise FormatError('页脚校验失败', offset=footer_start)
        try:
            footer = json.loads(raw.decode('utf-8'), object_pairs_hook=self._no_dup_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FormatError(f'页脚不是合法 UTF-8/JSON: {exc}', offset=footer_start) from exc
        footer['_footer_start'] = footer_start
        footer['_footer_len'] = footer_len
        self._check_footer(footer)
        return footer

    @staticmethod
    def _no_dup_keys(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FormatError(f'页脚 JSON 含重复键: {key}')
            result[key] = value
        return result

    def _check_footer(self, footer):
        if footer.get('format') != 'ncf':
            raise FormatError('页脚 format 标识非法')
        if footer.get('version') not in SUPPORTED_VERSIONS:
            raise FormatError(f"不支持的格式版本 {footer.get('version')}")
        if footer.get('encoding_version') != ENCODING_VERSION:
            raise FormatError(
                f"不支持的编码版本 {footer.get('encoding_version')}，支持 {[ENCODING_VERSION]}")
        if type(footer.get('row_count')) is not int or footer['row_count'] < 0:
            raise FormatError('行数非法')
        if type(footer.get('page_row_limit')) is not int or footer['page_row_limit'] <= 0:
            raise FormatError('page_row_limit 非法')
        schema = footer.get('schema')
        validate_schema(schema)
        expected_leaves = leaf_paths(schema)
        leaves = footer.get('leaf_columns')
        if not isinstance(leaves, list) or len(leaves) != len(set(leaves)):
            raise FormatError('叶列路径缺失或重复')
        if sorted(leaves) != sorted(expected_leaves):
            raise FormatError('叶列路径与 schema 不一致')
        pages = footer.get('pages')
        if not isinstance(pages, list):
            raise FormatError('页目录非法')
        size = self._source.size
        data_start = len(MAGIC) + 1
        footer_start = footer['_footer_start']
        expected_streams = {'.'.join(p): self._streams_of(node)
                            for node, p in iter_nodes(schema)}
        expected_row = 0
        total_rows = 0
        seen_ranges = []
        for page in pages:
            pid = page['page']
            if not isinstance(pid, int) or page['row_start'] != expected_row \
                    or page['row_count'] <= 0:
                raise FormatError('页行范围不连续或为空', page=pid)
            expected_row += page['row_count']
            total_rows += page['row_count']
            blocks = page.get('blocks')
            if not isinstance(blocks, list):
                raise FormatError('页块目录非法', page=pid)
            seen_stream_keys = set()
            for block in blocks:
                loc = {'page': pid, 'path': block.get('path'),
                       'stream': block.get('stream')}
                off, length = block.get('offset'), block.get('length')
                if type(off) is not int or type(length) is not int or off < 0 or length <= 0:
                    raise FormatError('块范围非法', **loc)
                if off < data_start or off + length > footer_start:
                    raise FormatError('块偏移越界', **loc)
                if block['path'] not in expected_streams \
                        or block['stream'] not in expected_streams[block['path']]:
                    raise FormatError('块不属于 schema 节点或流类型非法', **loc)
                key = (block['path'], block['stream'])
                if key in seen_stream_keys:
                    raise FormatError('页内重复结构流', **loc)
                seen_stream_keys.add(key)
                if type(block.get('slots')) is not int or block['slots'] < 0:
                    raise FormatError('块槽位数非法', **loc)
                for other_off, other_len, other_name in seen_ranges:
                    if off < other_off + other_len and other_off < off + length:
                        raise FormatError(f'块范围与 {other_name} 重叠', **loc)
                seen_ranges.append((off, length, f"{block['path']}:{block['stream']}"))
            expected_keys = set()
            for node, node_path in iter_nodes(schema):
                p = '.'.join(node_path)
                expected_keys.update((p, s) for s in expected_streams[p])
            if seen_stream_keys != expected_keys:
                raise FormatError('页内结构流集合与 schema 不符', page=pid)
        if total_rows != footer['row_count']:
            raise FormatError('各页行数之和与总行数不符')
        if footer['row_count'] == 0 and pages:
            raise FormatError('零行文件不得含数据页')

    @staticmethod
    def _streams_of(node):
        streams = []
        if node['nullable']:
            streams.append('validity')
        if node['kind'] == 'list':
            streams.append('lengths')
        if node['kind'] in LEAF_KINDS:
            streams.append('values')
        return tuple(streams)

    def _node_at(self, path):
        node = self.schema
        for part in path:
            if node['kind'] == 'struct':
                node = next(f for f in node['fields'] if f['name'] == part)
            else:
                if part != 'item':
                    raise FormatError(f'结构层级非法：list 元素路径必须为 item，得到 {part}')
                node = node['element']
        return node

    # ------------------------------------------------------------ 块读取
    def _block(self, page_index, path, stream):
        key = (page_index, path, stream)
        if key in self._cache:
            return self._cache[key]
        page = self.metadata['pages'][page_index]
        match = [b for b in page['blocks'] if b['path'] == path and b['stream'] == stream]
        if not match:
            raise FormatError('结构流缺失', page=page_index, path=path, stream=stream)
        block = match[0]
        loc = {'page': page_index, 'path': path, 'stream': stream}
        framed = self._read(block['offset'], block['length'], loc)
        cur = _Cursor(framed, block['offset'], loc)
        payload_len = cur.uvarint()
        expected = block['length'] - cur.pos - CRC_SIZE
        if payload_len != expected:
            raise FormatError('块帧长度与目录不符', **loc)
        payload = cur.take(payload_len)
        stored_crc = cur.u32le()
        cur.finish()
        if crc(payload) != stored_crc:
            raise FormatError('块校验失败', **loc)
        self._cache[key] = (payload, block['slots'], block['offset'])
        return self._cache[key]

    def _parse_validity(self, page_index, path, slot_count):
        payload, slots, base = self._block(page_index, path, 'validity')
        loc = {'page': page_index, 'path': path, 'stream': 'validity'}
        if slots != slot_count:
            raise FormatError(f'validity 槽位 {slots} 与期望 {slot_count} 不符', **loc)
        cur = _Cursor(payload, base, loc)
        declared = cur.uvarint()
        if declared != slot_count:
            raise FormatError('validity 内嵌槽位数不符', **loc)
        packed = cur.take((slot_count + 7) // 8)
        cur.finish()
        return [bool(x) for x in unpack_bits(packed, slot_count, loc)]

    def _parse_lengths(self, page_index, path, defined_count):
        payload, slots, base = self._block(page_index, path, 'lengths')
        loc = {'page': page_index, 'path': path, 'stream': 'lengths'}
        if slots != defined_count:
            raise FormatError(f'lengths 条数 {slots} 与非空列表数 {defined_count} 不符', **loc)
        cur = _Cursor(payload, base, loc)
        declared = cur.uvarint()
        if declared != defined_count:
            raise FormatError('lengths 内嵌条数不符', **loc)
        lengths = [cur.uvarint() for _ in range(defined_count)]
        cur.finish()
        return lengths

    def _parse_values(self, node, page_index, path, valid_count):
        payload, slots, base = self._block(page_index, path, 'values')
        loc = {'page': page_index, 'path': path, 'stream': 'values'}
        if slots != valid_count:
            raise FormatError(f'values 条数 {slots} 与非空叶值数 {valid_count} 不符', **loc)
        cur = _Cursor(payload, base, loc)
        kind = node['kind']
        if kind == 'boolean':
            packed = cur.take((valid_count + 7) // 8)
            cur.finish()
            return [bool(x) for x in unpack_bits(packed, valid_count, loc)]
        values = []
        for _ in range(valid_count):
            if kind == 'int64':
                values.append(unzigzag(cur.uvarint()))
            else:
                byte_len = cur.uvarint()
                raw = cur.take(byte_len)
                try:
                    values.append(raw.decode('utf-8'))
                except UnicodeDecodeError as exc:
                    raise FormatError(f'非法 UTF-8 字符串: {exc}',
                                      offset=cur.base + cur.pos, **loc) from exc
        cur.finish()
        return values

    # ------------------------------------------------------------ 重建
    def _needed_paths(self, columns):
        all_paths = {'.'.join(p) for _, p in iter_nodes(self.schema)}
        if columns is None:
            return all_paths
        leaves = set(leaf_paths(self.schema))
        unknown = [c for c in columns if c not in leaves]
        if unknown:
            raise FormatError(f'未知叶列 {unknown}，可选 {sorted(leaves)}')
        needed = set()
        for column in columns:
            parts = column.split('.')
            for depth in range(len(parts) + 1):
                needed.add('.'.join(parts[:depth]))
        return needed

    def _materialize(self, node, path, page_index, needed, slot_count):
        """返回长度 slot_count 的槽位列表；不在投影内的节点返回 None。"""
        p = '.'.join(path)
        if p not in needed:
            return None
        kind = node['kind']
        if kind == 'struct':
            valid = (self._parse_validity(page_index, p, slot_count)
                     if node['nullable'] else [True] * slot_count)
            child_columns = {}
            for field in node['fields']:
                child_path = path + (field['name'],)
                child = self._materialize(field, child_path, page_index, needed, slot_count)
                if child is not None:
                    child_columns[field['name']] = child
            result = [None] * slot_count
            for i, ok in enumerate(valid):
                if not ok:
                    for child in child_columns.values():
                        if child[i] is not None:
                            raise FormatError('空 struct 的子节点必须为空',
                                              page=page_index, path=p)
                    continue
                result[i] = {name: child[i] for name, child in child_columns.items()}
            return result
        if kind == 'list':
            valid = (self._parse_validity(page_index, p, slot_count)
                     if node['nullable'] else [True] * slot_count)
            defined_count = sum(valid)
            lengths = self._parse_lengths(page_index, p, defined_count)
            element_total = sum(lengths)
            element_path = path + ('item',)
            element_values = self._materialize(
                node['element'], element_path, page_index, needed, element_total)
            if element_values is not None and len(element_values) != element_total:
                raise FormatError(
                    f'列表元素槽位 {len(element_values)} 与长度和 {element_total} 不符',
                    page=page_index, path=p)
            result = [None] * slot_count
            cursor = 0
            li = 0
            for i, ok in enumerate(valid):
                if not ok:
                    continue
                length = lengths[li]
                li += 1
                if element_values is None:
                    result[i] = [None] * length
                else:
                    result[i] = element_values[cursor:cursor + length]
                cursor += length
            return result
        # 叶节点
        valid = (self._parse_validity(page_index, p, slot_count)
                 if node['nullable'] else [True] * slot_count)
        plain = self._parse_values(node, page_index, p, sum(valid))
        result = [None] * slot_count
        vi = 0
        for i, ok in enumerate(valid):
            if ok:
                result[i] = plain[vi]
                vi += 1
        return result

    # ------------------------------------------------------------ 对外接口
    def read(self, columns=None, row_start=0, row_end=None):
        if row_end is None:
            row_end = self.row_count
        if type(row_start) is not int or type(row_end) is not int:
            raise FormatError('行范围必须是整数')
        if not (0 <= row_start <= row_end <= self.row_count):
            raise FormatError(f'行范围越界: [{row_start},{row_end}) / {self.row_count}')
        if columns is not None:
            columns = list(columns)
        needed = self._needed_paths(columns)
        output = []
        for page in self.metadata['pages']:
            start, count = page['row_start'], page['row_count']
            if start >= row_end or start + count <= row_start:
                continue
            local = self._materialize(self.schema, (), page['page'], needed, count)
            output.extend(local)
        if not output:
            return []
        first_start = min(p['row_start'] for p in self.metadata['pages']
                          if p['row_start'] < row_end
                          and p['row_start'] + p['row_count'] > row_start)
        begin = row_start - first_start
        return output[begin:begin + (row_end - row_start)]

    def validate(self):
        """全量校验：魔数/版本/页脚/目录/每块 CRC 与全部结构流一致性。"""
        needed = {'.'.join(p) for _, p in iter_nodes(self.schema)}
        expected_row = 0
        for page in self.metadata['pages']:
            if page['row_start'] != expected_row:
                raise FormatError('页行范围不连续', page=page['page'])
            expected_row += page['row_count']
            self._materialize(self.schema, (), page['page'], needed, page['row_count'])
        if expected_row != self.row_count:
            raise FormatError('页行数之和与总行数不符')
        return {'status': 'ok', 'rows': self.row_count,
                'pages': len(self.metadata['pages']),
                'leaf_columns': leaf_paths(self.schema)}


def open_file(source):
    return NcfReader(source)


def read_bytes(data, **kwargs):
    reader = NcfReader(BytesSource(data))
    try:
        return reader.read(**kwargs)
    finally:
        reader.close()


def read_file(path, **kwargs):
    with NcfReader(FileSource(path)) as reader:
        return reader.read(**kwargs)


def validate_file(path):
    with NcfReader(FileSource(path)) as reader:
        return reader.validate()
