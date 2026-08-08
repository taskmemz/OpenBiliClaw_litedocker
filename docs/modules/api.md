# 后端 API

## 概述

`src/openbiliclaw/api/` 暴露本地 FastAPI 契约，并把 UI 请求编排到 durable storage、Soul、Dialogue 与 runtime。本文记录对话确认入口新增的公开端点；通用鉴权见 [api-auth.md](api-auth.md)，初始化端点见 [init.md](init.md)。

## 初始化期间的配置探测

`POST /api/config/probe-service` 只在内存副本上应用设置页草稿并真实探测 LLM、默认链、embedding 或网络策略，不写 `config.toml`、不热重载 runtime。它因此不受 guided init 的 HTTP 写端 409 门控；初始化运行时仍可测试，LLM 请求继续经过进程级稳定 total gate。`PUT /api/config` 仍在初始化期间返回 `409 init_running`，避免替换本轮任务正在使用的组件。

视觉预热配置也属于同一事务契约：`PUT /api/config` 对 `keyframe_max_frames (1..12)`、
`keyframe_fetch_limit (1..200)`、`danmaku_fetch_limit (1..200)` 和
`danmaku_max_chars (100..2000)` 做范围校验，保存后由 RuntimeContext 透传到推荐引擎；配置文件
与 API round-trip 保持这些数值。配置字段仍默认关闭视觉 / 弹幕功能，不会改变默认排序。

Discovery 配置响应与更新白名单同时公开 `keyword_digest_grace_hours`，默认 `24`、合法范围
`0..168`。`PUT /api/config` 拒绝布尔值、非整数和越界值；合法值进入同一次 TOML 持久化与
runtime apply。`0` 是只关闭跨 digest 关键词复用的回滚值，不会关闭统一 planner 或删除历史行。

## 配置保存与后台应用

`PUT /api/config` 把“持久化成功”和“运行时已经切换”分成两个明确阶段。请求仍在 `_CONFIG_SAVE_LOCK` 内完成校验、`config.toml.bak` 快照、`config.toml` 写入和凭据存储，然后统一立即返回 `202 apply_state="queued"`、`apply_revision` 与已脱敏配置快照；运行时 lane 由 app-owned latest-wins 队列在后台安全应用，前端通过 `GET /api/config/apply-status` 或 runtime event 观察终态，不把 202 当作失败。

Phase 2 cognition rollout 在配置 API 中也是 task-scoped：`soul` GET/PUT 模型公开
`preference_prompt_view`、`awareness_prompt_view`、`insight_prompt_view` 三个
`legacy|compact-v1` 字段，默认分别为 `legacy / compact-v1 / legacy`。旧的聚合
`cognition_prompt_view` 不在响应模型或更新白名单中。热重载后 Awareness 字段只影响
`soul.awareness_confusions`；普通 `soul.awareness` 固定使用 `legacy`，其余两个值各自只影响
对应 analyzer。

后台配置应用队列为 app-owned、latest-wins：正在应用的修订不会被取消，尚未开始的多个修订会合并为最新一份；因为每次 PATCH 都基于最新已落盘配置构建，合并不会丢掉前一轮已保存字段。成功广播 `config_reloaded`；失败且没有更新修订等待时恢复最后一次已生效配置并广播 `config_reload_failed`，若已有更新修订则不回滚覆盖它，直接继续应用最新值。进程在排队期间退出也不会丢配置，下一次启动直接从已落盘 `config.toml` 构建运行时。

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `PUT /api/config` | ✅ | 持久化成功后统一返回 `202 queued`；响应新增 `apply_state`、`apply_revision`，原有 `reloaded` / `rollback_applied` / `restart_required` 保持兼容。 |
| `GET /api/config/apply-status` | ✅ | 返回 `state`、最新请求修订、最后已应用修订、消息、非敏感错误分类和更新时间；不包含配置内容或凭据。 |

