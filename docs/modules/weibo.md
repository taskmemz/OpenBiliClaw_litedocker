# 微博来源

> 微博是 OpenBiliClaw 的第十一个正式平台来源。公开发现仍由后端匿名访客会话完成；首次画像可选地通过浏览器扩展登录态导入本人收藏、关注和互动事件。后端只接收布尔登录心跳与规范化事件，不接收或保存微博 Cookie。

## 能力边界

| 能力 | 当前行为 |
|---|---|
| 搜索 | `search`：从统一关键词 planner claim 普通关键词，读取公开微博并写入统一待评估池 |
| 热搜 | `hot`：先读取热搜词，再以热搜词搜索真实微博；热搜词本身不是内容候选 |
| 作者 | `creator`：只使用本轮 search / hot 已发现的作者 UID，读取其公开微博 |
| 相关推荐 | 未实现；没有把标签再搜索或作者复用伪装成 `related` |
| 登录 | 公开发现无需登录；初始化本人事件需要在当前浏览器登录微博并连接扩展。扩展在微博同源任务页读取 `/api/config` 的登录与 uid 证据 |
| Cookie | 后端不读取、不接受、不持久化用户微博 Cookie；扩展只上报布尔登录状态，个人事件在微博同源页面内完成读取 |
| 初始化画像 | 支持。guided init 可导入收藏、关注和互动事件；必须有正向 uid 才接受个人事件，并按账号绑定防止混号 |
| 行为采集 | 仅支持初始化/按需的只读 bootstrap 任务；不监听普通微博浏览，也不做持续增量刷新 |
| 平台写入 | 不支持收藏、点赞、评论、关注或其他微博写操作；通用本地收藏 / 稍后看只保存 canonical membership，并立即标记为 `unsupported/local_only_source`，不创建 native-save task |

这条边界是有意设计的：微博把“公开发现”和“本人信号导入”分成不同能力。登录态只允许同源只读任务，不把后端升级成用户账号代理，也不把热搜标题当作可推荐内容。

## 数据流

```text
Soul profile / unified KeywordPlanner
            │
            ├── search ──────────────┐
            │                        │
weibo.com hotSearch ── hot words ── search public posts
            │                        │
            └──────── authors seen in this round ── creator posts
                                             │
                                     Weibo normalizer
                                             │
                            discovery_candidates(pending_eval)
                                             │
                                     CandidateEvalCoordinator
                                             │
                                      content_cache

Guided init (explicit 微博 opt-in)
            │
  browser task: favorites / following / mentions
            │  (same-origin, current login, positive uid)
            ▼
       weibo_tasks ── staged task-result ── account-scoped profile events
```

三个分支最终都只产出真实微博正文，分别标记为 `weibo-search`、`weibo-hot`、`weibo-creator`。producer 将它们按 strategy 分组调用 `DiscoveryCandidatePipeline.enqueue_candidates()`；API daemon 下评估由唯一的 `CandidateEvalCoordinator` 认领，手动 CLI 路径可立即 drain。微博 producer 不写自己的阈值；普通候选统一由 `discovery/admission.py` 的全局 policy floor（默认 `0.60`）裁决。

个人 bootstrap 由 `weibo_tasks` 持久化队列承载。扩展任务页先读取 `/api/config`，确认当前会话已登录并能解析 uid，再通过微博移动 H5 的同源只读接口读取收藏（`/api/container/getIndex?containerid=230259`）、关注（`/api/friendships/friends`）和 `@我的` / 评论（`/message/mentionsAt`、`/message/mentionsCmt`）；旧接口只作为兼容性候选，不会把 404 当成空数据。扩展只回传去 HTML 后的正文、作者、URL、时间和计数。API 在 staged final 前按 `weibo:<scope>:<item_id>` 去重，绑定首个确认账号，随后把收藏映射为 `favorite`、关注映射为 `follow`、mentions 映射为 `comment`，供本次画像分析和事件账本使用。账号切换必须先清理/重置旧 bootstrap 状态，不能把两个账号的事件混在一个画像里。

