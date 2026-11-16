# 本地乐观关系事务（带谓词读取记录）

在原始只做写写检测的小仓库上，扩展为**可串行化的乐观事务**，并增加保存点。
所有结果由事务系统当场计算，不存在预写的调度答案。

## 文件与入口（保留并扩展原有可调用入口）

| 文件 | 职责 |
| --- | --- |
| `storage.py` | 多版本存储：保留每个 revision 的完整快照、`last_write`、提交日志 `commits`（事务标签+增量）。读路径无锁；锁只在 `publish` 的原子发布窗口内。`Store.begin()` 取当前快照版本。 |
| `transaction.py` | `Transaction`：`get/scan`（叠加私有 put/delete 并记录读取依据）、`put/delete`、`savepoint/rollback_to/release`、`commit/abort`。 |
| `validation.py` | `conflict`/`write_write_conflicts`（**只做写写检测**，保留原入口）与 `predicate_conflict`（提交点重验谓词/点读取）。 |
| `demo.py` | 真实演示：正常提交、write-skew 拦截及冲突证据、幻影、无关 team 并行、保存点回滚。 |
| `revision_probe.py` | 用正式保存点 API 复跑 `savepoint-cases.json` 两个关键交错。 |
| `harness.py` | 受控调度执行器 + **串行顺序枚举核对器** + 无半批/终态幂等核对。 |
| `verify.py` | 总验收：结构化用例、随机压力（固定种子可复跑）、真实线程交错、规模上限。 |
| `schedules.json` / `savepoint-cases.json` | 机器可跑的结构化调度（同时保留原有散文用例）。 |

```bash
python3 demo.py            # 场景演示（含冲突证据 JSON）
python3 revision_probe.py  # 保存点交错
python3 verify.py [seed]   # 全量验收，默认种子 20261116
```

## 选定的串行验证顺序

**按实际 `commit` 先后，把每个成功事务视为在其提交瞬间紧前执行**
（first-committer-wins 的串行证书）。因此提交时必须同时证明：

1. **写写**：本事务写过的键，自 `begin` 起没有被更早提交的事务改过
   （`last_write[key] > start` 即冲突）。
2. **谓词/点读重验**：每一次**已返回给调用方**的 `get/scan`，用
   “提交点已提交库 + 本事务当时覆盖的私有版本”重放，结果必须与返回值逐行一致。

重验覆盖：空范围被插入（幻影）、行改 team 迁入/迁出谓词、已返回行的
`on_call` 改变、点读缺失键被插入、点读行被删除/改值。**另一 team 范围外
的修改不进入候选键，不会让本事务失败**；无读取交叠时全局版本再怎么涨也
不拒绝（见 `global_revision_does_not_block_unrelated_tx`）。

## 读取依据（provenance）与保守冲突边界

每条读记录标注每个相关键的来源：

* `snapshot`：来自 begin 快照——只有这类观察会与外部已提交变化比较；
* `private`：被本事务私有 put/delete 覆盖，记录私有版本号 `wv`——只与该
  不可变私有版本比较，**撤销后的私有值绝不与外部当前值比较**，因此保存点
  回滚不会制造假冲突。

`scan` 对团队闭区间内的存在、缺失、值全部留痕，包括：被 `on_call` 条件
排除的键、私有删除墓碑、私有移出区间的键（它们不进结果但必须记录来源，
否则会被误当成外部行而漏检）。若谓词结果变化却无法归因到单个键，验证器
按**保守中止**处理（安全网），绝不静默放行；但范围外修改永不计冲突。

## 保存点语义

* 最多 4 个嵌套保存点；名称在活跃保存点中唯一；未知名称/重复活跃名称/
  超限返回 `INVALID_SAVEPOINT` 且不改变事务状态。
* `rollback_to` 恢复该点私有写、移除其后保存点，目标点保留且可再次回滚；
  `release` 移除该点及内层保存点但保留全部私有写。
* **回滚不撤回已返回的读取观察**：`reads` 不清除，全部继续参与提交验证。
* 私有版本号全局单调递增、版本内容永不剪枝；读记录通过版本号保留“当时由
  自己的哪个私有版本覆盖”的依据。保存点不改变 begin 快照，也不向其他事务
  发布任何数据。

## 终态与原子性

* 终态（`COMMITTED/CONFLICT/ABORTED`）后重复 `commit/abort` 返回第一次
  结果；重复 `get/scan` 返回冻结视图且不新增读记录。
* `commit` 要么整批进入一个新 revision，要么（无写）不产生 revision；
  `CONFLICT/abort` 不调用 `publish`，源保持不变，无半批。
* 只读事务也必须显式 `commit` 或 `abort` 结束。
* `begin/get/scan` 不持有任何跨事务互斥锁；`publish` 内部锁仅保护单次原子
  发布，支持多线程先各自扫描再依次提交（`verify.py` 线程用例）。

## 验收核对方法

`harness.enumerate_serial_orders` 对**成功事务的所有排列**做枚举：在每个
排列中，按各读记录的私有写水位重放它实际返回的 `get/scan`，再应用其写；
至少存在一个排列能复现全部读取，且最终表等于 seed 叠加所有且仅成功事务
的写。`check_no_half_batch` 逐 revision 重放历史，确认每个增量恰为一个
事务的完整写集，冲突/中止事务的写不出现在任何快照中。

约束：同时活跃事务 ≤ 4、单次调度操作 ≤ 200（保存点操作计入）、
行数 ≤ 1000、`team ∈ [-100,100]`。