guided init 不与待应用配置并行：队列为 `queued/applying` 时 `POST /api/init` 返回 `409 config_applying`；init 已开始时 `PUT /api/config` 仍返回既有 `409 init_running`。

## 公开项目统计

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `GET /api/project-stats` | ✅ | 桌面 Web 与扩展读取 GitHub Star 数量的公开同源端点。后端通过海外网络策略请求 GitHub，持久化 12 小时缓存并使用 ETag 条件请求；遇到 403 / 429 时遵循 `Retry-After` / `X-RateLimit-Reset` 有界退避。GitHub 失败不会透传为 HTTP 错误：有缓存返回 `source="cache", stale=true`，无缓存返回 `source="unavailable", stale=true` 且省略 `github_stars`，两者均为 200。该端点不包含用户数据，在密码门禁和降级模式下保持公开。 |

## 惊喜推荐消费契约

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `POST /api/delight/respond` | ✅ | `response="dismiss"` 是三端“× / 看过了，不再推荐”的永久消费动作：服务端按 `bvid` 解析 `content_cache` 中的 canonical `source_platform/content_id`，先写 `seen_items`，再置 `delight_notified=1`；后续普通推荐与惊喜推荐均硬排除。`view` 只置惊喜已读，`dislike` 另记录负偏好，`like/chat` 继续保留当前候选。 |
| `POST /api/delight/sent` | ✅ | 仅确认主动通知已送达并维护推送冷却，不代表用户已看，不写 `seen_items`；UI 叉号不得把它作为消费路径。 |

## 推荐反馈端点

### 推荐输出与 dislike 的即时一致性

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `GET /api/recommendations` | ✅ | 只读未处理历史；1 秒 snapshot 只有在 TTL 与 effective dislike digest 都未变化时复用。加载期间 dislike 变化会按新快照重读，再在 franchise cap 前过滤。 |
| `POST /api/recommendations/reshuffle` / `append` | ✅ | serve 使用带 flat-preference overlay 的画像，完成后在 HTTP 序列化前再读一次最新 effective dislikes，关闭请求进行中的偏好竞态。 |
| `GET /api/notifications/pending` | ✅ | 单条候选在返回前按最新 dislike 复核；模糊命中时不使用多卡窗口的“全灭恢复”保护。 |
| profile edit / `POST /api/feedback` | ✅ | durable edit 或单卡反馈 projection 完成后立即失效 recommendation snapshot；单卡反馈仍由 `exclude_processed` 同步隐藏。 |

这些边界不阻止 discovery 搜索，也不等待异步语义清池或完整 Soul rebuild。

### 公开事件写入口的幂等 ID

以下三个公开写入口都要求调用方显式提供稳定 ID；字段会先去除首尾空白，再校验非空且最长 400 字符：

| 入口 | 必填字段 | 重试规则 |
|---|---|---|
| `POST /api/events` | 批次中每个 event 的 `event_id` | 同一个具体动作的网络重试必须复用；两个外观相同但实际独立的动作必须使用不同 ID。 |
| `POST /api/feedback` | `request_id` | 同一 recommendation/type/note 重试复用；同 ID 改 payload 返回 409。 |
| `POST /api/recommendation-click` | `request_id` | 同一 concrete click 重试复用；稳定身份优先使用 recommendation/content ID，不把会轮换的签名 URL 或重渲染标题纳入 identity。 |

ID 字段是严格 JSON string，不接受数字、布尔或其它类型的自动转换。

字段缺失、空串、纯空白或去空白后超过 400 字符均由请求模型返回 HTTP 422；此时 route handler 尚未运行，不会写 `events`、`seen_items`、recommendation feedback 投影或其它数据库状态。服务端不会为这些 HTTP 入口补随机 ID，因为响应丢失后重新生成会把一次动作变成两次 durable fact。扩展、移动 Web 与桌面 Web 会把 pending ID 持久化到动作成功；顶层 `openbiliclaw feedback` 在省略 `--request-id` 时生成并打印一个 ID，跨命令重试必须复用该输出；OpenClaw CLI/skill 则把 `request_id` 设为必填。

