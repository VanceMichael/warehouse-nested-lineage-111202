"""DWD 规范化层：把 ODS 记录规范化为 订单/行项目/标签 三组实体。

语义约定：
- 缺失字段表示补丁未触及；null 表示清除；空数组是有效空集合。三者不混同：
  lines_null / tags_null 标记区分“被 null 清除”与“显式空集合”。
- 血缘 = 父 order_id + 父 version + 子业务标识（line_id / tag）。
  数组重排不改变身份；子行内容未变时保留其引入版本，不随父版本空转。
- 子标识缺失或重复时整组隔离父记录，父与子的既有状态不被部分覆盖。
- 每个父记录组一个事务：父更新与子行删除、新增一起发布，不存在新父配旧子。
"""
import copy
import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .catalog import encode, transaction
from .ods import JsonDiag, parse_json_strict

SCALAR_FIELDS = ('buyer', 'currency', 'note')


class _Missing:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'MISSING'


MISSING = _Missing()


class QuarantineError(Exception):
    """规范化阶段的语义失败，整组隔离父记录。"""

    def __init__(self, code, reason, order_id=None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.order_id = order_id


def canonicalize(value):
    """归一化载荷：Decimal 转规范文本，lines 按 line_id 排序，tags 排序，对象键排序。

    数组重排不改变归一化结果，因此身份与内容摘要对重排免疫。
    """
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    if isinstance(value, dict):
        return {key: canonicalize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        items = [canonicalize(item) for item in value]
        if items and all(isinstance(item, dict) and 'line_id' in item for item in items):
            items.sort(key=lambda item: item['line_id'])
        elif items and all(isinstance(item, str) for item in items):
            items.sort()
        return items
    return value


def canonical_state_view(state):
    """把合并状态转为来源形态视图：lines/tags 按标识排序，null 清除与空集合保持区分。"""
    view = {'version': state['version']}
    view.update(state['scalars'])
    if state['lines_null']:
        view['lines'] = None
    else:
        view['lines'] = [canonicalize({'line_id': line_id, **line})
                         for line_id, line in sorted(state['lines'].items())]
    if state['tags_null']:
        view['tags'] = None
    else:
        view['tags'] = sorted(state['tags'])
    return view


def state_digest(state):
    """合并态内容摘要：同版本重投按合并结果判等，数组重排不改变身份。"""
    return hashlib.sha256(encode(canonicalize(canonical_state_view(state)))).hexdigest()


def _decimal_of(value, path):
    if isinstance(value, bool):
        raise QuarantineError('INVALID_NUMBER', f'{path} 需要十进制数或数字字符串，不允许布尔值')
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise QuarantineError('INVALID_NUMBER', f'{path} 不是十进制数: {value!r}') from None
    raise QuarantineError('INVALID_NUMBER', f'{path} 需要十进制数或数字字符串')


def _version_of(value):
    if isinstance(value, bool):
        raise QuarantineError('INVALID_VERSION', 'version 需要非负整数')
    if isinstance(value, Decimal):
        if value != value.to_integral_value() or value < 0:
            raise QuarantineError('INVALID_VERSION', f'version 需要非负整数，实际为 {value}')
        return int(value)
    raise QuarantineError('INVALID_VERSION', 'version 需要非负整数')


def _parse_lines(raw):
    lines = {}
    for index, item in enumerate(raw):
        path = f'lines[{index}]'
        if not isinstance(item, dict):
            raise QuarantineError('INVALID_SHAPE', f'{path} 需要对象')
        line_id = item.get('line_id', MISSING)
        if line_id is MISSING or not isinstance(line_id, str) or not line_id:
            raise QuarantineError('MISSING_CHILD_KEY', f'{path} 缺少有效 line_id，父记录整组隔离')
        if line_id in lines:
            raise QuarantineError('DUPLICATE_CHILD_KEY', f'lines 重复标识 {line_id}，父记录整组隔离')
        line = {}
        for field in ('sku',):
            value = item.get(field, MISSING)
            if value is MISSING or not isinstance(value, str) or not value:
                raise QuarantineError('MISSING_FIELD', f'{path}.{field} 缺失或不是非空字符串')
            line[field] = value
        for field in ('quantity', 'price'):
            value = item.get(field, MISSING)
            if value is MISSING:
                raise QuarantineError('MISSING_FIELD', f'{path}.{field} 缺失')
            line[field] = _decimal_of(value, f'{path}.{field}')
        lines[line_id] = line
    return lines


class Patch:
    def __init__(self, order_id, version, scalars, lines, tags):
        self.order_id = order_id
        self.version = version
        self.scalars = scalars
        self.lines = lines
        self.tags = tags


def build_patch(record):
    """把解析后的来源记录构建为补丁；语义失败抛 QuarantineError。"""
    if not isinstance(record, dict):
        raise QuarantineError('INVALID_SHAPE', '顶层必须是对象')
    order_id = record.get('order_id', MISSING)
    if order_id is MISSING or not isinstance(order_id, str) or not order_id:
        raise QuarantineError('MISSING_FIELD', '缺少有效 order_id')
    if 'version' not in record:
        raise QuarantineError('MISSING_FIELD', '缺少 version', order_id)
    version = _version_of(record['version'])
    scalars = {}
    for field in SCALAR_FIELDS:
        value = record.get(field, MISSING)
        if value is not MISSING and value is not None and not isinstance(value, str):
            raise QuarantineError('INVALID_SHAPE', f'{field} 需要字符串或 null', order_id)
        scalars[field] = value
    lines = MISSING
    if 'lines' in record:
        raw = record['lines']
        if raw is None:
            lines = None
        elif isinstance(raw, list):
            lines = _parse_lines(raw)
        else:
            raise QuarantineError('INVALID_SHAPE', 'lines 需要数组或 null', order_id)
    tags = MISSING
    if 'tags' in record:
        raw = record['tags']
        if raw is None:
            tags = None
        elif isinstance(raw, list):
            tags = set()
            for index, tag in enumerate(raw):
                if not isinstance(tag, str) or not tag:
                    raise QuarantineError('INVALID_SHAPE', f'tags[{index}] 需要非空字符串', order_id)
                if tag in tags:
                    raise QuarantineError('DUPLICATE_CHILD_KEY', f'tags 重复标识 {tag}，父记录整组隔离', order_id)
                tags.add(tag)
        else:
            raise QuarantineError('INVALID_SHAPE', 'tags 需要数组或 null', order_id)
    return Patch(order_id, version, scalars, lines, tags)


def empty_state():
    return {'version': None, 'scalars': {field: None for field in SCALAR_FIELDS},
            'lines_null': False, 'lines': {}, 'tags_null': False, 'tags': set(),
            'line_versions': {}, 'tag_versions': {}}


def merge_state(current, patch):
    """按三态语义合并：缺失保留、null 清除、空数组为有效空集合。"""
    if current is None:
        if patch.lines is MISSING or patch.tags is MISSING:
            raise QuarantineError('MISSING_FIELD', '首次入仓记录必须包含 lines 与 tags（可为 null 或空数组）',
                                  patch.order_id)
        state = empty_state()
    else:
        state = copy.deepcopy(current)
    state['version'] = patch.version
    for field, value in patch.scalars.items():
        if value is not MISSING:
            state['scalars'][field] = value
    if patch.lines is not MISSING:
        if patch.lines is None:
            state['lines'] = {}
            state['lines_null'] = True
        else:
            state['lines'] = patch.lines
            state['lines_null'] = False
    if patch.tags is not MISSING:
        if patch.tags is None:
            state['tags'] = set()
            state['tags_null'] = True
        else:
            state['tags'] = patch.tags
            state['tags_null'] = False
    return state


def decide(current, patch):
    """版本裁决：publish / duplicate / stale。

    同版本时按“补丁合并后的状态”判等：重投同一来源包（哪怕数组被重排、
    全量快照与部分补丁形态不同）是幂等空操作；合并结果不同才是版本冲突。
    """
    if current is None:
        return 'publish'
    if patch.version > current['version']:
        return 'publish'
    if patch.version == current['version']:
        if state_digest(merge_state(current, patch)) == state_digest(current):
            return 'duplicate'
        raise QuarantineError('VERSION_CONFLICT',
                              f'版本 {patch.version} 已入仓但内容不一致', patch.order_id)
    return 'stale'


def load_state(connection, order_id):
    row = connection.execute('SELECT * FROM dwd_order WHERE order_id=?', (order_id,)).fetchone()
    if row is None:
        return None
    state = empty_state()
    state['version'] = row['version']
    state['scalars'] = {'buyer': row['buyer'], 'currency': row['currency'], 'note': row['note']}
    state['lines_null'] = bool(row['lines_null'])
    state['tags_null'] = bool(row['tags_null'])
    for line in connection.execute('SELECT * FROM dwd_order_line WHERE order_id=?', (order_id,)):
        state['lines'][line['line_id']] = {'sku': line['sku'], 'quantity': Decimal(line['quantity']),
                                           'price': Decimal(line['price'])}
        state['line_versions'][line['line_id']] = line['version']
    for tag in connection.execute('SELECT * FROM dwd_order_tag WHERE order_id=?', (order_id,)):
        state['tags'].add(tag['tag'])
        state['tag_versions'][tag['tag']] = tag['version']
    return state


def publish(connection, state, previous, patch, batch):
    """单事务发布一个父记录组：父更新与子行删除、新增一起生效。

    内容未变的子行保留原行与引入版本，数组重排不产生任何写入。
    """
    order_id = patch.order_id
    connection.execute(
        'INSERT INTO dwd_order (order_id, version, buyer, currency, note, lines_null, tags_null,'
        ' state_hash, updated_batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
        ' ON CONFLICT(order_id) DO UPDATE SET version=excluded.version, buyer=excluded.buyer,'
        ' currency=excluded.currency, note=excluded.note, lines_null=excluded.lines_null,'
        ' tags_null=excluded.tags_null, state_hash=excluded.state_hash,'
        ' updated_batch=excluded.updated_batch',
        (order_id, state['version'], state['scalars']['buyer'], state['scalars']['currency'],
         state['scalars']['note'], int(state['lines_null']), int(state['tags_null']),
         state_digest(state), batch))
    if patch.lines is not MISSING:
        new_ids = sorted(state['lines'])
        if new_ids:
            placeholders = ', '.join('?' for _ in new_ids)
            connection.execute(f'DELETE FROM dwd_order_line WHERE order_id=? AND line_id NOT IN ({placeholders})',
                               [order_id, *new_ids])
        else:
            connection.execute('DELETE FROM dwd_order_line WHERE order_id=?', (order_id,))
        for line_id in new_ids:
            line = state['lines'][line_id]
            if previous is not None and previous['lines'].get(line_id) == line:
                continue
            connection.execute(
                'INSERT INTO dwd_order_line (order_id, line_id, version, sku, quantity, price)'
                ' VALUES (?, ?, ?, ?, ?, ?)'
                ' ON CONFLICT(order_id, line_id) DO UPDATE SET version=excluded.version,'
                ' sku=excluded.sku, quantity=excluded.quantity, price=excluded.price',
                (order_id, line_id, patch.version, line['sku'], str(line['quantity']), str(line['price'])))
    if patch.tags is not MISSING:
        new_tags = sorted(state['tags'])
        if new_tags:
            placeholders = ', '.join('?' for _ in new_tags)
            connection.execute(f'DELETE FROM dwd_order_tag WHERE order_id=? AND tag NOT IN ({placeholders})',
                               [order_id, *new_tags])
        else:
            connection.execute('DELETE FROM dwd_order_tag WHERE order_id=?', (order_id,))
        for tag in new_tags:
            if previous is not None and tag in previous['tags']:
                continue
            connection.execute(
                'INSERT INTO dwd_order_tag (order_id, tag, version) VALUES (?, ?, ?)'
                ' ON CONFLICT(order_id, tag) DO UPDATE SET version=excluded.version',
                (order_id, tag, patch.version))


def _record_from_ods(row):
    try:
        value, _warnings = parse_json_strict(row['payload'])
    except JsonDiag as error:
        raise QuarantineError(error.code, error.reason) from None
    return value


def normalize(connection, only_batch=None, fail_at=None, fail_inside_at=None):
    """把 ODS 中待处理记录规范化入 DWD；每个父记录组一个事务。

    fail_at / fail_inside_at 仅用于回归：分别在第 N 组开始前与组内提交前模拟中断。
    """
    report = {'published': [], 'skipped_duplicate': [], 'skipped_stale': [], 'quarantined': []}
    sql = "SELECT * FROM ods_raw WHERE status='parsed'"
    params = []
    if only_batch is not None:
        sql += ' AND batch=?'
        params.append(only_batch)
    sql += ' ORDER BY ods_id'
    rows = connection.execute(sql, params).fetchall()
    for index, row in enumerate(rows):
        if fail_at is not None and index == fail_at:
            raise RuntimeError(f'模拟中断：第 {index} 组开始前')
        order_id = None
        try:
            with transaction(connection):
                record = _record_from_ods(row)
                if isinstance(record, dict) and isinstance(record.get('order_id'), str):
                    order_id = record['order_id']
                patch = build_patch(record)
                order_id = patch.order_id
                current = load_state(connection, patch.order_id)
                action = decide(current, patch)
                if action == 'publish':
                    state = merge_state(current, patch)
                    publish(connection, state, current, patch, row['batch'])
                    status = 'published'
                    report['published'].append(patch.order_id)
                else:
                    status = 'skipped_duplicate' if action == 'duplicate' else 'skipped_stale'
                    report[status].append(patch.order_id)
                if fail_inside_at is not None and index == fail_inside_at:
                    raise RuntimeError(f'模拟中断：第 {index} 组提交前')
                connection.execute('UPDATE ods_raw SET status=?, order_id=? WHERE ods_id=?',
                                   (status, patch.order_id, row['ods_id']))
        except QuarantineError as error:
            with transaction(connection):
                connection.execute("UPDATE ods_raw SET status='quarantined', order_id=? WHERE ods_id=?",
                                   (error.order_id or order_id, row['ods_id']))
                connection.execute(
                    'INSERT INTO quarantine (stage, ods_id, batch, order_id, byte_start, byte_end,'
                    ' reason_code, reason, raw_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    ('normalize', row['ods_id'], row['batch'], error.order_id or order_id,
                     row['byte_start'], row['byte_end'], error.code, error.reason, row['raw_text']))
            report['quarantined'].append({'ods_id': row['ods_id'], 'order_id': error.order_id or order_id,
                                          'code': error.code, 'reason': error.reason})
    return report


def reprocess(connection, qid, path):
    """重处理入口：修正包只允许包含原父记录，修正只影响原父。"""
    from . import ods
    row = connection.execute('SELECT * FROM quarantine WHERE qid=?', (qid,)).fetchone()
    if row is None:
        raise ValueError(f'隔离记录不存在: {qid}')
    if row['status'] != 'open':
        raise ValueError(f'隔离记录 {qid} 已处理，不能重复重处理')
    expected = row['order_id']
    ids = ods.read_payload_order_ids(path)
    if len(ids) > 1:
        raise ValueError(f'修正包包含多个父记录 {sorted(ids)}，违反只影响原父约束')
    if expected is not None and ids and ids != {expected}:
        raise ValueError(f'修正包父记录 {sorted(ids)} 与隔离记录父记录 {expected} 不一致')
    batch = f'reprocess-{qid}'
    ingest_report = ods.ingest_file(connection, path, batch)
    normalize_report = normalize(connection, only_batch=batch)
    resolved = False
    if expected is not None:
        resolved = connection.execute('SELECT 1 FROM dwd_order WHERE order_id=?', (expected,)).fetchone() is not None
    elif len(ids) == 1:
        resolved = connection.execute('SELECT 1 FROM dwd_order WHERE order_id=?', (next(iter(ids)),)).fetchone() is not None
    if resolved:
        connection.execute("UPDATE quarantine SET status='reprocessed' WHERE qid=?", (qid,))
    return {'qid': qid, 'resolved': bool(resolved), 'ingest': ingest_report, 'normalize': normalize_report}


def overview_nested(connection):
    """按订单汇总金额：数量与单价都是精确十进制文本，不产生兄弟数组笛卡尔积。"""
    totals = defaultdict(lambda: Decimal('0'))
    for row in connection.execute('SELECT order_id, quantity, price FROM dwd_order_line ORDER BY order_id, line_id'):
        value = Decimal(row['quantity']) * Decimal(row['price'])
        if not value.is_finite():
            raise ValueError('数量必须为有限十进制')
        totals[row['order_id']] += value
    for row in connection.execute('SELECT order_id FROM dwd_order ORDER BY order_id'):
        totals.setdefault(row['order_id'], Decimal('0'))
    return [{'order_id': key, 'amount': str(value)} for key, value in sorted(totals.items())]


def dump_lineage(connection):
    """导出三张 DWD 表的原始行（含版本血缘），用于中断重跑一致性比对。"""
    tables = {}
    for table in ('dwd_order', 'dwd_order_line', 'dwd_order_tag'):
        tables[table] = [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1, 2')]
    return tables


def orphan_count(connection):
    """孤儿子行计数：任何子行找不到父行都违反血缘约束。"""
    count = 0
    for table in ('dwd_order_line', 'dwd_order_tag'):
        count += connection.execute(
            f'SELECT COUNT(*) FROM {table} child LEFT JOIN dwd_order parent'
            ' ON child.order_id = parent.order_id WHERE parent.order_id IS NULL').fetchone()[0]
    return count