### search

正式 runtime 优先从 `KeywordFetchCoordinator` claim `platform="weibo"` 的 `regular` 关键词。关键词只有在请求成功、归一化成功且对应候选最终被统一 pipeline 接受后才标记为 `used`；上游明确返回空结果时标记为 `failed`。timeout、network、5xx、schema 漂移、归一化异常以及尚未执行的 claim 都会 rollback，避免把未交付的词消耗掉；一轮里先成功后失败时，只结算已成功 handoff 的分组。planner claim 池暂时为空时会从 Soul profile 回退少量确定性关键词；画像值只接受真实字符串，dict / list 不会被 `str()` 后发往上游。

每个搜索响应可能同时包含直接 `mblog` card 和嵌套 `card_group`。client 防御性递归展开 card group；normalizer 丢弃缺少稳定微博 ID 或正文的条目，绝不为 schema 缺口合成内容。

### hot-as-seed

`GET https://weibo.com/ajax/side/hotSearch` 只提供热搜种子。producer 对种子做去重和数量上限后，逐个调用与 search 相同的公开微博搜索接口。归一化时保留热搜原始排名为 `source_rank`，但候选的 `content_id`、正文、作者、链接和互动数都来自搜索到的真实微博。

因此：

- 热搜词不会作为 `content_type="topic"` 或合成卡片写入候选池；
- 一个热搜词搜不到真实微博时，该种子只产生空结果；
- 热搜接口成功不等于 discovery 有产出，调度生产性只认实际 enqueue 的微博。

### creator

creator 分支不维护用户订阅，也不猜账号。它只从同一轮 search / hot 已归一化的帖子中提取作者 UID，再读取 `containerid=107603<uid>` 的公开微博。固定执行顺序为 `search → hot → creator`，所以 creator 冷启动没有作者种子时会如实返回 `no_creator_seeds`。配置 API 不接受 creator-only；桌面与插件设置会自动同时选中 search，旧 TOML 的 creator-only 也会在加载时修复为 `search + creator`。

这能扩大同一兴趣方向的内容深度，同时避免永久跟踪账号或引入额外身份数据。

## 匿名访客会话

微博移动 H5 container 接口在游客态仍要求匿名 `SUB`。`WeiboClient` 首次执行 search / creator 时主动完成以下只读流程：

1. GET `https://visitor.passport.weibo.cn/visitor/visitor`，解析 `request_id`、`return_url`，以及页面实际声明的动态 callback / version；
2. 使用上述 callback / version POST `https://visitor.passport.weibo.cn/visitor/genvisitor2`，只解析与 callback 精确匹配的 inert JSONP 包装并取得匿名 `SUB`；
3. 只在当前 `WeiboClient` 实例内存中保存该值，并仅随移动 H5 请求发送；
4. 访客态被拒时清空并最多刷新一次，避免无限刷新或认证风暴。

client 自建的 `httpx.AsyncClient` 使用 `trust_env=False`，与国内直连来源的网络边界一致。即使测试或调用方注入了带 `Authorization` / `Cookie` 默认头的 client，每次请求也会先剥离这些头，只加入本实例匿名访客值，防止误把已登录会话带入匿名 discovery。

热搜接口不需要该访客 Cookie，只发送公开页面 `Referer: https://weibo.com/`。匿名访客值仍只在 client 内存中存在；登录态初始化另走下节的布尔 heartbeat 与浏览器任务，不复用匿名 Cookie。

## 登录态初始化

登录态不是公开 discovery 的前置条件，而是 `profile` / `bootstrap` 能力的前置条件。初始化页面或 CLI 选择微博后：