`POST /api/feedback` 的成功边界是 **event-first 的两次 commit**，不是跨表原子事务：先由 `EventIngressService` 把带 `request_id` 幂等键的 `feedback` event 提交到 durable ledger，再单独调用 `update_recommendation_feedback()` 提交 recommendation 展示投影。若进程或数据库故障发生在 event commit → recommendation projection 之间，本次请求会失败；客户端用同一 `request_id` 重试时，event ingress 返回 duplicate receipt，API 校验 durable row 中的 recommendation/type/note 与请求一致后重新执行投影，从而修复间隙。相同 `request_id` 携带不同反馈返回 409，不能驱动投影。之后只唤醒 event scheduler 并立即返回；HTTP 不获取 pipeline lock，也不等待 LLM。

当 `scheduler.unified_interest_line=true`（默认）时，`events` 表是 durable ingress queue：app-owned `EventProcessingScheduler` 先由 generic `profile_events` consumer 领取显式归其所有的普通行为/推荐点击，再由 `content_feedback` consumer 领取 `like/dislike/comment/dismiss` 内容反馈；二者都以 event row ID 派生稳定 signal ID，通过 `checkpointed_enqueue_batch()` 把 buffer 与各自 cursor 原子发布到同一份 `pipeline_state.json`，随后 owner 调用 `tick_if_buffered()`。只有独立周期画像维护调用 `tick()`。首次 app startup 只同步发布 owner cutover fence 并 admission 一个由 scheduler 持有的 recovery task，lifespan 不 await event scan、buffer consume 或 LLM，因而 provider 401、慢响应或永不返回都不能阻止 HTTP listener/health 就绪；scheduler 在 shutdown 负责取消并 gather 该任务。配置热重载仍先 pause+drain，再同步 recover 遗留 event，最后恢复新 runtime 后台任务，保持旧 owner 到新 owner 的顺序屏障。两条生命周期都覆盖 HTTP commit→wake、event scan→checkpoint 或 checkpoint→consume 的崩溃窗口。旧名 `FeedbackBatchScheduler` 仅为兼容 alias。

`POST /api/events` 将 raw `dislike` 规范为 `feedback`。统一线下，显式内容反馈只唤醒上述 durable cursor owner，不再同时进入 generic `signals_from_events()` / profile backfill；hypothesis / import feedback 属于其它 owner，同样不进 generic 增量路径且只由 feedback cursor 越过；retraction 仍保留 generic pipeline 的折价与 tombstone 路径。`unified_interest_line=false` 时维持旧 feedback batch 与 generic event 行为。

## 来源任务结果的两阶段完成

`POST /api/sources/{xhs,dy,yt,zhihu,reddit}/task-result` 的最终回调不再先把任务写成 `completed`。后端先在 `BEGIN IMMEDIATE` 中合并并冻结第一份 canonical result（含 XHS `self_info` 私有快照），任务仍保持非终态；随后只从这份持久结果重放来源事件、seen-key 和来源专属投影，全部成功后才执行不替换 `result_json` 的 terminal flip。若进程分别退出在 canonical merge→event ingress、event ingress→seen-key 或 seen-key→terminal 三个窗口，后续 callback 会忽略变化后的 body，用第一份结果补齐缺口。队列把 staged marker 视为业务 mutation 的逻辑终态：并发/迟到的 partial、final、fail、rate-limit 都不能改写它；但它继续遵守普通 claim lease，丢失非 2xx 响应后会在 15 分钟 lease 过期时由 dispatcher 重新领取，从而自动触发修复。seen-key 通过 `update_source_bootstrap_state()` 原子、严格落盘并按源保留最新 5,000 个身份键，失败会阻止 terminal flip；事件稳定键不含 task ID，因此 ingress 已提交但 marker 未写时的重放只返回 duplicate receipt。Reddit post/comment/subreddit/user 使用各自稳定身份，comment URL fallback 只接受含 comment id 的完整 permalink，不能把 post id 或标题误作 comment key。

