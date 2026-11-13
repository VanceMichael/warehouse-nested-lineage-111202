"""ODS -> DWD 嵌套链路演示：带换行 CSV、金额精度、数组重排、部分补丁、子行撤销、
坏记录诊断与重处理、幂等重投、重组比对。全部使用虚构订单，失败返回非零退出码。"""
import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NESTED = ROOT / 'examples' / 'nested'


def execute(*arguments, expect_failure=False):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if expect_failure:
        if result.returncode == 0:
            raise RuntimeError('命令本应失败却成功: ' + ' '.join(command))
        print(result.stderr.strip().splitlines()[-1], flush=True)
        return None
    if result.returncode != 0:
        print(result.stdout, result.stderr, flush=True)
        raise RuntimeError('命令失败: ' + ' '.join(command))
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def check(condition, message):
    if not condition:
        raise RuntimeError('校验失败: ' + message)
    print('✓ ' + message, flush=True)


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'nested.sqlite3')

        print('== 1. 接入：CSV 内嵌 JSON（含引号内换行）与 JSONL ==', flush=True)
        csv_report = execute('ingest', '--db', database, '--input', str(NESTED / 'orders.csv'), '--batch', 'csv-1')
        check(csv_report['records'] == 3, '三条 CSV 记录全部接入（o1001 载荷跨物理行）')
        risk = [w for w in csv_report['warnings'] if w['code'] == 'PRECISION_RISK']
        check(len(risk) == 1 and risk[0]['path'] == '$.lines[0].price'
              and risk[0]['value'] == '19.989999999999998', '精度风险被诊断并精确保留原文数字')
        execute('ingest', '--db', database, '--input', str(NESTED / 'patches.jsonl'), '--batch', 'jsonl-1')
        execute('ingest', '--db', database, '--input', str(NESTED / 'redelivery.jsonl'), '--batch', 'jsonl-2')

        print('== 2. 坏记录：引号 / 语法 / 错号 / 编码 / 非法常量，保留字节位置与原文 ==', flush=True)
        bad_csv = execute('ingest', '--db', database, '--input', str(NESTED / 'bad.csv'), '--batch', 'bad-1')
        check([q['code'] for q in bad_csv['quarantined']] == ['CSV_QUOTING', 'JSON_SYNTAX', 'ID_MISMATCH'],
              'CSV 引号错误、JSON 语法错误、列与载荷错号均被隔离且不影响同包其他记录')
        bad_jsonl = execute('ingest', '--db', database, '--input', str(NESTED / 'bad.jsonl'), '--batch', 'bad-2')
        check([q['code'] for q in bad_jsonl['quarantined']] == ['ENCODING', 'JSON_CONSTANT'],
              '编码错误定位到字节、NaN 被拒绝')

        repeated = execute('ingest', '--db', database, '--input', str(NESTED / 'orders.csv'), '--batch', 'csv-1')
        check(repeated['changed'] is False, '同一来源包重投幂等，不重复写入')

        print('== 3. 规范化：部分补丁、子行撤销、数组重排 ==', flush=True)
        first = execute('normalize', '--db', database)
        check(len(first['published']) == 11, '11 条记录发布（含坏包中两条完好记录）')
        check(first['skipped_duplicate'] == ['o1001'], 'o1001 v3 重投（数组重排）判定为幂等重复')
        check(first['skipped_stale'] == ['o1002'], 'o1002 旧版本补丁判定为过期跳过')
        check(sorted(q['code'] for q in first['quarantined']) == ['DUPLICATE_CHILD_KEY', 'MISSING_CHILD_KEY'],
              '子标识缺失与重复的父记录被整组隔离')
        second = execute('normalize', '--db', database)
        check(all(not second[key] for key in second), '重跑规范化无任何变化')

        print('== 4. 金额精度与三态语义 ==', flush=True)
        amounts = {row['order_id']: row['amount'] for row in execute('inspect-nested', '--db', database)}
        check(amounts['o1001'] == '0.40', 'o1001 金额 0.40（2×0.10 + 4×0.05，无兄弟数组笛卡尔积）')
        check(amounts['o1002'] == '59.969999999999994', 'o1002 金额保持 17 位十进制精确值')
        rebuilt_o1003 = execute('rebuild', '--db', database, '--order', 'o1003')
        check(rebuilt_o1003['lines'] == [] and rebuilt_o1003['tags'] is None,
              '空数组是有效空集合（lines=[]），null 是清除（tags=null），二者不混同')
        rebuilt_o1002 = execute('rebuild', '--db', database, '--order', 'o1002')
        check(rebuilt_o1002['buyer'] == '青崖物流华东仓（虚构）' and rebuilt_o1002['currency'] == 'CNY'
              and rebuilt_o1002['note'] is None and rebuilt_o1002['tags'] == ['fragile'],
              '部分补丁只改 buyer，null 清除 note，未触及字段原样保留')
        rebuilt_o1001 = execute('rebuild', '--db', database, '--order', 'o1001')
        check('\n' in rebuilt_o1001['note'], 'CSV 引号内换行与 JSON 换行转义都完整还原')
        check([line['line_id'] for line in rebuilt_o1001['lines']] == ['l1', 'l3'],
              '子行撤销：l2 已删除、l3 已新增，与父更新同一事务发布')

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        lineage = {row['line_id']: row['version'] for row in connection.execute(
            'SELECT line_id, version FROM dwd_order_line WHERE order_id=?', ('o1001',))}
        check(lineage == {'l1': 1, 'l3': 3}, '血缘稳定：l1 内容未变保留引入版本 1，l3 随版本 3 引入')
        orphans = sum(connection.execute(
            f'SELECT COUNT(*) FROM {table} c LEFT JOIN dwd_order p ON c.order_id = p.order_id'
            ' WHERE p.order_id IS NULL').fetchone()[0]
            for table in ('dwd_order_line', 'dwd_order_tag'))
        check(orphans == 0, '无孤儿子行')

        print('== 5. 隔离诊断不丢原文 ==', flush=True)
        rows = {row['reason_code']: row for row in connection.execute('SELECT * FROM quarantine')}
        csv_bytes = (NESTED / 'bad.csv').read_bytes()
        quoting = rows['CSV_QUOTING']
        check(csv_bytes[quoting['byte_start']:quoting['byte_end']].decode('utf-8') == quoting['raw_text'],
              'CSV 引号错误记录的字节区间与原文完全一致')
        jsonl_bytes = (NESTED / 'bad.jsonl').read_bytes()
        encoding = rows['ENCODING']
        check(base64.b64decode(encoding['raw_b64']) == jsonl_bytes[encoding['byte_start']:encoding['byte_end']]
              and 0xFF in base64.b64decode(encoding['raw_b64']),
              '编码错误记录原始字节按 base64 完整保留')
        connection.close()

        print('== 6. 重组归一化比对与重处理 ==', flush=True)
        report = execute('reconcile', '--db', database)
        check(report['diffs'] == [] and report['quarantine_mismatches'] == [],
              f"ODS 重放与 DWD 重组比对一致（{report['orders']} 个订单）")
        before = set(amounts)
        qid = rows['MISSING_CHILD_KEY']['qid']
        resolved = execute('reprocess', '--db', database, '--qid', str(qid),
                           '--input', str(NESTED / 'bad_fixed.jsonl'))
        check(resolved['resolved'] is True and resolved['normalize']['published'] == ['o3001'],
              '修正后的 o3001 经重处理入口重新入仓')
        after = {row['order_id'] for row in execute('inspect-nested', '--db', database)}
        check(after - before == {'o3001'}, '修正只影响原父，其他订单状态不变')
        final = execute('reconcile', '--db', database)
        check(final['diffs'] == [], '重处理后全链路比对仍然一致')
        execute('reprocess', '--db', database, '--qid', str(qid),
                '--input', str(NESTED / 'bad_fixed.jsonl'), expect_failure=True)
        check(True, '已处理的隔离记录拒绝重复重处理')

        print('全部场景校验通过', flush=True)


if __name__ == '__main__':
    main()
