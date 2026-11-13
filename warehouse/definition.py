SCHEMA = {'purchase_lines': 'line TEXT PRIMARY KEY, order_id TEXT NOT NULL, sku TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL'}

# ODS -> DWD 规范化链路结构。
# ods_raw 保留每条来源记录的原始文本与字节位置；dwd_* 按 订单/行项目/标签 分别建模；
# quarantine 隔离坏记录并保留诊断与重处理入口。
CHAIN_SCHEMA = {
    'ods_raw': "ods_id INTEGER PRIMARY KEY, batch TEXT NOT NULL, seq INTEGER NOT NULL, "
               "kind TEXT NOT NULL, byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, "
               "raw_text TEXT NOT NULL, payload TEXT, order_id TEXT, "
               "warnings TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, UNIQUE(batch, seq)",
    'dwd_order': "order_id TEXT PRIMARY KEY, version INTEGER NOT NULL, buyer TEXT, currency TEXT, "
                 "note TEXT, lines_null INTEGER NOT NULL, tags_null INTEGER NOT NULL, "
                 "state_hash TEXT NOT NULL, updated_batch TEXT NOT NULL",
    'dwd_order_line': "order_id TEXT NOT NULL, line_id TEXT NOT NULL, version INTEGER NOT NULL, "
                      "sku TEXT, quantity TEXT, price TEXT, PRIMARY KEY (order_id, line_id), "
                      "FOREIGN KEY (order_id) REFERENCES dwd_order(order_id)",
    'dwd_order_tag': "order_id TEXT NOT NULL, tag TEXT NOT NULL, version INTEGER NOT NULL, "
                     "PRIMARY KEY (order_id, tag), "
                     "FOREIGN KEY (order_id) REFERENCES dwd_order(order_id)",
    'quarantine': "qid INTEGER PRIMARY KEY, stage TEXT NOT NULL, ods_id INTEGER, batch TEXT, "
                  "order_id TEXT, byte_start INTEGER, byte_end INTEGER, "
                  "reason_code TEXT NOT NULL, reason TEXT NOT NULL, "
                  "raw_text TEXT NOT NULL, raw_b64 TEXT, status TEXT NOT NULL DEFAULT 'open'",
}
