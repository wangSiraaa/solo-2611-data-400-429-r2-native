# 关系视图编辑接口（open-orders view lens）

把运营拿到的「开放订单 + 客户名称」投影视图，以关系差分的方式写回
`customers` / `orders` 两张基础表，同时保留视图不表达的信息（关闭订单、
范围外订单、`private_note`、`internal_note`、无开放订单的客户）。

## 模块

| 文件 | 职责 |
| --- | --- |
| `view.py` | `read_view(db, scope=None)` 原投影入口（scope 可选）；`open_view(db, scope=None)` 返回 `(rows, credential)`，投影只在 SQLite 内执行，公开列固定为 `order_id/customer_id/customer_name/amount`，按 `order_id` 排序 |
| `credential.py` | 可保存的视图凭据：HMAC-SHA256 签名的不透明令牌，固化规范化 scope、可见身份与读取时原始公开值；令牌只是数据，任何部分都不会被当作 SQL |
| `writeback.py` | `put_view(db, desired, credential=None)`：单个真实 SQLite 事务内做凭据校验 + 新鲜度核对 + 关系差分（改名 / 改金额 / 改归属 / 删除 / 插入），失败整体回滚 |
| `scope_reader.py` | 保留 `read_scope(db, scope)`，直接委托 `read_view`，读范围、凭据校验与写差分共用同一套半开区间语义 `amount_lo <= amount < amount_hi` |

## 边界与差分语义

* **删除**：只有凭据快照里出现、而目标集合中消失的 `order_id` 才删除。
  关闭订单、范围外开放订单不在凭据内，永不因缺席而删除。
* **读后变化**（凭据存在时）：
  * 快照内订单的任一公开值（`customer_id/customer_name/amount`）变化或不再是
    open → `STALE_VIEW`；
  * 读后新出现且**落入该 scope** 的开放订单 → `STALE_VIEW`；
  * 范围外新增开放订单、范围外金额修改后仍在范围外、关闭订单的
    `internal_note` 修改 → 允许写回且原样保留。
  * 订单因并发改金额进入/离开 scope → `STALE_VIEW`。
* **共享客户改名**：同一 `customer_id` 在目标多行中的名字必须一致，否则
  `CONFLICT`（与遍历顺序无关）。改名作用于客户实体，范围外/关闭订单通过
  JOIN 显示新名字，但不触碰它们的 `amount/status/internal_note`，也不改
  `private_note`。
* **改归属**：引用已有客户保留其 `private_note`；新客户按 schema 默认
  `private_note=''` 建立；新订单按 schema 默认 `internal_note=''`、
  `status='open'` 建立。
* **隐藏 ID 占用**：目标里的新 `order_id` 若已被关闭订单或范围外开放订单
  占用 → `CONFLICT`，不会把关闭行改成 open，也不借用隐藏身份。
* **客户不做垃圾回收**：没有开放订单后客户行仍保留。
* **scope 不可扩权**：`put_view` 不接受 scope 参数，编辑范围只来自凭据；
  目标行金额超出 `[lo, hi)` → `OUT_OF_SCOPE`，整笔不写。端点为有符号
  64 位整数且 `lo < hi`（违反为 `BAD_SCOPE`/`OUT_OF_SCOPE`）。
* 省略凭据的旧调用仍表示全部 open 订单的全量视图（无新鲜度保护，保持兼容）。

## 错误码（`ViewEditError.code`）

`BAD_CREDENTIAL`、`STALE_VIEW`、`CONFLICT`、`OUT_OF_SCOPE`、
`BAD_SCOPE`、`BAD_TARGET`。错误信息只引用关系身份（order_id/customer_id）
与冲突字段，绝不包含 `private_note/internal_note`，也不出现 SQL 文本。

## 验证

```sh
python3 demo.py            # 原始反例：隐藏信息不再丢失（counterexample_detected=false）
python3 revision_probe.py  # scoped 空编辑保留 inside/outside/closed 与全部隐藏列
python3 test_lens.py       # 135 项真实 SQLite 断言
```

`test_lens.py` 覆盖：空编辑 GetPut、单金额编辑、共享客户一致/矛盾改名、
改归属（已有/新客户）、可见删除、隐藏 ID 占用、读后内部备注变化、
STALE_VIEW 全部来源、凭据篡改、坏目标、scope 边界与 int64 域、
范围外新增/跨范围移动/共享名字传播、越界目标、两个连接的受控交错、
保存点嵌套与事务原子性。每次成功 put 后查询**全部源列**核对保留信息并用
绑定 scope 重新 `open_view` 验证目标等价；每次失败同时校验逻辑内容与
数据库文件字节均未变化。