1. 扩展通过 `chrome.cookies` 只上报是否观察到登录 Cookie（`SUBP` + `ALF`）；游客 `SUB` 不算登录凭据，后端只保存布尔值和时间戳。
2. API `/api/init` 读取本地 heartbeat，未就绪时返回 `no_profile_signal_sources`（微博单独被选中）或把微博从本次画像来源中移除并保留公开发现。
3. 已就绪时，后端排队 `bootstrap_events`；service worker 创建隐藏的 `m.weibo.cn` 任务页，content script 在同源环境读取收藏、关注、mentions。任务不抓取页面 HTML、不监听普通浏览、不调用点赞/收藏/关注/评论写接口。
4. 任务结果必须包含当前 uid。后端把账号绑定到 bootstrap state，拒绝不同 uid 的后续结果；结果先 staged，再做事件去重与导入，因此扩展重试不会重复画像信号。

个人事件是 `init-only`：本版本没有定时增量刷新。用户需要补齐新行为时可重新运行微博初始化；公开内容发现仍由独立的匿名 producer cadence 运行。

## 归一化契约

`sources/weibo.py::weibo_post_to_content()` 将 H5 `mblog` 转成统一 `DiscoveredContent`：

| 上游字段 | 统一字段 |
|---|---|
| `id` / `mid` / `idstr` | `content_id`，平台身份为 `weibo:<id>` |
| `text_raw` / `text` | 清洗 HTML 后的 `body_text`；首行裁剪为 `title` |
| `user.id` / `screen_name` | 作者 UID / `author_name` |
| `bid` + UID | `https://weibo.com/<uid>/<bid>`；缺失时回退移动详情 URL |
| `pics` / `page_info.page_pic` 等 | 首个安全 HTTPS 图片 `cover_url` |
| `topic_struct` / 正文 `#话题#` | 去重后的 tags |
| `attitudes_count` | `like_count` |
| `comments_count` | `comment_count` |
| `reposts_count` | `share_count` 和 `retweet_count` |
| `reads_count`（仅上游真实提供时） | `view_count` |
| `created_at` | 仅在可解析且不晚于当前时间时写 UTC RFC3339 `published_at`（`Z` 后缀）；无效、未知或未来时间留空 |
| 热搜种子排名 | `source_rank`，仅 `weibo-hot` 分支存在 |

内容类型固定为 `post`。上游 `mblog` 确实带 `reads_count` 时才映射 `view_count`；字段缺失时浏览量保持 0，绝不把热搜 `num`、点赞或转发数冒充浏览量。`favorite_count` 与 `danmaku_count` 是当前公开 schema 的结构性缺失，固定为 0 且前端不渲染占位。HTML 解析只提取可见文本，末尾 UI 控件“全文”会被移除；缺 ID / 空正文条目被跳过。

微博图片 host（`*.sinaimg.cn`）进入共享图片代理白名单，并按国内 CDN 直连，不继承海外代理配置。真实图床在共享浏览器 UA 下会校验防盗链，因此代理只在当前 redirect 目标仍属于 `sinaimg.cn` 时附 `Referer: https://weibo.com/`；跳转到其它白名单 CDN 后重新生成请求头，不传播微博 Referer。

## 调度、预算与退避

默认配置：

```toml
[sources.weibo]
enabled = false
source_modes = ["search", "hot", "creator"]
daily_search_budget = 60
daily_hot_budget = 10
daily_creator_budget = 30
request_interval_seconds = 3
min_interval_minutes = 10

[scheduler.pool_source_shares]
weibo = 1
```

