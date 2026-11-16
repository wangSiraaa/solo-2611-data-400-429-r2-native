# 415：基于证据的 join 消除（目录资格 + 受控写入 + 真实查询改写）

关系是有限 JSON 行 bag（每行独立 `row_id`，业务连接列允许 NULL 和重复）。
查询是 typed JSON 的 scan 与最多两层 INNER/LEFT 等值 join，输出仅指定源表的列、
保留重复出现；不解析 SQL，等值连接中 NULL 不匹配。

## 核心命题

“目录里声明了外键”只是待验证主张，**不**等于旧数据已校验，更不保证外键列无 NULL。
能否消除一个父表列不被输出的 join，由三类相互独立的证据共同决定：

| 前提 | 证据 | 为什么需要 |
| --- | --- | --- |
| 引用存在性 | `FOREIGN_KEY(child.k -> parent.k)`（SQL 式，允许子侧 NULL） | INNER 会删除孤儿子行 |
| NULL 处理 | `NOT_NULL(child.k)`（FK 验证允许 NULL，不能替代它） | 等值连接 NULL 不匹配，INNER 会删除 NULL 子行 |
| 至多一个匹配 | `UNIQUE(parent.k)` | 父键重复会让每个子行被放大成多行 |

* **INNER 消除**：三件证据必须在**同一源状态版本**下全部 TRUSTED；
  运行时守卫路径则现场核对“无 NULL 子键、每个非 NULL 子键恰有一个父匹配、父键无重复”。
* **LEFT 消除**：只需 `UNIQUE(parent.k)`。LEFT 无匹配本就保留左行，
  NULL 子键与无匹配子行都不会被丢弃，因此即使没有外键也可消除。
* 证据不足时**保留真实 join 是允许的安全路径**；任何涉及被消除父侧过滤的 join 一律保留。

## 模块

| 文件 | 职责 |
| --- | --- |
| `catalog.py` | `declare/validate/status`；登记 UNIQUE/FOREIGN_KEY/NOT_NULL，保存状态、覆盖范围、违反见证、计数与所验证 `state_version`；受控写入后的资格延展与 unchecked 降级 |
| `database.py` | 行 bag、版本号、访问序号；受控 `apply()` 批次（暂存全表校验，冲突整体拒绝）与显式 `unchecked_import()`（数据可查询前原子降级相关资格）；`CostLedger` 分桶记账 |
| `planner.py` | `prepare()` 只规范化逻辑计划并给出标注 `non_binding` 的静态建议；**不产生永久“可删 join”结论** |
| `executor.py` | `execute()` 固定当前源版本，按可信目录或运行时守卫重新裁决；产出实际算子计划、访问轨迹与决策轨迹；守卫失败自动 fallback 朴素 join |
| `oracle.py` | 独立朴素嵌套循环 join 小输入 oracle，输出按来源出现身份排序、不去重 |
| `benchmark.py` | clean 基准端到端驱动，输出资格记录、实际算子计划、访问轨迹与分桶成本 |
| `demo.py` | 完整父子关系正常路径 + 孤儿子行反例（盲删 INNER 多返回一行） |
| `test_semantics.py` | 20 个端到端用例（父重复、FK 列 NULL、LEFT 无匹配、未验证声明、验证失败、非法写入拒绝、unchecked 导入后下次 execute、两层 join、过滤、身份排序） |
| `test_property.py` | 300 个随机脏世界 × 4 查询 × 4 策略，结果必须始终等于 oracle |

## 两种消除路径

1. **可信目录**：`validate` 读真实关系落证；之后正常写路径 `apply()` 在暂存全表上
   重跑所有 TRUSTED 约束（冲突整体拒绝、版本不动），通过后资格延展到新版本。
2. **运行时守卫**：不需要目录，execute 时读取当前父表与左子树键集合，
   显式统计 NULL 数与每个键的匹配数；守卫读取计入 `guard_rows`，
   与查询算子的 `query_rows` 分开；失败即 fallback 并把父表计入真实访问轨迹。

## 成本不藏进“元数据”

`CostLedger` 独立分桶：`validation_reads`（先前验证）、`guard_rows`（守卫读取）、
`maintenance_reads`（受控写入维护）、`query_rows`（查询算子真实业务行读取）、
`rejected_batches`、`applied_batches`、`unchecked_imports`。
被消除的父表不会出现在 `query_rows` / 访问轨迹中。

## 运行

```bash
python3 demo.py
python3 benchmark.py
python3 benchmark.py --json benchmark-report.json
python3 -m unittest test_semantics test_property -v
```

基准结果（100 父 / 1000 子）：INNER 与 LEFT 的实际算子均为 `eliminated_join`，
查询阶段父表业务行读取为 0；验证、守卫与维护成本在报告中分桶可见。