周期任务 payload 带 `incremental=true`；五源 handler 在 guided init 外给 durable event 标记 `profile_update_owner="generic"`，在 init-owned 回调中只落事实、由阶段 2/3 统一建模。事件 ingress 成功或 duplicate receipt 后才按响应顺序 checkpoint seen key，再翻 terminal；没有 handler 直接调用画像 pipeline。扩展离线时 runtime 不创建任务，也不推进调度时间。

## 封面代理与抓取状态

`GET /api/image-proxy?url=...` 先在线程中读取本地 `data/image-cache/`；命中不占网络槽并返回原始图片类型、`Cache-Control`、`nosniff` 与 `X-Image-Cache: hit`。未命中进入 app-owned `ImageFetchCoordinator`：API 前台请求和 `ContinuousRefreshController` 后台预取共用总上限 4，后台最多 3，队列有前台请求时优先放行；同一 `image_cache_key(url)` 只产生一个 upstream task。单个 HTTP waiter 取消不会取消共享抓取，>=500 失败仍会在线程中做一次“并发写入已落盘”的 cache race fallback。成功响应保留 `X-Image-Cache: miss`。

抓取继续复用统一 SSRF 边界：域名白名单、每次 redirect 重验、`image/*`、10MB 上限，以及国内 CDN 直连 / 境外 CDN 继承代理。磁盘写入使用同目录临时文件 `flush + fsync + os.replace`，失败只保留旧文件或无文件，不暴露半写结果。日志只记录 host、cache hash 前缀和错误类别，不记录签名路径/query；`GET /api/runtime-status` 公开 `image_fetch_active/waiting/inflight_keys` 与 `upstream_started/singleflight_joins/peak_active/peak_background`，这些字段只含整数，不含 URL 或 token。协调器不随 `RuntimeContext` 热重载替换；新 controller 在后台任务恢复前重绑同一实例，shutdown 先停 refresh producer 再取消协调器持有的 active/queued upstream task。

## 降级配置恢复

`PUT /api/config` 在 `llm_registry_unavailable` 降级态下不再只写盘并要求重启。服务端会复用当前进程已经初始化的数据库、MemoryManager、事件总线、任务注册表和 LLM total gate，通过正常热重载路径原子构造完整的 LLM Registry、Soul、Discovery、Recommendation、来源客户端与 runtime controller。构造全部成功后才解除业务 API 的 503 guard，并在后台应用状态进入 `applied` 后广播 `config_reloaded`；`/setup/` 会等待该终态，插件与桌面设置页也会观察同一状态后继续。

如果核心运行时构造失败，已有 `config.toml` 会从事务备份恢复，响应为 HTTP 503、`ok=false`、`rollback_applied=true`，降级 guard 保持不变。若核心已经成功发布、只是附属后台循环重启失败，则保留已生效的新配置与健康运行时，返回 `ok=true`、`reloaded=true` 并携带 warning，避免把磁盘配置回滚成与内存运行时不一致的旧版本。只有没有可回滚旧文件且进程内激活失败的异常 bootstrap 路径，才保留 `restart_required=true` 兼容兜底。