- 来源默认关闭；关闭时保存的 share 不占有效候选池 quota。
- `source_modes` 只能包含 `search`、`hot`、`creator`，且至少一项；creator 必须同时启用 search 或 hot。API 拒绝 creator-only，旧 TOML 加载时自动补 search。
- daily budget 按 UTC 日和分支分别记账，只计算全局去重、最终 limit 与候选 pipeline 过滤后实际保留的条目；`0` 表示不设该分支日上限，负数会被配置 API 拒绝。
- `request_interval_seconds` 是 client 内串行请求的最小间隔；`min_interval_minutes` 是 producer 最近一次**实际保留候选**后的运行地板，空轮或全部被 pipeline 拒绝的轮次不会锁死完整间隔。
- 空轮另有独立、持久化的短退避：上游明确空结果 300 秒、请求成功但 pipeline 零保留 120 秒、基础设施 / schema / normalizer 故障 60 秒；它们不会冒充生产性 cadence，也不会与 HTTP 429 的全局 cooldown 混用。
- `--force` 绕过 producer cadence 与上述 outcome 短退避，方便人工诊断；它仍不绕过日预算、HTTP 429 cooldown 或份额感知 pool gate。
- producer 在微博族低于有效 share 时可越过“全局池已满”闸，先给欠份额来源补 raw material；最终是否入正式池仍由统一评估和 admission 决定。

两类 ledger 各司其职：`weibo_discovery_runs` 按 mode 保存实际执行的 `units / discovered / reason / error_code`，其中 `units` 只计最终保留候选，供 UTC 日预算与本地来源状态；`weibo_discovery_state` 保存 HTTP 429 的全局 `cooldown_until`，以及按 outcome 分类的短退避截止时间。只有整轮实际保留至少一条候选才写共享 `source_producer_runs`，它只约束跨重启 cadence，不参与来源健康判定；`last_run_at` 只是无数据库构造下的进程内 fallback。

### 限流与 schema 漂移

`WeiboClientError` 提供稳定错误码，控制面不泄露 Cookie 或完整上游响应：

- HTTP `429` 映射为 `rate_limited`，读取并限制 `Retry-After` 到最多 24 小时；producer 持久化 cooldown，期间定时与手动 discovery 都返回 `rate_limited`，不继续打上游。当前不会仅凭成功 HTTP 响应里的任意提示文案推断限流。
- 访客态拒绝会刷新匿名 session 一次；仍失败则返回 `visitor_rejected`，不会要求用户粘贴 Cookie。
- 短暂 5xx / transport 错误最多做一次有界重试。
- 每个 endpoint 先校验响应 MIME：JSON API 只接受 JSON，visitor 入口只接受 HTML/XHTML，visitor JSONP 只接受 JavaScript；错误或缺失的 `Content-Type` fail closed。
- search 只有 `total=0`，或 `cards=[]` 且没有 `total`，才属于有证据的 empty；非空未知 card、`total>0` 的空 cards，以及 non-empty hot 列表里全是 malformed row 都返回 `schema_changed`。cards、hot data 等关键结构不再符合契约时同样 fail closed。系统保留同轮已经成功抓到的 partial items，记录稳定错误并等待后续 tick；不会把未知结构猜成候选，也不会偷偷切换到账号 Cookie 或浏览器 DOM。

这就是这里的“fallback”边界：会话失效可刷新一次、短暂传输失败可重试一次、同轮成功内容可保留；结构变化本身必须 fail closed，等待适配器更新。

## CLI

分支级只读 smoke：

```bash
openbiliclaw discover-weibo "大模型"
openbiliclaw discover-weibo-hot
openbiliclaw discover-weibo-creator 1234567890
```

三条命令直接调用匿名 client，便于验证真实响应与归一化，不要求先启用来源，不写 memory、不更新画像，也不执行任何微博写操作。

正式补池：

```bash
openbiliclaw discover --source weibo
openbiliclaw discover --source weibo --limit 20 --force
```

正式路径要求 `[sources.weibo].enabled=true`，按配置的 modes、预算、cadence、cooldown 和 pool share 执行，并进入统一 candidate pipeline。`--strategy` 对微博无效；分支由 `source_modes` 决定。

## API 与来源状态

微博复用平台中立控制面；个人 bootstrap 另外提供 capability-specific task endpoints：

