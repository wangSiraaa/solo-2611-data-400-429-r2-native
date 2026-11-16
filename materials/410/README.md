# 410：多版本行堆 + 历史覆盖索引的快照精确范围读取

## 问题回顾

旧实现的行堆是多版本的，但覆盖索引只保留每个 row_id 的*当前* category/amount：
分类迁移后，固定在迁移前的报表快照再按旧 category 范围查询会漏行；旧索引命中
也无法回答“该行在当时是否可见”。本实现让行堆与索引共享同一套提交/可见性协议，
在任意已产生快照上给出精确范围读取，并新增原子批量提交 `Engine.apply`。

## 数据结构

### 行堆 `heap.py`（append-only 版本链）

* 每次 put/delete 都追加一个不可变版本，`version_id` 单调递增且即版本身份；
  删除产生 `deleted=True` 墓碑版本。
* row_id 删除后重新出现得到**新的 version_id**，旧身份版本永不被新值覆盖。
* 快照 `s` 上某 row_id 的可见行 = 其 `commit <= s` 的版本中身份最新者；
  墓碑表示缺行（与 `amount=NULL` 是两回事）。
* `read_version(version_id)` 按版本身份回表；`full_scan_at` 仅保留给 oracle/demo
  对照，查询路径不使用。

### 二级索引 `cover_index.py`（按版本展开 + 提交区间）

每个行版本一条索引项，全局有序列表（`bisect`/`insort` 维护）：

```
key   = (category, row_id 的 UTF-8 字节, version_id)
value = (version_id, row_id, category, amount?, start_commit, end_commit)
```

* 新版本以 `[start_commit=commit, end_commit=+∞)` 开启区间；同一 row_id 的后继
  版本（put 或墓碑）发布时，把当前开放项的 `end_commit` 关闭为该 commit。
* 查询先用两次 `bisect_left` 把候选压缩到 `[lo, hi)` 的 category 键区间
  （真正经二级索引缩小候选，不读全量历史），再逐项核对
  `start_commit <= snapshot < end_commit`。每个 row_id 至多一项覆盖该快照，
  结果天然无重复，且已按 `(category, UTF-8 字节序, version_id)` 有序。
* **覆盖读取为何可直接返回**：索引项在发布时完整复制了该不可变版本的
  category/amount，提交区间又精确刻画它的可见范围；区间覆盖查询快照时，该项
  就是当时可见版本本身的值副本。
* **回表路径**（`Engine(covering=False)`）：索引不携带 amount，每个命中项严格按
  其 `version_id` 调 `Heap.read_version` 读对应身份，绝不按 row_id 取最新行，
  并附带堆/索引可见性一致性断言。
* 一次查询固定一个快照、只扫这一代索引列表，不混用两代索引或覆盖值。

### 引擎 `engine.py`

* `put` / `delete`：单项写入，成功即取得一个递增 commit。
* `snapshot()`：登记并返回当前已提交 commit；未登记或晚于当前 commit 的快照
  在 `query` 时被拒绝（`SNAPSHOT_NEVER_TAKEN` / `SNAPSHOT_AFTER_CURRENT_COMMIT`）。
* `apply(operations)`：≤100 项有序 put/delete。先在**私有暂存态**完成全部校验
  与折叠（同身份反复改动以最后状态为准；新身份同批先插后删合法且折叠为空），
  全过后行堆与索引才以**同一个 commit** 发布；非法类型、越界值、未知操作、
  删除私有状态中不存在的身份都会整批失败，行堆/索引/commit/已取得快照原样保留。
  非空成功批恰好一个 commit（无净变化也分配），空批不分配 commit。返回值是
  已登记、可立即查询的快照。
* 校验范围：category ∈ [-1000, 1000] 整数；amount 为 NULL 或有符号 64 位整数；
  row_id 为非空字符串；布尔值不被当作整数。

### 审计计数（`engine.audit`，每次 query 重置）

| 字段 | 含义 |
| --- | --- |
| `index_entries_examined` | 本次在 category 键区间内实际检查可见性的索引项数 |
| `index_entries_returned` | 区间覆盖查询快照、实际输出的索引项数 |
| `heap_versions_read` / `heap_fetches` | 按 version_id 回表读取的堆版本数（覆盖模式为 0） |
| `covering_reads` | 直接由覆盖索引项返回的行数（回表模式为 0） |

## 入口与运行

在本目录下执行：

```bash
python3 demo.py            # 正常查询 + 同一 row_id 旧/新快照的不同正确结果
python3 revision_probe.py  # batch-import-cases.json 的原子提交与失败不变性
python3 benchmark.py       # 240 互异分类小基准，断言堆版本读取数 < 24
python3 verify.py [seed]   # 独立逐版本 oracle 差分验证（默认种子 410，可重复）
```

### 小基准结果（benchmark.json 场景）

240 条互异分类装载后取快照，再把 r017 从 category 17 迁到 1000，在**旧快照**上
查询 `[17,18)`：索引键区间内只检查 **1 个索引项**，覆盖模式堆版本读取 **0**、
回表模式按身份读取 **1**，均远小于公开阈值 24；全量历史为 241 个堆版本、
241 条索引项，未被整表读出。该结论只约束此公开分布，不宣称任意重复分类都能少读。

### verify 的独立解释器

`verify.py` 中的 `IndependentOracle` 不引用引擎/索引代码，只消费同一份操作事件流
（含单项写入与成功批次的 commit 边界），逐版本重放每个快照的逻辑行表后独立排序、
过滤、投影。覆盖：分类迁移、仅覆盖值修改（含 NULL）、删除重插身份不覆盖、
同分类多行与 UTF-8 字节序、NULL 与缺行区分、快照合法性、固定快照后继续写、
apply 全部失败路径与原子性、空批/超长批，以及 500 身份 × 20 次替换/删除 ×
20 个快照的上限规模随机差分；覆盖与回表两种引擎模式在每个快照上逐结果比对。

## 存储与放大（如实统计）

* 索引按版本展开，存储量与存活+历史 put 版本同阶；墓碑不建索引项，只关闭区间。
* 读取放大 = 键区间内的历史索引项检查数（含已关闭项），由
  `index_entries_examined` 如实记录；回表次数等于命中数（仅回表模式）。
* 纯单进程内存实现，无磁盘掉电恢复、历史回收或通用 SQL；写放大无限制，
  每次发布为每个最终变化追加一个堆版本与至多一条索引项。