## 小红书任务安全边界

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `GET /api/sources/xhs/next-task` | ✅ | native-save job 仍是用户显式动作；自动 discovery 的 search / creator / bootstrap 则在每次 claim 前动态检查 `sources.xiaohongshu.enabled` 与 `scheduler.enabled`。任一关闭时返回 bodyless 204，既有任务保持 pending，不会再驱动扩展打开页面。search / creator 的 `task_interval_seconds` 是目标值，后端按任务 ID 施加 ±25% 稳定抖动并持久化实际下一次时间；处于节流或平台冷却时返回 204，存在明确等待时间时附 `Retry-After`。 |
| `POST /api/sources/xhs/task-result` | ✅ | 除 `ok / partial / empty / error` 外接受 `status="rate_limited"`。legacy task 命中后终结该任务、按连续轮次持久化 `1h → 2h → 4h … → 24h` 平台冷却，并将关联 `source_keyword_id` 从 executing 无损退回 pending；同一活动冷却内的重复报告不增加轮次，native-save 结果命中同样打开平台级冷却。冷却后的正常 search / creator 完成会重置轮次，活动冷却中的晚到成功不会提前解封。search / creator 的 `empty` 仍作为可重试失败，但缺失 error 的旧插件 payload 会归一为 `xhs_empty_result`；扩展结构化 debug 只允许 pathname、页面生命周期和 route anchor 计数，不要求或存储搜索词、验证页全文或页面 state。 |
| `POST /api/sources/xhs/observed-urls` | ✅ | URL-only 与带 note metadata 两条分支都接受 `/explore/{id}`、旧 `/discovery/item/{id}` 和 `/search_result/{id}` 三种笔记路由；`/search_result?keyword=...` 搜索列表页本身不计入 accepted。metadata 继续进入 `discovery_candidates`，URL-only 继续写 observed ledger 并参与 token 回填。 |
| `GET /api/sources/status` | ✅ | 来源仍开启且冷却生效时，将小红书 legacy 状态投影为 `state="rate_limited"`、`feed_paused=true` 并显示连续触发轮次和剩余分钟；来源已关闭时不让冷却覆盖 `enabled=false` 的正交配置事实。该端点只读本地状态，不访问小红书。 |

## 对话确认端点

### Turn 级上下文绑定（2026-08-01）

`chat_turns.reply_to_turn_id` 是用户 turn 指向 durable card/question 的唯一显式关系。带关系的
请求只提交 target ID；服务端在 user row INSERT 前读取 completed target 与 settlement queue 的
exact admission snapshot，生成并冻结 `payload.dialogue_binding`（`bound`、canonical context、
完整 `context_digest`）。`kind/ref/generation/title/evidence` 等客户端字段不被采信，冲突的
scope/subject 返回错误；无关系请求继续区分 `ordinary` 与 `detached`。

回复、历史关系前缀、raw dialogue event、learn envelope、engine provenance 与卡片结算都消费
同一冻结 binding。绑定 target 在 POST 前已过期、被保留、失败或不存在时不会创建 fallback user
row；相同 `turn_id` 的同一 normalized request 仍幂等，任何 relation/message 分歧返回
`turn_id_conflict`。