| 端点 | 微博字段 / 行为 |
|---|---|
| `GET /api/config` | `sources.weibo` 全部字段与 `scheduler.pool_source_shares.weibo` |
| `PUT /api/config` | 校验并保存 enabled、modes、预算、请求间隔、运行间隔和 share，随后走统一热重载 |
| `GET /api/sources/status` | 本地读取 discovery health 与微博 capability auth；`discover` 始终匿名 ready，`profile/bootstrap` 根据最近浏览器 heartbeat 显示 login-required / ready；不会现场访问微博 |
| `GET /api/sources/credentials` | 显示“微博浏览器登录态”，不导出 Cookie；可写 kind 只有 `login_state`，实际由扩展上报布尔值 |
| `POST /api/sources/weibo/credential` | 接受 `{kind:"login_state",value:boolean}`，写入本地 `weibo_login_state` 时间戳并返回 capability receipt |
| `GET /api/sources/weibo/next-task` | 扩展领取有界 `bootstrap_events` 任务 |
| `POST /api/sources/weibo/task-result` | 接收规范化收藏/关注/mentions 结果，按 uid 绑定、staged、去重后导入画像事件 |
| `POST /api/sources/weibo/verify` | 使用浏览器 heartbeat 能力，不读取或接收 Cookie |

`SourceStatusItem.enabled` 与运行状态分离。为了兼容旧客户端，legacy `state` 保持 `no_auth`；正交 auth 契约为 `auth_required=false`、`credential=none|present`，并在 `capabilities` 中分别表达匿名 discover 与登录 required 的 profile/bootstrap。收到扩展心跳后 `verify_method=browser_heartbeat`，尚未收到心跳时诚实返回 `verify_method=none`；`logged_in=true` 只表示公开发现不被登录前置阻断，不代表持有用户微博账号。

`discovery_state` 独立返回 `disabled / unverified / ready / partial / error / rate_limited`，所以匿名 auth 仍可诚实显示“无需登录”，首页同时能把最近发现失败或 cooldown 列为 actionable issue。`feed_paused=true` 只在持久化限流 cooldown 生效时出现，并保留给旧前端作为告警 fallback。`detail` 由上述本地 run / cooldown 记录组装，表达未启用、尚未运行、限流退避或 recent run health；`empty` 与 `ok` 都属于“公开路径可用”的 ready 健康态，状态文案不会逐字回显 `empty`。producer 的即时 skip（例如 cadence throttled 或 pool full）不会写 `weibo_discovery_runs`，因此状态页不会把它伪装成一次上游健康结果。状态请求本身始终零上游 I/O。

## 浏览器扩展与初始化边界

微博在三端来源设置、来源状态、平台身份、作者、文字卡片与本地收藏中出现；平台筛选 / 来源计数当前只属于桌面 Web。移动 Web 与 extension popup 对所有来源都没有 per-platform filter，因此这里是产品级显式排除，不伪造一个只对微博生效的筛选器。三端无封面微博都采用内容驱动的文字卡；popup 的窄屏 surprise 导航允许换行并保留触控命中区，不再强制 16:9 空白槽。扩展侧明确支持：

- `weibo.com`、`m.weibo.cn` host permission 与微博 content script；
- `/api/sources/weibo/next-task` / `task-result` 任务桥；
- 只上报布尔登录态的 cookie-sync、同源 `/api/config` + `/api/account/getuid` 身份确认；
- guided init 来源选项以及收藏、关注、mentions 三类首轮画像事件。

它仍然没有普通微博页面行为监听、response tap、Cookie 回写或平台写操作。打开微博推荐卡只是普通外链导航；OpenBiliClaw 不观察用户随后在微博页面内做了什么。微博也没有 native-save adapter / executor；推荐卡的收藏与稍后看只保存本地 membership，并立即进入 `unsupported/local_only_source` 终态，不创建同步 task，也不展示单项 / 批量同步或重试动作。


## GitHub 调研与许可证边界

