# 双时态观测库迁移契约

## 现有入口和旧格式

使用 Python 标准库及 SQLite，无第三方依赖；setup 为空。所有文件均在项目根目录。
ledger.Store(path=":memory:")、apply(request, rows, fail_at=None)、read(valid_at, known_at=None)、head、close() 是既有接口；db 是用于核账的 SQLite 连接。
samples.py 是七批原始请求；seed.sql 是由这些请求生成的完整 v1 数据库 SQL 转储，可直接用 sqlite3.Connection.executescript 建库，不需要先运行新版 Store。
python3 checks.py normal 核对正常业务、历史读取、回执、输入拒绝和事务回滚；python3 checks.py demo 展示真实历史差异。

rows 为列表，每行是 [id, rev, entity, start, end, value, deleted]，批内 id 唯一。
id、entity、request 为非空 Unicode 标量字符串；value 为 Unicode 标量字符串或 null，deleted 必须是布尔值。
rev 为正的有符号 64 位整数；start 和非 null 的 end 为有符号 64 位整数，end>start；布尔值不算整数。
[start,end) 是有效期，end=null 表示无上界。删除行同样完整校验、保存字段。
完整版本替换同 id 的旧版本，而不是只覆盖新版本的有效区间。

v1 的 meta(singleton=1,version=1,head) 保存知识代次。
batches(seq,accepted_json) 按代次存放真正接纳的完整行，accepted_json 为 canonical(accepted_rows)。
receipts(request,digest,reply_json) 保存每个成功请求的绑定及原始回执。
digest 为 ledger.canonical(rows).encode("utf-8") 的 SHA-256，canonical 的完整实现见 ledger.py。
首次成功请求使 head 增加 1，即使为空或只有过期版本；同 id 只接纳更高 rev，与当前最高 rev 同版同内容、低版忽略，与当前最高 rev 同版异内容抛 ValueError("REVISION_CONFLICT")。
apply 返回 {"seq":知识代次,"accepted":按 id 排序的接纳 id 列表}。
已成功 request 同内容重放优先返回原回执且不改变数据库，异内容抛 ValueError("REQUEST_CONFLICT")。
所有输入先校验，输入非法抛 ValueError("INPUT")；原有异常信息保持不变。

read 的 valid_at 是有效时间，known_at 是 0 至 head 的知识代次，缺省使用同一读取事务捕获的 head。
每个 id 先在该知识代次已接纳的版本中选最高 rev，再过滤删除、entity 和有效期；不能先过滤有效期再选版本。
每个 entity 的有效候选按 (rev,id) 取最大者，id 使用 Python 字符串顺序，不能按到达代次取胜。
read 返回按 entity 排序的 [entity,value,胜者id,胜者rev] 列表；没有候选就没有该行，value=null 的行仍存在。
知识代次 0 全空；valid_at 和 known_at 的类型、取值非法均抛 ValueError("INPUT")。

样本预期：read(25,1) 为 [["basin",null,"b",2],["coast","sea","c",1]]。
read(5,2) 为 []，因为 a 的完整版本已换成 [10,70)，c 已移出 coast。
read(27,4) 为 [["basin",null,"b",2]]；read(27,5) 为 [["basin","shadow","z",1]]。
b 在代次 5 缩到 [30,50) 后，[25,30) 显露 z；代次 4 接纳 z 时它仍被 b 隐藏。

## 本轮目标格式与维护窗口

新增 migration.upgrade(path, fail_at=None)，将已有 v1 库原子升级为 schema_v2.sql 给出的 v2 Schema。
调用方在维护窗口关闭其他连接，不要求在线迁移、并发推进、后台服务或迁移进度协议。
保留 meta 的 head、receipts 的三列内容和所有已接纳源版本；成功后 version=2 并移除 batches 表。
source_versions 每行保存一个已接纳版本，learned 为它原来的知识代次；value_json 为 value 的 JSON 文本，包括字符串 "null"。
winner_segments 的 [v_from,v_to)×[k_from,k_to) 是一个胜者不变的矩形，null 上界表示无上界，winner、winner_rev、value_json 是原 read 可见的完整胜者。
同一 entity、有效时间及知识代次至多命中一个矩形；无候选的区域不存行。下游使用 k_from、k_to 审计该片有效期可见胜者的出现和撤回代次。
矩形只能在该片有效期的可见胜者确实变化时开启或关闭；未变化部分不得制造新的知识边界，可以切分已有历史矩形。
相同可见 value 但胜者 id 或 rev 不同，也算胜者变化；隐藏候选变化、低版忽略和空批不算胜者变化。
例如唯一 a@rev1 在代次 1 覆盖 [0,100)，代次 2 新增更高优先级 b 覆盖 [30,40)，则 a 的 [0,30) 和 [40,100) 仍从知识代次 1 开放，只有 [30,40) 的 a 在代次 2 关闭；不能关闭整个旧段再重开两侧。
不要求矩形分解唯一，不要求最少矩形；不引入额外压缩算法。

失败时旧 Schema、数据及回执全部不变；upgrade 对已升级库无副作用。
upgrade 在完成目标数据、尚未发布版本前支持 fail_at="before_publish"，抛 RuntimeError("INJECTED") 并整次回滚。
迁移后 Store.apply/read 的返回、异常及事务行为保持兼容，关闭重开后历史和当前结果不变；新库可直接创建为 v2。
新批次接纳版本、矩形更新、head 与回执须一起提交或回滚；保留 after_log、after_receipt 注入名称及 RuntimeError("INJECTED")，在相应版本写入后、回执写入后触发。
读取只查询 meta 与 winner_segments，不得重放旧批次、重算 source_versions 或保存逐代完整快照；不能按单位时间展开。
新写只更新本批接纳版本涉及的旧、新 entity 的当前区间，不重建已关闭历史；可重扫这些 entity 的源版本并点查各 id 的最高版本，不要求局部索引算法。

## 已提供的交付核验入口

保留已有断言，可以补充验证。python3 checks.py target 从冻结的 seed.sql 建立真实旧库，经过失败回滚、成功升级、重复升级及升级后新写和重开，核对旧接口与目标矩形。
核验器按源有效期和矩形端点划分有限常值区间，独立检查矩形覆盖与知识边界，不逐整数展开，也不调用生产区间维护函数。
交付时将 .pointwise-runtime.json 的 verify 改为 ["python3","checks.py","target"]；demo 保持真实业务展示。
提供简短 MIGRATION.md，说明版本筛选与区间维护、维护窗口操作及失败重试边界。