| 方法与路径 | 状态 | 契约 |
|---|---|---|
| `POST /api/chat/turns` | ✅ | 普通消息在 user row INSERT 前解析可选 `reply_to_turn_id`，冻结 server-owned canonical `DialogueTurnBinding`（bound/ordinary/detached）和 context digest；随后落成 `pending` 并立即返回，只向 app-owned `DurableChatReplyScheduler` 发 wake；单 worker 按 `chat_turns.rowid` 严格串行生成回复，启动会分页恢复全部 pending。provider、限流、配置、超时与取消都保持 pending 并原位有界退避，不能被后续 turn 越过；只有显式无效/空响应可终结为 failed。`scope="hypothesis"` 时服务端生成结构化卡片 payload（`type/kind/ref/title/evidence_refs/actions/state`），直接返回 `status="completed"`，不会调用 LLM worker。若双轨冷却允许，普通 durable 用户消息会先原子插入一条系统确认卡/问题，再写用户 turn；payload 的 `attached_to_turn_id` 负责重试与重启去重。 |
| `GET /api/chat/contexts/{reply_to_turn_id}` | ✅ | 只读返回 canonical context preview（target、kind/ref/generation、可读 evidence、digest）。不创建 queue job、anchor、event，也不修改 card；三端只持久化 target ID，并用 preview 校验恢复。 |
| `GET /api/chat/turns?session=<label>` | ✅ | `session` 只过滤当前 UI 可见 turn；插件、移动 Web、桌面 Web 的主聊天统一使用 `session=popup` 并读取完整 `chat/hypothesis/confusion` 可见历史，因此三端共享普通消息、确认卡和澄清问题；其它 session 仍可用于隔离集成。不同 UI 仍共享一份认知 history。列表中的每个非终态卡片只 submit `card.reconcile` 到唯一结算队列并返回本次 durable 快照；request task 不直接写 card/object/anchor。 |
| `GET /api/chat/turns/{turn_id}` | ✅ | 返回单个 durable turn。普通 turn 仍为 pending 时只幂等唤醒同一 reply worker，重复轮询不会复制 queued/in-flight/backoff 工作。若读到非终态卡片，只同步 admission `card.reconcile` 并立即返回快照。worker 会为 `applied=1` receipt 补 stable audit、跨 session projection 与 exact-generation 解锚，也会把没有对应 active anchor 的 orphan `discussing` 校正回 `pending`；因此 publication gap 的第一次 GET 可仍见旧态，queue 完成后的下一次 GET 见权威状态。 |
| `GET /api/chat/pending-confirmations` | ✅ | 读取前在 settlement worker 空闲时扫描 orphan claim：只有 `clarifying` claim 已超过 30 秒创建安全窗、ask-turn identity 未变化、且 durable turn 仍不存在时才释放；worker 正忙时跳过该次修复并直接返回 durable 快照，避免只读 UI 被长 LLM job 卡住，下一次空闲读取/open 会继续修复。随后返回 `{"count":N,"items":[...]}`；只列未结算的高优先级对象且最多 3 条：未验证假设 `confidence>=0.60`、active 疑惑 `interpretation_confidence>=0.50`。无活跃澄清时疑惑固定预留 1 席；已有全局 `clarifying` 时只保留该持有者，隐藏必然无法 claim 的其它 open 疑惑。UI 传 `?session=popup|webui` 后，若该持有者已在本 session 有 turn，则不重复显示；其它 session 仍可打开同一 ref 并获得本地 turn。`?count_only=1` 保留轻量只读响应 `{"count":N}`，供兼容客户端/诊断使用；当前 service worker 明确不调用它，工具栏角标只表达后端不可达或未初始化，待聊数字只在 popup、移动 Web 与桌面 Web 的对话入口显示。`openbiliclaw questions` 读取完整响应且不复制筛选规则。用户主动列表不套用系统冷却。 |
| `POST /api/chat/pending-confirmations/{ref}/open` | ✅ | body 为 `{"session":"popup|webui|..."}`。若唯一 settlement worker 正在处理长 LLM job 或处于原子交接，端点在任何 claim/turn 写入前返回 `503 detail.code="dialogue_busy"` 与 `Retry-After: 2`；popup、移动 Web 与桌面 Web 共享 helper，最长按安全热重载窗口自动重试并显示等待态。空闲后，假设生成 completed card；疑惑通过 required `confusion.open.sync` 进入 `clarifying`，再由 required `anchor.establish` 以 `pending_open` 建锚，不使用会超时后继续执行的 1 秒 fast path，因此不会留下“claim 已完成、turn 未创建”的半截状态。相同 `(ref,session)` 原子复用，跨 session 各自产 turn；API 不在 request task 执行 protected mutation。 |
| `POST /api/chat/cards/{turn_id}/action` | ✅ | body 为 `{"action":"confirm|reject|discuss|defer"}`。四动作分别 submit `settle.hypothesis`、`card.discuss`、`card.defer` 到唯一队列；confirm/reject 与锚定 `support/contradict/revise/answer`、普通 chat settles、legacy endpoint 共用 immutable ref winner。discuss 在 worker 内 `pending→discussing→建锚`，建锚失败立即补偿回 pending；defer 只对 pending/discussing 卡在 worker 内更新卡片/冷却，若卡由 pending-open 建锚但仍保持 `pending`，会按 origin turn 精确释放同代锚，若卡已 confirmed/rejected 则返回权威终态的 `already_settled` 且不写 cooldown。HTTP 最多等本地 job 1 秒，完成保持同步 `200`，队头阻塞返回 `202 processing` 且不会取消已入队 job。 |
| `POST /api/insights/feedback` | deprecated | 保留旧客户端响应结构和 `Deprecation: true`，内部通过共同 façade submit 同一队列，台账 `source="legacy_endpoint"`；1 秒内未完成时同样返回 HTTP `202`，不新增 legacy 专用 executor。**锚冲突返回 `409`**：当另一张卡片持有对话锚时结算会被拒绝（`outcome=stale_anchor` / `anchor_dependency_failed`），此时 `card_settlements` 与台账都没有写入，端点返回 `409` 并在 detail 里说明原因，`Deprecation` / `Link` 头仍然保留。旧行为把这种拒绝包装成 `200 {"ok":true,"matched":false}`，老客户端会误以为确认成功。 |

