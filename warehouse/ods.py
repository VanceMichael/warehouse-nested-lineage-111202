"""ODS 接入层：按字节扫描 CSV / JSONL 来源，保留原始位置与文本，输出明确诊断。

- CSV 内嵌 JSON 列（表头 order_id,payload），支持引号内换行；引号错误按记录隔离并继续扫描。
- JSONL 每行一条记录。
- 编码错误定位到绝对字节位置，原始字节以 base64 保留，诊断不丢原始文本。
- JSON 数字按十进制解析，超过双精度 15 位有效数字时给出 PRECISION_RISK 警告并精确保留。
"""
import base64
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .catalog import transaction

CSV_KIND = 'csv'
JSONL_KIND = 'jsonl'
HEADER = ['order_id', 'payload']


class JsonDiag(Exception):
    """可诊断的来源解析失败，code 为机器可读原因码。"""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsonDiag('JSON_DUPLICATE_KEY', f'对象键重复: {key}')
        result[key] = value
    return result


def parse_json_strict(text):
    """严格解析 JSON：数字进 Decimal，拒绝 NaN/Infinity，诊断重复键与精度风险。"""
    def _constant(name):
        raise JsonDiag('JSON_CONSTANT', f'非法 JSON 常量: {name}（JSON 不允许 NaN/Infinity）')
    try:
        value = json.loads(text, parse_float=Decimal, parse_int=Decimal,
                           parse_constant=_constant, object_pairs_hook=_pairs_hook)
    except json.JSONDecodeError as error:
        raise JsonDiag('JSON_SYNTAX', f'JSON 语法错误 第{error.lineno}行第{error.colno}列: {error.msg}') from None
    warnings = []
    _precision_walk(value, '$', warnings)
    return value, warnings


