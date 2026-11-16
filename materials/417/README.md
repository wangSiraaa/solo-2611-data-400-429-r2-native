# 可更新语料上的精确 BM25 排名与有依据块剪枝

本目录在原有静态实现（建库时冻结数值上界、查询时全扫语料重算统计）的基础上，
实现了一个**可更新、可持久化、可配置、剪枝可证明**的 BM25 引擎。核心修复是把
“会随语料/参数过期的数值上界”替换为“与 idf/avgdl/k1/b 无关的 tf/长度包络”，
数值上界只在每次查询时按**当前 revision 与本次参数**即时导出。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `scorer.py` | 原 BM25 公式（默认 k1=1.2,b=0.75，逐字节保持原行为）+ 参数化 kernel `score_p`/`contribution_p`、`idf`、参数校验 |
| `envelopes.py` | 参数无关块包络 `(tf_max, len_min)` 的构建，以及查询时安全数值上界导出与适用性依据 |
| `store.py` | 原始 tokens 载荷存储与**原始文档访问计量**；载荷与元数据分离、可持久化 |
| `inverted.py` | 可更新索引：增量 N/df/总长度、posting、固定块、块包络、revision、审计历史、save/open |
| `ranker.py` | 查询：固定索引状态 → 按当前统计+本次参数导出每块上界 → 严格不等式跳块 → 元数据预筛 → 仅命中文档读载荷评分 |
| `bounds.py` | 固定 (revision,k1,b) 快照数值界、带适用性检查的导出、缓存失效异常 `StaleBoundError` |
| `oracle.py` | 独立逐活跃文档全评分 oracle：自读全部载荷、独立重算统计、不用任何块信息 |
| `inspect.py` | 不依赖先跑 query 的统计/包络适用性检查、块摘要、更新与查询历史、计量账目 |
| `legacy.py` | 仅用于演示的“旧冻结上界”引擎，复现删除后误剪枝反例，不参与真实查询路径 |
| `cli.py` | build/put/remove/query/inspect 命令行，索引落盘可重读 |
| `demo.py` | 静态一致 → 删除 20 篇 beta → B 真实分超 A；旧引擎误剪 vs 修复引擎正确 |
| `raw_access_probe.py` | 真实载荷访问计数：修复引擎 5 次 vs 旧查询内全扫 205 次 |
| `feasibility_probe.py` / `prepared_benchmark_probe.py` | 公开 100 文档基准的剪枝与访问预算（更新前后） |
| `revision_probe.py` | profile-cases 两套配置各自 oracle 核对、切换后仍剪枝且达预算 |
| `test_engine.py` | 2000+ 项随机与定向校验（见下） |

## 剪枝为什么始终成立

单文档单项贡献：

```
C(idf,tf,L) = idf · tf·(k1+1) / (tf + k1·(1-b+b·L/avgdl))
```

- 对固定 idf/k1/b/avgdl，分数部分关于 `tf` 单调递增（导数为正）；
- 当 b>0 时分母随文档长度 `L` 增大，分数关于 `L` 单调递减；b=0 时长度完全消失；
- idf 对任意 0≤df≤N 都非负（对数参数 ≥1）。

因此块内取 **tf 的最大值 tf_max** 与 **含该词文档的最小长度 len_min**（二者可来自
不同文档，只会让界更松），代入公式得到的值 ≥ 块内任一真实文档的该项贡献。多词
查询的文档分是各项之和，故块上界取各项上界之和，仍支配块内所有文档。包络只含
tf 与长度，**不含 idf/avgdl/k1/b**，所以统计或参数变化不会让它失效；只有
tf/长度/活跃身份变化才需要重建，而那正发生在 build/put/remove 之中。

跳块判定用**严格小于**当前第 k 名分数；相等绝不跳（同分且 doc_id 字节序更小者
必须保留反超机会）。每次跳块都在审计里记录：块号、各项上界、块上界、阈值、
revision、N、avgdl、df、k1、b 及文字证明。另加 `1e-12` 单向向上舍入余量。

## 统计更新与适用性依据

- `put`/`remove` 按每文档 tf 表与长度**增量**维护 N、每词 df、总长度；空文档计入
  N 与平均长度但不产生任何 term。每次变更 `revision += 1`，只重建受影响块的包络
  （元数据操作，不读载荷）。
- `current_stats()` 纯由计数器得到 avgdl，查询内**不扫描任何载荷**。
- `inspect` 直接给出 `derived_under_revision == live_revision` 的 `current` 标志，
  无需先跑一次 query。
- 数值上界每次查询现场导出，basis 同时携带 revision 与 k1/b；旧 idf/旧 avgdl 生成
  的数字不会因为“它曾经是上界”而被继续使用。

## 访问计量口径

- **原始文档访问** = 读取一份完整 tokens 载荷；重复读取、内存缓存命中都重复计数。
  查询窗口从进入 `query` 起算，重建/更新发生在此前的单独阶段，不计入。
- 计数器、df、posting 中的 tf/长度、块包络属于**元数据**，独立计量
  （`metadata_accesses`），且元数据不编码完整 tokens。
- oracle 全扫在独立 `oracle` phase 计量，与 query 账目分开。

公开 100 文档基准（block_size=5，alpha top-1）：实际跳过 19 个非空块，
`scored=5`、查询内原始文档访问 `5`，均 < 50；旧的查询内统计全扫为 205 次。

## 运行方式

```bash
python3 demo.py
python3 raw_access_probe.py
python3 feasibility_probe.py
python3 prepared_benchmark_probe.py
python3 revision_probe.py
python3 test_engine.py

python3 cli.py build --store ./idx --block-size 5 --docs docs.json
python3 cli.py put    --store ./idx --doc D1 --tokens '["alpha","beta"]'
python3 cli.py remove --store ./idx --doc D1
python3 cli.py query  --store ./idx --terms alpha beta --k 3 --k1 2 --b 0 --evidence
python3 cli.py inspect --store ./idx --terms alpha
```

## 校验覆盖（test_engine.py）

删除常见词文档、长文变短、短文变长、同 ID 词项替换（posting/df 同步）、删除重插、
全空语料（avgdl=0 不除零）、空文档计入 N/avgdl、k 大于命中数、无命中词、完全同分
按 UTF-8 字节序；随机多步更新序列上对默认与多组合法 (k1,b) 逐次比对独立 oracle 的
排名与分数，并验证**每一个块的导出上界都真实支配块内所有文档**；持久化重开结果
一致；非法/非有限/越界参数拒绝且不改索引；公开基准与双配置预算。