### 卡片 action 返回

- `outcome="applied"`：本次已由 worker 完成 event/object/derived/rebuild marker 并发布 `applied=1`。
- `outcome="already_settled"`：已存在 `applied=1` 的对象结算；返回既有 verdict 并刷新本卡片投影。
- HTTP `202` + `outcome="processing"`：本地 1 秒等待预算耗尽；入队 completion 被 shield、继续在唯一 worker 执行，不会把 `applied=0` 伪装成终态。
- `outcome="discussing"` / `"deferred"`：分别表示活锚已建立 / 当前卡片已延期。
- `state="revised"`（终态，文案「已按你的修正记下」）：修正式结算——原假设被替换、派生假设已写入。它**不是** `rejected`；把 revise 投影成否定会让刚说完「我认可修正版」的用户看到「已标记不准」。
- `outcome="stale_anchor"` / `"anchor_dependency_failed"`（`state="stale"`）：对话锚被另一张卡片占用，本次结算被拒绝，`card_settlements` 与台账均无写入。前端共享 helper 把这两个 outcome 归入 `retryable_error`：乐观态回滚到操作前的真实状态，提示用户先结束当前正在聊的那条再重试——**不得**回落到乐观终态，否则卡片会显示「已确认」而后端什么都没记。

## 一致性边界

所有生产 `dialogue.respond()` 入口（durable reply、惊喜 chat、legacy `/api/chat`、兴趣探针 chat、避雷探针 chat）共享 app-owned `DialogueExecutionCoordinator`，同一时刻最多一个 active execution。调用方拿到 lease 后才解析当前 `ctx.dialogue` 与对应 Soul speculator，并把回复后的认知、事件与状态副作用一并留在 lease 内。配置热重载先暂停 admission、排空 active execution，才发布新 runtime；等待中的请求恢复后解析新 owner。25 分钟内不能排空时不调用 rebuild，恢复旧 lane 并回滚配置。guided init 的 `resume_execution_lanes=false` 只控制 event lane，不会把独立 chat lane 留在 paused。

durable reply 的可见终态使用 `WHERE status='pending'` compare-and-swap：模型调用在进程崩溃窗口可能至少一次，但 completed/failed 只发布一次。`/api/runtime-status` 以 SQLite 的真实 pending 数暴露 `chat_reply_depth`，另有 `chat_reply_active/last_error/processed`；即使 runtime controller 降级不可用也保留 event/chat scheduler 状态，且字段不含用户消息或回复内容。

`event_lane_depth` 只表示调度器当前是否有 dirty wake（`0/1`），不是 SQLite event backlog，也不是两个 durable cursor 之后的待处理行数。真实恢复依据是 `events` 表与 `pipeline_state.json` 中各 consumer checkpoint；`event_lane_active/last_error/processed` 只描述 app-owned owner pass。

