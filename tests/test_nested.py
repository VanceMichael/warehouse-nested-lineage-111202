"""ODS -> DWD 嵌套链路回归：诊断、血缘、三态、幂等、中断重跑、重组比对。"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from warehouse.catalog import connect
from warehouse.dwd import (dump_lineage, normalize, orphan_count, reprocess)
from warehouse.ods import ingest_file
from warehouse.reconcile import rebuild, reconcile

NESTED = Path(__file__).resolve().parents[1] / 'examples' / 'nested'


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / 'chain.sqlite3'
        self.connection = connect(self.db)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def ingest(self, name, batch):
        return ingest_file(self.connection, NESTED / name, batch)

    def ingest_text(self, text, batch, suffix='.jsonl'):
        path = Path(self.temporary.name) / (batch + suffix)
        path.write_text(text, encoding='utf-8')
        return ingest_file(self.connection, path, batch)

    def full_flow(self):
        self.ingest('orders.csv', 'csv-1')
        self.ingest('patches.jsonl', 'jsonl-1')
        self.ingest('redelivery.jsonl', 'jsonl-2')
        self.ingest('bad.csv', 'bad-1')
        self.ingest('bad.jsonl', 'bad-2')
        return normalize(self.connection)

    def ods_row(self, batch, seq):
        return self.connection.execute(
            'SELECT * FROM ods_raw WHERE batch=? AND seq=?', (batch, seq)).fetchone()

    def quarantine_of(self, code):
        return self.connection.execute(
            'SELECT * FROM quarantine WHERE reason_code=?', (code,)).fetchone()

    # --- 接入诊断 ---

    def test_csv_multiline_record_and_byte_offsets(self):
        report = self.ingest('orders.csv', 'csv-1')
        self.assertEqual(report['records'], 3)
        data = (NESTED / 'orders.csv').read_bytes()
        multiline = self.ods_row('csv-1', 1)
        self.assertEqual(data[multiline['byte_start']:multiline['byte_end']].decode('utf-8'),
                         multiline['raw_text'])
        self.assertIn('\n', multiline['payload'])
        normalize(self.connection)
        self.assertIn('\n', rebuild(self.connection, 'o1001')['note'])

    def test_precision_warning_and_exact_amount(self):
        report = self.ingest('orders.csv', 'csv-1')
        risk = [w for w in report['warnings'] if w['code'] == 'PRECISION_RISK']
        self.assertEqual(len(risk), 1)
        self.assertEqual(risk[0]['path'], '$.lines[0].price')
        self.assertEqual(risk[0]['value'], '19.989999999999998')
        normalize(self.connection)
        price = self.connection.execute(
            "SELECT price FROM dwd_order_line WHERE order_id='o1002'").fetchone()[0]
        self.assertEqual(price, '19.989999999999998')
        amount = self.connection.execute(
            "SELECT quantity, price FROM dwd_order_line WHERE order_id='o1002'").fetchone()
        from decimal import Decimal
        self.assertEqual(str(Decimal(amount[0]) * Decimal(amount[1])), '59.969999999999994')

    def test_csv_quoting_quarantine_and_resync(self):
        report = self.ingest('bad.csv', 'bad-1')
        codes = [q['code'] for q in report['quarantined']]
        self.assertEqual(codes, ['CSV_QUOTING', 'JSON_SYNTAX', 'ID_MISMATCH'])
        row = self.quarantine_of('CSV_QUOTING')
        data = (NESTED / 'bad.csv').read_bytes()
        self.assertEqual(data[row['byte_start']:row['byte_end']].decode('utf-8'), row['raw_text'])
        normalize(self.connection)
        self.assertIsNotNone(rebuild(self.connection, 'o2003'))

    def test_json_syntax_diagnostic_keeps_position(self):
        self.ingest('bad.csv', 'bad-1')
        row = self.quarantine_of('JSON_SYNTAX')
        self.assertIn('第1行第34列', row['reason'])
        data = (NESTED / 'bad.csv').read_bytes()
        self.assertEqual(data[row['byte_start']:row['byte_end']].decode('utf-8'), row['raw_text'])

    def test_encoding_error_quarantine_keeps_raw_bytes(self):
        report = self.ingest('bad.jsonl', 'bad-2')
        self.assertEqual([q['code'] for q in report['quarantined']], ['ENCODING', 'JSON_CONSTANT'])
        row = self.quarantine_of('ENCODING')
        data = (NESTED / 'bad.jsonl').read_bytes()
        slice_bytes = data[row['byte_start']:row['byte_end']]
        self.assertIn('字节 467 处', row['reason'])
        self.assertEqual(base64.b64decode(row['raw_b64']), slice_bytes)
        self.assertIn(0xFF, slice_bytes)
        json.dumps(row['raw_text'])  # 诊断文本必须可序列化
        normalize(self.connection)
        self.assertEqual(orphan_count(self.connection), 0)

    def test_json_constant_rejected(self):
        self.ingest('bad.jsonl', 'bad-2')
        row = self.quarantine_of('JSON_CONSTANT')
        self.assertIn('NaN', row['reason'])

    def test_id_mismatch_quarantine(self):
        self.ingest('bad.csv', 'bad-1')
        row = self.quarantine_of('ID_MISMATCH')
        self.assertIn('o2005', row['reason'])
        self.assertIn('o9999', row['reason'])

    def test_duplicate_json_key_quarantine(self):
        self.ingest_text('{"order_id": "o9", "order_id": "o9", "version": 1}\n', 'dup-key')
        self.assertIsNotNone(self.quarantine_of('JSON_DUPLICATE_KEY'))

    def test_same_batch_redelivery_idempotent_and_conflict_rejected(self):
        self.assertTrue(self.ingest('orders.csv', 'csv-1')['changed'])
        self.assertFalse(self.ingest('orders.csv', 'csv-1')['changed'])
        with self.assertRaises(ValueError):
            self.ingest('patches.jsonl', 'csv-1')

    # --- 血缘与整组隔离 ---

    def test_missing_child_key_quarantines_whole_group(self):
        self.ingest('bad.jsonl', 'bad-2')
        report = normalize(self.connection)
        self.assertIn('MISSING_CHILD_KEY', [q['code'] for q in report['quarantined']])
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM dwd_order WHERE order_id='o3001'").fetchone())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM dwd_order_line WHERE order_id='o3001'").fetchone()[0], 0)

    def test_duplicate_child_key_quarantines_whole_group(self):
        self.ingest('bad.jsonl', 'bad-2')
        normalize(self.connection)
        self.assertIsNotNone(self.quarantine_of('DUPLICATE_CHILD_KEY'))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM dwd_order WHERE order_id='o3002'").fetchone())

    def test_initial_record_requires_collections(self):
        self.ingest_text('{"order_id": "o8", "version": 1, "buyer": "甲"}\n', 'initial')
        report = normalize(self.connection)
        self.assertEqual([q['code'] for q in report['quarantined']], ['MISSING_FIELD'])
        self.assertIsNone(rebuild(self.connection, 'o8'))

    def test_array_reorder_keeps_identity(self):
        self.ingest('orders.csv', 'csv-1')
        self.ingest('patches.jsonl', 'jsonl-1')
        normalize(self.connection)
        before = dump_lineage(self.connection)
        self.ingest('redelivery.jsonl', 'jsonl-2')
        report = normalize(self.connection)
        self.assertEqual(report['skipped_duplicate'], ['o1001'])
        self.assertEqual(report['skipped_stale'], ['o1002'])
        self.assertEqual(dump_lineage(self.connection), before)
        lineage = {row['line_id']: row['version'] for row in self.connection.execute(
            "SELECT line_id, version FROM dwd_order_line WHERE order_id='o1001'")}
        self.assertEqual(lineage, {'l1': 1, 'l3': 3})

    def test_version_conflict_quarantine(self):
        self.ingest('orders.csv', 'csv-1')
        normalize(self.connection)
        self.ingest_text('{"order_id": "o1002", "version": 1, "buyer": "篡改", "currency": "CNY",'
                         ' "note": null, "tags": [], "lines": []}\n', 'conflict')
        report = normalize(self.connection)
        self.assertEqual([q['code'] for q in report['quarantined']], ['VERSION_CONFLICT'])
        self.assertEqual(rebuild(self.connection, 'o1002')['buyer'], '青崖物流（虚构）')

    # --- 三态语义 ---

    def test_partial_patch_preserves_untouched_fields(self):
        self.full_flow()
        view = rebuild(self.connection, 'o1002')
        self.assertEqual(view['buyer'], '青崖物流华东仓（虚构）')
        self.assertEqual(view['currency'], 'CNY')
        self.assertIsNone(view['note'])
        self.assertEqual(view['tags'], ['fragile'])
        self.assertEqual(len(view['lines']), 1)

    def test_empty_array_null_missing_distinct(self):
        self.full_flow()
        view = rebuild(self.connection, 'o1003')
        self.assertEqual(view['lines'], [])
        self.assertIsNone(view['tags'])
        row = self.connection.execute(
            "SELECT lines_null, tags_null FROM dwd_order WHERE order_id='o1003'").fetchone()
        self.assertEqual((row[0], row[1]), (0, 1))
        untouched = rebuild(self.connection, 'o1001')
        self.assertEqual(untouched['tags'], ['export', 'priority'])

    def test_child_revocation_atomic_with_parent(self):
        self.full_flow()
        view = rebuild(self.connection, 'o1001')
        self.assertEqual([line['line_id'] for line in view['lines']], ['l1', 'l3'])
        self.assertEqual(view['version'], 3)
        self.assertEqual(orphan_count(self.connection), 0)

    # --- 幂等与中断重跑 ---

    def test_normalize_rerun_is_noop(self):
        self.full_flow()
        report = normalize(self.connection)
        self.assertTrue(all(not value for value in report.values()))

    def test_crash_inside_group_then_rerun(self):
        self.ingest('orders.csv', 'csv-1')
        self.ingest('patches.jsonl', 'jsonl-1')
        clean = connect(Path(self.temporary.name) / 'clean.sqlite3')
        try:
            ingest_file(clean, NESTED / 'orders.csv', 'csv-1')
            ingest_file(clean, NESTED / 'patches.jsonl', 'jsonl-1')
            normalize(clean)
            expected = dump_lineage(clean)
        finally:
            clean.close()
        with self.assertRaises(RuntimeError):
            normalize(self.connection, fail_inside_at=3)
        pending = self.connection.execute(
            "SELECT COUNT(*) FROM ods_raw WHERE status='parsed'").fetchone()[0]
        self.assertGreater(pending, 0)
        normalize(self.connection)
        self.assertEqual(dump_lineage(self.connection), expected)
        self.assertEqual(orphan_count(self.connection), 0)
        # 未改字段没有被空值覆盖
        view = rebuild(self.connection, 'o1002')
        self.assertEqual(view['currency'], 'CNY')
        self.assertEqual(view['tags'], ['fragile'])
        self.assertEqual(len(view['lines']), 1)

    def test_crash_between_groups_then_rerun(self):
        self.ingest('orders.csv', 'csv-1')
        self.ingest('patches.jsonl', 'jsonl-1')
        with self.assertRaises(RuntimeError):
            normalize(self.connection, fail_at=2)
        normalize(self.connection)
        self.assertEqual(orphan_count(self.connection), 0)
        view = rebuild(self.connection, 'o1001')
        self.assertEqual(view['version'], 3)
        self.assertEqual([line['line_id'] for line in view['lines']], ['l1', 'l3'])
        report = normalize(self.connection)
        self.assertTrue(all(not value for value in report.values()))

    # --- 重组比对与重处理 ---

    def test_reconcile_matches_replay(self):
        self.full_flow()
        report = reconcile(self.connection)
        self.assertEqual(report['diffs'], [])
        self.assertEqual(report['quarantine_mismatches'], [])
        self.assertEqual(report['orders'], 5)

    def test_reprocess_only_affects_original_parent(self):
        self.full_flow()
        before = dump_lineage(self.connection)
        qid = self.quarantine_of('MISSING_CHILD_KEY')['qid']
        result = reprocess(self.connection, qid, NESTED / 'bad_fixed.jsonl')
        self.assertTrue(result['resolved'])
        after = dump_lineage(self.connection)
        self.assertEqual({row['order_id'] for row in after['dwd_order']}
                         - {row['order_id'] for row in before['dwd_order']}, {'o3001'})
        for table in ('dwd_order', 'dwd_order_line', 'dwd_order_tag'):
            untouched_before = [row for row in before[table] if row['order_id'] != 'o3001']
            untouched_after = [row for row in after[table] if row['order_id'] != 'o3001']
            self.assertEqual(untouched_before, untouched_after)
        self.assertEqual(self.quarantine_of('MISSING_CHILD_KEY')['status'], 'reprocessed')
        self.assertEqual(reconcile(self.connection)['diffs'], [])
        with self.assertRaises(ValueError):
            reprocess(self.connection, qid, NESTED / 'bad_fixed.jsonl')

    def test_reprocess_rejects_foreign_parent(self):
        self.full_flow()
        qid = self.quarantine_of('MISSING_CHILD_KEY')['qid']
        path = Path(self.temporary.name) / 'foreign.jsonl'
        path.write_text('{"order_id": "o9998", "version": 1, "buyer": "甲", "currency": "CNY",'
                        ' "note": null, "tags": [], "lines": []}\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            reprocess(self.connection, qid, path)
        self.assertIsNone(rebuild(self.connection, 'o9998'))


if __name__ == '__main__':
    unittest.main()