def _precision_walk(value, path, warnings):
    if isinstance(value, Decimal):
        digits = len(value.as_tuple().digits)
        if digits > 15:
            warnings.append({'code': 'PRECISION_RISK', 'path': path, 'value': str(value),
                             'reason': f'有效数字 {digits} 位超出双精度 15 位，已按十进制精确保留，未转二进制浮点'})
    elif isinstance(value, dict):
        for key, item in value.items():
            _precision_walk(item, f'{path}.{key}', warnings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _precision_walk(item, f'{path}[{index}]', warnings)


def parse_csv_fields(text):
    """解析单条 CSV 记录（可能含引号内换行）为字段列表。"""
    try:
        rows = list(csv.reader([text], strict=True))
    except csv.Error as error:
        raise JsonDiag('CSV_QUOTING', f'CSV 字段解析失败: {error}') from None
    return rows[0] if rows else []


def _scan_csv_records(data):
    """字节级 CSV 记录扫描，产出 (start, end, error)；error 为 (code, reason) 或 None。

    引号内换行属于同一条记录；引号错误时隔离当前记录并在下一行恢复扫描。
    """
    records = []
    n = len(data)
    i = 0
    start = 0
    in_quotes = False
    after_quote = False
    field_start = True
    broken = None
    while i < n:
        byte = data[i]
        if broken is not None:
            if byte == 0x0A:
                records.append((start, i, broken))
                broken = None
                i += 1
                start = i
                field_start = True
                after_quote = False
            else:
                i += 1
            continue
        if in_quotes:
            if byte == 0x22:
                if i + 1 < n and data[i + 1] == 0x22:
                    i += 2
                else:
                    in_quotes = False
                    after_quote = True
                    i += 1
            else:
                i += 1
            continue
        if after_quote:
            if byte == 0x2C:
                after_quote = False
                field_start = True
                i += 1
            elif byte == 0x0D or byte == 0x0A:
                records.append((start, i, None))
                if byte == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
                    i += 1
                i += 1
                start = i
                field_start = True
                after_quote = False
            else:
                broken = ('CSV_QUOTING', f'引号闭合后跟随非法字节 0x{byte:02x}（应为逗号或换行，字段内引号需双写转义）')
                i += 1
            continue
        if byte == 0x22:
            if field_start:
                in_quotes = True
                field_start = False
                i += 1
            else:
                broken = ('CSV_QUOTING', '未加引号的字段内出现引号（含引号的字段需整体加引号并双写转义）')
                i += 1
        elif byte == 0x2C:
            field_start = True
            i += 1
        elif byte == 0x0D or byte == 0x0A:
            records.append((start, i, None))
            if byte == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
                i += 1
            i += 1
            start = i
            field_start = True
        else:
            field_start = False
            i += 1
    if in_quotes and broken is None:
        broken = ('CSV_QUOTING', '文件结束时引号未闭合')
    if start < n or broken is not None:
        records.append((start, n, broken))
    return records


def _scan_jsonl_records(data):
    """按行切分 JSONL，产出 (start, end, None)，end 不含行尾换行。"""
    records = []
    start = 0
    n = len(data)
    for i in range(n):
        if data[i] == 0x0A:
            end = i
            if end > start and data[end - 1] == 0x0D:
                end -= 1
            records.append((start, end, None))
            start = i + 1
    if start < n:
        end = n
        if data[end - 1] == 0x0D:
            end -= 1
        records.append((start, end, None))
    return records


def _detect_kind(path):
    return CSV_KIND if path.suffix.lower() == '.csv' else JSONL_KIND


def _strip_bom(data, notes):
    if data.startswith(b'\xef\xbb\xbf'):
        notes.append('检测到 UTF-8 BOM，已跳过前 3 字节')
        return data[3:], 3
    return data, 0


def _decode(raw, absolute_start):
    """返回 (text, encoding_error)；失败时 text 为转义展示形态，原始字节另行保留。"""
    try:
        return raw.decode('utf-8'), None
    except UnicodeDecodeError as error:
        absolute = absolute_start + error.start
        safe = raw.decode('utf-8', 'surrogateescape').encode('ascii', 'backslashreplace').decode('ascii')
        reason = f'字节 {absolute} 处 UTF-8 解码失败: {error.reason}（原始字节已按 base64 保留）'
        return safe, ('ENCODING', reason)


def scan_records(path, kind=None):
    """扫描来源文件，产出记录描述列表（不落库），供接入与重处理预检共用。"""
    path = Path(path)
    data = path.read_bytes()
    kind = kind or _detect_kind(path)
    notes = []
    segment, base = _strip_bom(data, notes)
    scanner = _scan_csv_records if kind == CSV_KIND else _scan_jsonl_records
    results = []
    for seq, (start, end, scan_error) in enumerate(scanner(segment)):
        raw = segment[start:end]
        text, encoding_error = _decode(raw, base + start)
        results.append({'seq': seq, 'byte_start': base + start, 'byte_end': base + end,
                        'raw': raw, 'text': text, 'scan_error': scan_error,
                        'encoding_error': encoding_error})
    return {'kind': kind, 'notes': notes, 'records': results}


def payload_of(record, header):
    """从扫描记录中提取 JSON 载荷文本；失败抛 JsonDiag。"""
    if record['kind'] == CSV_KIND:
        fields = parse_csv_fields(record['text'])
        if len(fields) != len(header):
            raise JsonDiag('CSV_FIELD_COUNT', f'字段数 {len(fields)} 与表头 {len(header)} 不一致')
        return fields[header.index('payload')], fields[header.index('order_id')]
    return record['text'], None


def read_payload_order_ids(path, kind=None):
    """尽力解析文件中的 order_id 集合（重处理前校验用，不落库）。"""
    scanned = scan_records(path, kind)
    header = HEADER
    ids = set()
    for record in scanned['records']:
        if record['scan_error'] or record['encoding_error']:
            continue
        text = record['text']
        if not text.strip():
            continue
        try:
            if scanned['kind'] == CSV_KIND:
                if record['seq'] == 0:
                    header = parse_csv_fields(text)
                    continue
                payload, _ = payload_of({**record, 'kind': CSV_KIND}, header)
            else:
                payload = text
            value, _ = parse_json_strict(payload)
        except JsonDiag:
            continue
        if isinstance(value, dict) and isinstance(value.get('order_id'), str):
            ids.add(value['order_id'])
    return ids


def ingest_file(connection, path, batch, kind=None):
    """把来源包接入 ODS：整包一个事务，坏记录隔离但保留字节位置与原文。"""
    path = Path(path)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    report = {'batch': batch, 'changed': False, 'records': 0, 'quarantined': [], 'warnings': [], 'notes': []}
    with transaction(connection):
        previous = connection.execute('SELECT digest FROM ingest_batches WHERE batch=?', (batch,)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise ValueError('同批次内容不一致')
            return report
        scanned = scan_records(path, kind)
        report['notes'] = scanned['notes']
        header = HEADER
        count = 0

        def quarantine(record, code, reason, order_id=None, payload=None):
            raw_b64 = None
            raw_text = record['text']
            if record['encoding_error']:
                raw_b64 = base64.b64encode(record['raw']).decode('ascii')
            cursor = connection.execute(
                'INSERT INTO ods_raw (batch, seq, kind, byte_start, byte_end, raw_text, payload, order_id, warnings, status)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (batch, record['seq'], scanned['kind'], record['byte_start'], record['byte_end'],
                 raw_text, payload, order_id, '[]', 'quarantined'))
            connection.execute(
                'INSERT INTO quarantine (stage, ods_id, batch, order_id, byte_start, byte_end,'
                ' reason_code, reason, raw_text, raw_b64) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('ingest', cursor.lastrowid, batch, order_id, record['byte_start'], record['byte_end'],
                 code, reason, raw_text, raw_b64))
            report['quarantined'].append({'seq': record['seq'], 'byte_start': record['byte_start'],
                                          'byte_end': record['byte_end'], 'code': code, 'reason': reason})

        for record in scanned['records']:
            text = record['text']
            if record['scan_error'] is None and record['encoding_error'] is None and not text.strip():
                continue
            if record['scan_error']:
                code, reason = record['scan_error']
                quarantine(record, code, reason)
                continue
            if record['encoding_error']:
                code, reason = record['encoding_error']
                quarantine(record, code, reason)
                continue
            if scanned['kind'] == CSV_KIND and record['seq'] == 0:
                header = parse_csv_fields(text)
                if sorted(header) != HEADER:
                    raise ValueError(f'CSV 表头需要 {HEADER}，实际为 {header}')
                continue
            try:
                payload, column_id = payload_of({**record, 'kind': scanned['kind']}, header)
                value, warnings = parse_json_strict(payload)
                if not isinstance(value, dict):
                    raise JsonDiag('INVALID_SHAPE', '顶层必须是对象')
                order_id = value.get('order_id')
                if order_id is not None and not isinstance(order_id, str):
                    raise JsonDiag('INVALID_SHAPE', 'order_id 需要字符串')
                if column_id is not None and order_id is not None and column_id != order_id:
                    raise JsonDiag('ID_MISMATCH', f'CSV 列 order_id={column_id} 与载荷 order_id={order_id} 不一致')
            except JsonDiag as error:
                quarantine(record, error.code, error.reason)
                continue
            for warning in warnings:
                report['warnings'].append({'seq': record['seq'], 'order_id': order_id, **warning})
            connection.execute(
                'INSERT INTO ods_raw (batch, seq, kind, byte_start, byte_end, raw_text, payload, order_id, warnings, status)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (batch, record['seq'], scanned['kind'], record['byte_start'], record['byte_end'],
                 text, payload, order_id, json.dumps(warnings, ensure_ascii=False), 'parsed'))
            count += 1
        connection.execute('INSERT INTO ingest_batches VALUES (?, ?, ?)', (batch, digest, count))
        report['changed'] = True
        report['records'] = count
    return report
