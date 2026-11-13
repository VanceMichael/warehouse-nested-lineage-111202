# nested-lineage

读取平面采购行并计算订单金额，商品数量与行标识各自独立。

## 当前数据组织

- `purchase_lines`：`line TEXT PRIMARY KEY, order_id TEXT NOT NULL, sku TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL`

`examples/source.json` 为本地合成输入，`examples/expected.json` 是当前查询的公开样例结果。`schema.json` 描述结构，运行时定义在 `warehouse/definition.py`。业务计算位于 `warehouse/domain.py`，事务装载位于 `warehouse/catalog.py`，内容寻址 JSONL 对象位于 `warehouse/store.py`。

输入批次以内容 SHA-256 固定；相同批次重投不重复写入，相同批次内容不同则报错。装载以完整输入包为事务，主键冲突不留下半批记录。关系数据保存在 SQLite，精确数量用十进制文本；导出对象按内容哈希命名，读取会重新校验，来源不在运行时修改。

## ODS → DWD 嵌套规范化链路

采购接口的行项目（lines）与标签（tags）为嵌套数组。链路分两层，订单、行项目、标签分别建模，不产生兄弟数组笛卡尔积：

- `ods_raw`：每条来源记录的原始文本、字节区间 `[byte_start, byte_end)`、解析警告与处理状态。
- `dwd_order` / `dwd_order_line` / `dwd_order_tag`：父版本与子标识组成稳定血缘，子行携带引入版本。
- `quarantine`：坏记录的原因码、可读原因、字节位置、原文（编码错误时另存 base64），并提供重处理入口。

语义约定：

- 缺失字段表示补丁未触及；`null` 表示清除；空数组是有效空集合（`lines_null` / `tags_null` 区分清除与空集合，三者不混同）。
- 数组重排不改变身份：子行按业务标识（`line_id` / tag）归键，同版本重投按“补丁合并后的状态”判等，内容未变的子行保留原引入版本。
- 子标识缺失或重复时父记录整组隔离；首次入仓记录必须包含 `lines` 与 `tags`（可为 `null` 或空数组）。
- 每个父记录组一个事务：父更新与子行删除、新增一起发布，不存在新父配旧子；中断后重跑幂等，无孤儿子行，未触及字段不被空值覆盖。
- 诊断：CSV 引号错误按记录隔离并继续扫描；编码错误定位到绝对字节；JSON 数字按十进制解析，超过 15 位有效数字给出 `PRECISION_RISK` 警告并精确保留；`NaN`/`Infinity`、重复键、语法错误均拒绝。

`examples/nested/` 为虚构订单样例：`orders.csv`（内嵌 JSON 列，o1001 载荷跨物理行）、`patches.jsonl`（重排、部分补丁、撤销）、`redelivery.jsonl`（幂等重投）、`bad.csv` / `bad.jsonl`（坏记录）与 `bad_fixed.jsonl`（修正包）。

```bash
python3 -m warehouse ingest --db /tmp/n.sqlite3 --input examples/nested/orders.csv --batch csv-1
python3 -m warehouse normalize --db /tmp/n.sqlite3
python3 -m warehouse reconcile --db /tmp/n.sqlite3      # ODS 重放 vs DWD 重组，不一致时退出码非零
python3 -m warehouse rebuild --db /tmp/n.sqlite3 --order o1003
python3 -m warehouse quarantine --db /tmp/n.sqlite3
python3 -m warehouse reprocess --db /tmp/n.sqlite3 --qid 6 --input examples/nested/bad_fixed.jsonl
python3 -m warehouse inspect-nested --db /tmp/n.sqlite3
```

完整演示（带换行 CSV、金额精度、数组重排、部分补丁、子行撤销、诊断与重处理）：

```bash
python3 scripts/demo_nested.py
```

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。时区相关开发使用系统 IANA 时区库。数据库和对象目录均由入口参数指定。源码和合成数据可以直接复制到 Linux 环境运行。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

## 运行

每次使用全新临时目录执行真实导入、查询、导出和内容校验：

```bash
python3 scripts/demo.py
```

分步调用可用：

```bash
python3 -m warehouse import --db /tmp/nested-lineage.sqlite3 --input examples/source.json --batch first
python3 -m warehouse inspect --db /tmp/nested-lineage.sqlite3
python3 -m warehouse export --db /tmp/nested-lineage.sqlite3 --objects /tmp/nested-lineage-objects
```

`scripts/demo.py` 打印实际命令和完整输出，验证首次写入、重复导入、查询和导出往返。小规模样例在数秒内结束，失败返回非零退出码，不回放预制日志。手工重复使用数据库路径时应留意已存在的业务主键。
