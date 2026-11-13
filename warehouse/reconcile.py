"""重组与归一化比对：从 DWD 重组来源形态记录，与 ODS 重放的期望状态逐字段比对。

- expected：从 ods_raw 重放同一套裁决与合并逻辑得到的纯内存状态。
- actual：从 dwd_order / dwd_order_line / dwd_order_tag 重组的来源形态记录。
- 两侧都经 canonicalize 归一化（数组按业务标识排序、数字按十进制规范文本），
  因此数组重排不影响比对结果。
"""
from .dwd import (MISSING, QuarantineError, build_patch, canonical_state_view, canonicalize,
                  decide, merge_state)
from .ods import JsonDiag, parse_json_strict


def replay_expected(connection):
    """从 ODS 重放期望状态与每条的裁决结果（不依赖 DWD 内容）。"""
    states = {}
    outcomes = {}
    for row in connection.execute('SELECT * FROM ods_raw ORDER BY ods_id'):
        if row['payload'] is None:
            outcomes[row['ods_id']] = ('quarantined', None)
            continue
        try:
            record, _warnings = parse_json_strict(row['payload'])
            patch = build_patch(record)
            current = states.get(patch.order_id)
            action = decide(current, patch)
            if action == 'publish':
                states[patch.order_id] = merge_state(current, patch)
            outcomes[row['ods_id']] = (action, None)
        except QuarantineError as error:
            outcomes[row['ods_id']] = ('quarantined', error.code)
        except JsonDiag as error:
            outcomes[row['ods_id']] = ('quarantined', error.code)
    return states, outcomes


def rebuild(connection, order_id):
    """从 DWD 重组单个父记录的来源形态。"""
    row = connection.execute('SELECT * FROM dwd_order WHERE order_id=?', (order_id,)).fetchone()
    if row is None:
        return None
    state = {'version': row['version'],
             'scalars': {'buyer': row['buyer'], 'currency': row['currency'], 'note': row['note']},
             'lines_null': bool(row['lines_null']), 'tags_null': bool(row['tags_null']),
             'lines': {}, 'tags': set()}
    from decimal import Decimal
    for line in connection.execute('SELECT * FROM dwd_order_line WHERE order_id=? ORDER BY line_id', (order_id,)):
        state['lines'][line['line_id']] = {'sku': line['sku'], 'quantity': Decimal(line['quantity']),
                                           'price': Decimal(line['price'])}
    for tag in connection.execute('SELECT * FROM dwd_order_tag WHERE order_id=? ORDER BY tag', (order_id,)):
        state['tags'].add(tag['tag'])
    view = canonical_state_view(state)
    view['order_id'] = order_id
    return view


def rebuild_all(connection):
    return {row['order_id']: rebuild(connection, row['order_id'])
            for row in connection.execute('SELECT order_id FROM dwd_order ORDER BY order_id')}


def reconcile(connection):
    """全链路比对：ODS 重放期望 vs DWD 重组实际，附带隔离一致性检查。"""
    expected_states, outcomes = replay_expected(connection)
    expected = {}
    for order_id, state in expected_states.items():
        view = canonical_state_view(state)
        view['order_id'] = order_id
        expected[order_id] = view
    actual = rebuild_all(connection)
    diffs = []
    for order_id in sorted(set(expected) | set(actual)):
        if expected.get(order_id) != actual.get(order_id):
            diffs.append({'order_id': order_id,
                          'expected': expected.get(order_id), 'actual': actual.get(order_id)})
    quarantine_codes = {}
    for row in connection.execute('SELECT ods_id, reason_code FROM quarantine WHERE ods_id IS NOT NULL'):
        quarantine_codes[row['ods_id']] = row['reason_code']
    quarantine_mismatches = []
    for row in connection.execute('SELECT ods_id, payload FROM ods_raw ORDER BY ods_id'):
        ods_id = row['ods_id']
        action, code = outcomes[ods_id]
        if action == 'quarantined':
            if ods_id not in quarantine_codes:
                quarantine_mismatches.append({'ods_id': ods_id, 'detail': '重放判定应隔离但缺少隔离记录'})
            elif code is not None and quarantine_codes[ods_id] != code:
                quarantine_mismatches.append({'ods_id': ods_id,
                                              'detail': f'原因码不一致: 重放 {code} vs 实际 {quarantine_codes[ods_id]}'})
        elif ods_id in quarantine_codes:
            quarantine_mismatches.append({'ods_id': ods_id, 'detail': '重放判定可发布但存在隔离记录'})
    return {'orders': len(actual), 'matched': len(actual) - len([d for d in diffs]),
            'diffs': diffs, 'quarantine_mismatches': quarantine_mismatches}
