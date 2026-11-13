# 双时态观测库 v1 → v2 迁移说明

## 1. 版本筛选规则（迁移与新写共用同一语义）

对任一读取点 `(valid_at, known_at)`，胜者判定严格分两步，顺序不可颠倒：

1. **先选版本，再过滤区间**：对每个 `rid`，只在 `learned <= known_at`
   已接纳的版本中取最高 `rev`（完整版本替换语义；同 rev 同内容忽略、
   异内容报 `REVISION_CONFLICT`、低 rev 忽略）。
2. **再过滤并选胜者**：在每个 id 的最高版本上，剔除删除行、entity 不匹配行、
   有效期不覆盖 `valid_at` 的行（`[v_from,v_to)`，`v_to=null` 为无上界）；
   同一 entity 的候选按 `(rev, rid)` 取最大，id 按 Python 字符串顺序比较。
   没有候选则该 entity 不出现（缺行）；`value=null` 的胜者仍然成行，
   即 `null` 与缺行不等价。

该规则由 `ledger.winner_at` 以纯函数实现，迁移期全量重建与升级后增量维护
调用同一份逻辑，避免两套实现漂移。知识代次 0 恒为空。

## 2. 区间（矩形）维护

`winner_segments` 的每行是有效时间 × 知识时间上胜者不变的矩形
`[v_from,v_to) × [k_from,k_to)`，null 上界表示无上界；无候选区域不存行。

### 2.1 有效时间切分

- 对一个 entity，收集其候选 rid（当前或曾经属于该 entity 的所有 id）
  全部版本的有效区间端点，相邻端点构成常值 slab；含 `v_to=null`
  的版本再追加一个无上界尾 slab。
- 把已移走 rid 的旧版本端点也纳入，保证实体迁移、区间收缩后
  原 entity 的覆盖范围不丢 slab。

### 2.2 迁移期全量重建（`migration.upgrade`）

- 每个 slab 上对知识代次 1..head 逐代求胜者，做游程编码：
  胜者签名 `(winner, winner_rev, value_json)` 相同的连续代次合并为一个矩形，
  只在胜者真实变化的代次开启/关闭矩形。
- 隐藏候选变化、低版本忽略、空批不会改变胜者，因此不产生知识边界。
  相同 value 但胜者 id/rev 不同仍算变化。
- 不要求最少矩形，不做额外压缩；可在已有历史矩形内部沿有效时间切分。

### 2.3 升级后增量维护（`Store.apply` → `_refresh_entity`）

新批次只刷新本批接纳版本涉及的旧、新 entity，其它 entity 与所有
`k_to < seq` 的已关闭历史矩形一律不删除、不重建：

- 删除该 entity 当前仍开放（`k_to IS NULL`）的矩形，按新的 slab 集合重建；
  已关闭矩形保持原样。
- 对每个 slab 比较 seq-1 与 seq 的胜者：
  - 覆盖它的旧矩形仍开放且胜者未变：继承原 `k_from` 继续开放，
    仅在有效时间轴上细分，不制造新的知识边界；
  - 胜者在 seq 变化/消失：旧开放矩形在 seq 关闭，新胜者从 seq 开放；
  - slab 被细分且只有已关闭矩形覆盖：补出的历史矩形从该关闭矩形的
    `k_to` 开始、到 seq 关闭，与既有关闭矩形首尾相接而不重叠。
- 写入路径允许重扫受影响 entity 的 `source_versions` 并对各 id
  点查最高版本；读取路径（`read`）只查询 `meta` 与 `winner_segments`，
  不重放批次、不重算源版本、不存逐代快照，也不按单位时间展开。

### 2.4 原子性

新批次的 `source_versions` 写入、矩形更新、`meta.head` 推进与 receipt
在同一 `BEGIN IMMEDIATE` 事务内提交；`after_log`（版本写入后）与
`after_receipt`（回执写入后）注入点抛出 `RuntimeError("INJECTED")`
时整事务回滚。成功 request 的同内容重放直接返回原回执且不改库，
异内容抛 `REQUEST_CONFLICT`。

## 3. 维护窗口操作

1. 关闭到该数据库文件的其它连接（本方案不支持在线/并发迁移）。
2. 调用 `migration.upgrade(path)`；需要演练失败路径时传
   `fail_at="before_publish"`。
3. 迁移完成后 `meta.version=2`、`batches` 表被移除，`meta.head`、
   `receipts` 三列内容与全部已接纳源版本（迁入 `source_versions`，
   `learned` 记原知识代次，`value_json` 含字符串 `"null"`）原样保留。
4. 之后用 `Store(path)` 打开即走 v2 链路；`apply/read/head/close/db`
   的签名、返回值、异常与事务行为与 v1 兼容，关闭重开结果不变。
   全新库直接创建为 v2，无需先建 v1。

## 4. 失败与重试边界

- 整个升级在单事务内完成，SQLite 的 DDL 与 DML 同事务可回滚。
  任何异常（含 `before_publish` 注入的 `INJECTED`）都会 ROLLBACK，
  磁盘上的旧 schema、数据与回执逐字节不变，可用 `iterdump` 比对验证。
- 发布动作（`version=2` 与 `DROP batches`）是事务内最后一步；
  在它之前失败，外界观察到的始终是完整 v1 库。
- 对已是 v2 的库调用 `upgrade` 直接返回，无任何写入，重复升级无副作用；
  因此失败后或不确定状态下可安全重试 `upgrade(path)`。
- 非 v1/v2 的 schema 抛 `RuntimeError("UNSUPPORTED_SCHEMA")`，不做改动。
- 升级后新写的失败语义与 v1 一致：`after_log`/`after_receipt` 注入或
  任意输入/冲突错误都回滚整批，源版本、矩形、head、回执要么全部生效、
  要么全部不变，重试同一请求得到相同结果。
