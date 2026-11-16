# 范围删除与安全局部压缩

Python 3.11，无外部依赖。数据库只在指定/临时目录生成 JSON 文件。

## 模块

- `run_store.py`：不可变 run、manifest、seq 单调元数据、压缩 journal、16 run / 1000 记录约束与输入校验。
- `reader.py`：跨 run 全局可见性。扫描线 + 堆在实际点 key 上求覆盖 erase 的最大 seq，不枚举区间整数；`get` 走点查刺点路径。
- `compactor.py`：局部压缩。点版本按全库 winner/可见性淘汰；erase 仅在仍保护未选 run 中更旧点时整条保留，否则回收；全量压缩输出不含任何删除记录。
- `inspect.py` / `main.py inspect`：活动 run、保留的点与删除、读取代价计量、每次保留/回收的逐条依据。
- `DESIGN.md`：表示选择（原区间读时检查 vs 规范化覆盖片段）与重叠/更新/压缩对检查量、索引维护、片段膨胀的影响。

## 运行

```
python3.11 -B demo.py          # 题目要求的三个观察点
python3.11 -B verify.py        # 独立字典模型 + 固定种子随机历史全量核对
python3.11 -B cost_report.py   # 真实记录计数的代价计量
python3.11 -B main.py <dir> add|get|scan|compact|inspect ...
```

## 规则摘要

点 `put(key,value,seq)`、区间删除 `erase(lo,hi,seq)` 作用于 `lo <= key < hi`。
某 key 仅保留 seq 最大的 put，且其 seq 严格大于覆盖该 key 的所有 erase 时可见；
删除后的新 put 可以重新出现。seq 库内唯一且新导入必须严格大于全部历史 seq，
与文件名、导入顺序、段号无关；同一历史重新切段不改变任何 get/scan 结果。