所有声明的对话结算入口只进入一个 `DialogueSettlementQueue`、一个 actual worker。confirm/reject 的顺序固定为：`INSERT OR IGNORE` 固化 immutable winner → event identity 与 event 同事务 → object → derived → rebuild marker + stable audit → `applied=1` → 跨 session projection → exact-generation 解锚。卡片 action、legacy endpoint、锚关系与无锚 chat 的 speculation/insight/confusion settles 共用这条 ref 路径；只有 `applied=1` 可生成终态投影。protected façade 校验 actual worker Task + lifecycle nonce；worker 内嵌套 settle 由该 task 直接 `_apply_*`，不会 submit/inline dispatcher。API request、active child 与跨 job detached child 均不能写或冒充队外 producer。

`card_settlements` 不再保存 claim/lease/token/`seg_*`，也没有文件锁、takeover 或恢复 scanner。rebuild marker 仍使用同目录临时文件 `flush+fsync` 后原子替换；写盘失败会使 job 失败且 receipt 保持 `applied=0`，不会提前投影卡片。后续同 ref 显式重试采用原 winner，幂等 effect 补齐缺口。

对话内结算（锚归属 `support/contradict/revise/answer`）落库在**回复完成之后**——worker 还要跑归属判断和队列 job。因此桌面 Web 在回复完成后继续按 1/2/5/5/5/5/5 秒重读对话，直到卡片进入终态或用完 ~30 秒预算（与卡片 action 的 `CARD_ACTION_POLL_DEADLINE_MS` 同量级）；只在屏幕上确有未结算卡片时才轮询。少了这步，用户说完「我认可修正版」后卡片会一直停在「正在聊这条」直到手动刷新——真机浏览器 E2E 实测 8 秒预算会漏掉。

队列本身是进程内、非 durable 的：若进程在 `202` 后重启，尚未执行的 job 可以丢失，但 durable card/receipt 不会伪终态。popup、移动 Web 与桌面 Web 对 `202 processing` 按 `1s/2s/5s`（之后保持 5s）读取 `GET /api/chat/turns/{turn_id}`，总截止 30 秒；终态立即停止，超时、读取持续失败或页面 abort 显示本地 `retryable_error`，允许刷新或重试 action。三端的 active insights/认知更新区保持只读；CLI/OpenClaw 也不消费该 HTTP action 契约。

系统抛出的两个 gate 必须同时满足：距上次全局抛出至少 12 小时，且同 ref 的 `last_asked_at` / `deferred_until` 已超过 72 小时；两者持久化在 `memory/dialogue_confirmation_state.json`。用户主动 open 明确绕过这两个时间 gate，但疑惑仍受数据库 `clarifying <= 1` 约束。附着 turn 与用户 turn 同秒时，以 `(created_at,rowid)` 保证卡/问题在前；空消息校验与既有 `turn_id` 幂等检查均发生在附着前。

## 客户端入口约束（Wave D）

popup、移动 Web 与桌面 Web 只有 durable 对话中的假设卡片保留 confirm/reject/discuss/defer 主动动作，并共享上述按需轮询 helper；同步 `200` 不启动额外轮询。三端画像/认知更新区均只读。`openbiliclaw questions` 也只发 GET 并展示列表。`POST /api/insights/feedback` 仍为旧客户端保留并转发共同队列，但新客户端不再调用它；因此“对话是唯一主动 UI 确认入口”与 legacy 兼容同时成立。

## Runtime stream 保活与重连

`GET ws://.../api/runtime-stream` 在 20 秒没有业务事件时发送 `{"type":"runtime.heartbeat","sent_at":"..."}`。心跳与普通事件共用唯一 writer，避免并发 `send_json`；鉴权撤销仍在每次发送前和 15 秒 watchdog 中 fail closed。桌面 Web 收到心跳即确认“实时连接正常”，异常 close 则显示“实时流重连中”、记录 close code/reason，并按 3 秒节奏重连；页面进入后台时仍按 visibility 生命周期主动关闭，不把该主动关闭显示成后端离线。