接入前对社区实现做了“接口事实与响应形状”调研，生产代码为独立实现：

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) / [微博 client](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/client.py)：只参考移动搜索 client、card / `card_group` 形状等可观察事实；其 [NON-COMMERCIAL LEARNING LICENSE 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE) 代码没有复制、改写或作为依赖引入。
- [RSSHub](https://github.com/DIYgod/RSSHub) / [微博 utils](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/weibo/utils.ts)：只参考游客 Cookie 失效重试和转发链递归等行为边界；[GNU AGPL-3.0](https://github.com/DIYgod/RSSHub/blob/master/LICENSE) 代码没有复制、改写或作为运行时依赖引入。OpenBiliClaw 的访客获取是自行实现的 HTTP visitor 流程，不是 RSSHub 的 Playwright 实现。
- [nghuyong/WeiboSpider](https://github.com/nghuyong/WeiboSpider) / [字段归一化参考](https://github.com/nghuyong/WeiboSpider/blob/master/weibospider/spiders/common.py)：只用来交叉核对 `mblog` 中稳定 ID、作者、正文和互动数字段的公开命名；当前主分支的 [LICENSE](https://github.com/nghuyong/WeiboSpider/blob/master/LICENSE) 经 GitHub License API 核验为 MIT。OpenBiliClaw 没有引入其 Scrapy 实现或复制解析代码。
- [dataabc/weiboSpider](https://github.com/dataabc/weiboSpider) / [微博字段文档](https://github.com/dataabc/weiboSpider/blob/master/README.md#微博信息)：只用来交叉核对公开微博字段命名与 schema，不依赖其抓取器。核验时该仓库主分支没有 `LICENSE` 文件，`setup.py` 也未声明 license，因此这里按“许可证未声明”处理，不能把它写成 MIT 或复用代码。
- [Yooki-K/weibo-mcp-server](https://github.com/Yooki-K/weibo-mcp-server) / [热搜实现](https://github.com/Yooki-K/weibo-mcp-server/blob/main/src/weibo_mcp_server/server.py)：只用来交叉核对无 Cookie 的 `https://weibo.com/ajax/side/hotSearch` 与 `data.realtime` shape；项目为 [MIT](https://github.com/Yooki-K/weibo-mcp-server/blob/main/LICENSE)，OpenBiliClaw 不依赖其服务。

许可证更宽松的参考也不改变项目的 clean-room 边界：我们依据真实请求对照和公开响应自行编写 client、parser、normalizer 与测试 fixture，不搬运第三方实现代码。特别是，评论、转发链抓取和需要账号 Cookie 的能力都没有顺手带入当前来源。

## 关键文件与测试

- `src/openbiliclaw/sources/weibo_client.py` — 匿名访客 client、节流、错误分类、响应 shape 校验
- `src/openbiliclaw/sources/weibo.py` — HTML / card / `mblog` 防御性归一化
- `src/openbiliclaw/runtime/weibo_producer.py` — 三分支编排、预算、cadence、cooldown、pool gate、candidate enqueue
- `src/openbiliclaw/sources/weibo_tasks.py` — 登录态 bootstrap 任务队列、账号绑定、事件转换
- `extension/src/content/weibo/task-executor.ts` / `extension/src/background/weibo-task-dispatcher.ts` — 同源只读个人事件任务
- `tests/test_weibo_client.py` — visitor、header stripping、search / creator / hot、嵌套 cards、retry / 限流 / schema
- `tests/test_weibo_producer.py` — hot-as-seed、同轮 creator、预算、关键词账本、partial / cooldown、统一候选池
- `tests/test_weibo_wiring.py` — 配置、平台注册表、runtime、API、CLI、图片代理与 guided-init wiring 契约
- `tests/test_weibo_contract.py` — capability-specific auth、任务桥、精确 mapping 与不适用能力的 exclusion nodeid
- `tests/fixtures/weibo/*.redacted.json` — success、empty 与 schema-drift 的真实脱敏响应证据
- `docs/platform-source-contract.weibo.toml` / `docs/platform-source-acceptance.weibo.md` — 可执行来源契约与验收台账
