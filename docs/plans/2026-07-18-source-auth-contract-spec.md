# 平台来源接入契约统一 Spec — 一个绿灯只有一种含义

**Created:** 2026-07-18
**Scope:** 所有平台来源的「凭据存在性 / 登录态 / 验证强度」的后端契约、写入端点、验证动作，以及桌面 Web、插件 popup、移动 Web 三端的接入状态与凭据表单渲染。涉及 `api/app.py`、`api/models.py`、`sources/protocol.py`、`runtime/init_prereqs.py`、`web/desktop/assets/js/app.js`、`extension/popup/popup.js`、`web/js/app.js`。
**Out of scope:** 不改各平台的取数逻辑与 discover 策略；不改 `[network]` 代理策略本身（X / Reddit 代理脱管另立 issue，见 D12）；不新增平台；不动 `saved_sync` 的逐条目 `login_required` 账本（见 D10 的边界说明）。

## Goal

**当前失败**：`/api/sources/status` 用同一个 `state` 字段和同一个 `logged_in` 布尔，表达 7 个平台**证据强度完全不同**的登录结论。实测（2026-07-18，本地 8420）四个平台同时显示「接入：凭据已就绪」，但背后分别是：B 站数了 3 个 cookie 字段名（不联网）、小红书浏览器 72 小时内有心跳、知乎同理、Reddit 本地文件存在且未超 7 天（代码注释明说绝不联网）。X 显示「接入：接入可用」——文案不同但语义与前四者的「就绪」并无区别，只因后端返回 `ok` 而非 `ready`。抖音即使 cookie 完全有效也永远显示「状态待验证」——**2026-07-18 实测确认该机器的抖音 cookie 确实处于登录态，且该平台存在可用的活体探针（D11），当前显示属误报**。

用户无法从 UI 分辨"真的能用"和"只是填了个值"，这是误导而不只是不美观。

**量化目标**（全部可复现）：

| 指标 | 当前 | 目标 | 复现命令 |
| --- | --- | --- | --- |
| `sources_status()` 函数体量 | 424 行 | ≤ 140 行（聚合器 + 每平台独立 provider） | 见下方 `scripts/` 门 |
| 桌面 `app.js` **平台源设置区域内**的 per-platform 分支 | 1 处 | 0 处 | 同上 |
| 前端状态映射表副本数 | 2 份（desktop / popup，已漂移） | 1 份（后端下发） | `rg 'SOURCE_ACCESS_STATE\|SOURCE_STATUS_DOT'` |
| 有统一命名的 verify 路由的平台 | 0 / 7（能力上是 1/7，见 D7 修正） | canonical source-family registry 全覆盖（当时 7/7，当前 8/8） | `POST /api/sources/{k}/verify` ∩ `SOURCE_AUTH_PROVIDERS` ∩ `VERIFY_ACTIONS`；分母来自 canonical registry |
| 后端可活体验证的平台 | 1 / 7（仅 B 站，且藏在 `init-status` 等端点下） | 2 / 7（+ 抖音，D11 实测） | 见 Phase 1 映射表 |
| 凭据写入端点命名形态 | **5** 种（含 `PUT /api/config`，见 D5 修正） | 1 种 + 旧端点转发 | `openapi.json` 断言 |
| 承载平台源设置的前端 | 2（desktop + popup） | 2（移动端有意排除，见下） | `scripts/source_contract_metrics.py` 第 6 项 |

基线数字复现：

```bash
cd .worktrees/source-auth-contract
python3 scripts/source_contract_metrics.py     # Phase 0 交付，输出上表左列
```

**指标 2 的口径**：全文件扫描会得到 6 处，但其中 5 处在推荐卡渲染（`contentUrl` 拼 URL、作者主页链接），与登录态契约无关。把它们计入会让 Phase 4 被迫连推荐卡一起改造，范围失控。因此指标只统计**平台源设置区域**（`renderSourcesStatus*` / `renderSourceCredentialRows` 等由 `/api/sources/status`、`/api/sources/credentials` 驱动的渲染函数）内的分支，基线 1 处（`app.js:5831` 的 `key === "xiaohongshu"` 凭据文案特判）。推荐卡的 per-platform 分支另案处理。

## Design invariants (MUST hold in every phase)

1. **I1 单一真相源**：任一平台「凭据在不在」只能由一个函数回答。`resolve_*_cookie()` 家族之外不得再出现第二条读取路径。验证面：`tests/test_source_auth_contract.py::test_single_credential_resolver_per_platform` 用 AST 扫描 `api/` 与 `runtime/`，断言无旁路 `getattr(cfg.<platform>, "cookie")` 直读。

2. **I2 语义正交**：`enabled`（要不要调度）、`auth_required`（要不要凭据）、`credential`（凭据在不在）、`verification`（验证结论）四个维度互不覆盖。任一维度的取值变化不得改变其他三者。验证面：`test_auth_dimensions_are_orthogonal` 对 4×3×3×4 组合断言派生函数无交叉污染。

3. **I3 证据强度必须诚实**：`verify_method` 必须如实反映结论来源，`live_probe` 只允许在真的发起了网络请求时使用。**禁止为了 UI 好看编造验证结果**；无验证能力时必须如实标 `none`。反过来同样强制：**声称某平台"无法验证"之前，必须先做剥离对照实验**（完整凭据 vs 剥离登录 cookie 的游客态，同一签名器 / UA / 时刻），拿不出对照数据就不许写进 docstring——D11 里抖音"没有稳定 nav 端点"的断言正是这样以讹传讹了整个平台的状态。对照实验本身也必须防伪：`/aweme/v1/web/query/user/` 在两组返回相同 uid（设备级标识），只看"有没有返回 uid"会误判。验证面：`test_verify_method_matches_actual_io` — 用 `httpx` mock 断言声明 `live_probe` 的平台确实出网、声明 `local_file` / `browser_heartbeat` 的平台确实没出网。

4. **I4 前端零 per-platform 分支**：三端渲染只消费后端契约字段，不得出现 `key === "<platform>"` 形式的展示分支。验证面：`scripts/source_contract_metrics.py` 门值为 0，接入 CI。

5. **I5 写入即校验**：能结构校验的必须结构校验并拒绝落盘；能活体校验的必须活体校验；**都不能的必须在响应里显式声明未校验**，不得静默返回成功。同一份凭据经 `POST /api/sources/{k}/credential` 与 `PUT /api/config` 两条路径，校验强度必须一致。验证面：`test_write_paths_have_equal_validation`。

6. **I6 四端契约**（CLAUDE.md pitfall #5）：接入状态与凭据表单在插件 popup、桌面 Web、移动 Web、**setup 引导页**四个前端一致渲染，CLI 有等价查询命令；共享逻辑必须在后端。任何有意的排除必须写在 PR 描述里。

7. **I7 语义属性不得用语法代理判定**：判断"是不是展示映射""写不写凭据""验不验证""有没有源设置"这类语义问题时，**不许用变量名、路径词根、路由名、目录位置作为判据**。本 spec 的初版在 D5 / D6 / D7 / D10 四条上都栽在这里，而且是同一种错法：`PUT /api/config` 因为叶子词是 `config` 就没被算作凭据写入端点（实际上它是最大的一个）；B 站的活体验证因为路由不叫 `verify` 就被记成"没有验证能力"；两份 map 的漂移因为只比对了键名，测到的全是后端根本发不出的死键。验证面：任何以计数为门的指标（`scripts/source_contract_metrics.py`）都必须在注释里写明它用的是语法代理，并在 PR 里附一次语义复核——**门可以用代理，结论不行**。

## Current diagnosis

### D1. `state` 单字段承载四个正交维度，语义必然坍塌

`SourceStatusItem`（`src/openbiliclaw/api/models.py:604`）的 `state` 取值域有 12 个：`ok / ready / partial / stale / missing / unverified / login_required / error / expired / rate_limited / blocked / no_auth`。这一个字段同时被用来表达：

- 凭据存在性（`missing` / `partial`）
- 凭据有效性（`ok` / `expired` / `error`）
- 验证新鲜度（`stale` / `unverified`）
- 是否需要登录（`no_auth`）
- 来源开关（Bangumi 分支的 `disabled`，见 D10）

结果是 `ready` 与 `ok` 语义重叠——前者是"有凭据但没验证过"，后者是"验证过"，但两个前端各自把它们翻译成「凭据已就绪」和「接入可用」，用户读不出差别。**确认事实**，实测响应见 Goal 段。

### D2. 同一个 `logged_in=true` 背后有 6 种强度迥异的证据

`logged_in` 是 `state ∈ {ok, ready, no_auth}` 的便捷位（`models.py:627-628`）。逐平台实际含义：

| 平台 | 判定代码 | 实际含义 | 出网 |
| --- | --- | --- | --- |
| bilibili | `app.py:8805-8844` | cookie 串里数出 `SESSDATA`/`bili_jct`/`DedeUserID` 三个字段名 | ✗ |
| xiaohongshu | `app.py:8846-8909` | 插件上报 `web_session` 存在 + 72h 新鲜窗口（`app.py:8774`） | ✗ |
| zhihu | `app.py:8978-9089` | 插件上报 `z_c0` 存在 + 72h（`app.py:8775`），无心跳时回落 `zhihu_tasks` 历史 | ✗ |
| douyin | `app.py:8912-8934` | cookie 非空即 `unverified`，`logged_in` **恒为 False** —— 但该平台其实**有**可用探针，见 D11 | ✗ |
| youtube | `app.py:8938-8944` | 硬编码常量，它本就不需要登录 | ✗ |
| twitter | `app.py:8946-8977` | 上次真实请求未返回 401/403/429（`storage/x_health.py`） | ✓ 被动 |
| reddit | `app.py:9090-9202` → `sources/reddit_tasks.py:942` | 本地凭据文件存在且未超 7 天 TTL（`reddit_tasks.py:50`），docstring 明说 never invokes rdt | ✗ |

唯一的活体校验（B 站 nav 接口，`bilibili/auth.py:101`）**没有接进这个端点**，只喂 `/api/init-status`。**确认事实**。

**小红书 72h 窗口实测（2026-07-18）**：直接改写 `auth_state.xhs_login_state_at` 做边界验证（测后已还原）——71h → `ready`/`logged_in=True`，73h → `stale`/`logged_in=False` + "小红书登录态已过期，请连接插件刷新本地状态"。**TTL 逻辑本身正确且生效**。

但窗口内的语义是一张长期空头支票：后端**无条件**相信插件上报的那个 bool，直到 72 小时耗尽。用户若在窗口内登出小红书，UI 会继续显示「已登录」最长 72 小时，且后端**架构上无从发现**——它一个字节的小红书 cookie 都不存（`storage/database.py:11739` 只有 bool + 时间戳）。这与抖音形成正好相反的失败模式：

| | 小红书 | 抖音 |
| --- | --- | --- |
| 后端有凭据吗 | ✗ 只有 bool + 时间戳 | ✓ 完整 cookie（实测 57 项，含 `sessionid`/`sid_tt`） |
| 能后端活体验证吗 | **✗ 架构上不能** | **✓ 能（D11 实测）** |
| 当前做法 | 信 72h 窗口 | 永不验证 |
| 评价 | 受限于架构的合理妥协，但窗口过宽 | **可修的误报** |

因此 verify 动作对两者的形态必然不同：小红书只能走"请插件立刻重新上报"的往返（Phase 2 的 `browser_heartbeat` 分支，等待至多 5s），抖音则应直接出网探测。**这也是 `verify_method` 必须存在的实证依据**——两个平台的绿灯不可能是同一种绿。

### D3. B 站凭据双读取路径不一致 → 两个页面对同一份凭据给出相反结论（真 bug）

`runtime/init_prereqs.py:192` 只读 config：

```python
cookie = str(getattr(getattr(cfg, "bilibili", None), "cookie", "") or "").strip()
if cfg is None or not cookie:
    self._bili_value = "failed"
```

而 `sources_status`（`app.py:8809`）、`sources_credentials`（`app.py:9267`）、运行时客户端（`api/runtime_context.py:581`）都走 `resolve_runtime_cookie()`（`bilibili/auth.py:189`，config **或** `data/bilibili_cookie.json`）。

CLI `openbiliclaw auth login`（`cli.py:2321`）只写文件、不写 config.toml。**触发路径**：用 CLI 登录 → `/api/init-status` 报 `bilibili_not_logged_in`（引导页拦人），`/api/sources/status` 同时报 `ready`（设置页显示已就绪）。违反 I1。**确认事实**。

### D4. B 站写入校验自相矛盾

- `POST /api/bilibili/cookie`（`app.py:3405`）：`validate_with_bilibili` 默认 `True`（`models.py:454`），打 nav 校验，失败**拒绝落盘**，返回 `error_code ∈ {empty_cookie, cookie_invalid, validation_network}`。
- `PUT /api/config`（`app.py:10938`）：同一份 `bilibili.cookie`，只挡掩码回显和空值清除，**完全不校验**。

设置页手工粘贴走的正是后者。同一凭据两条写入路径两种强度，违反 I5。**确认事实**。

### D5. 凭据写入端点与存储位置各自为政

| 平台 | 写入端点 | 存储位置 | 写入校验 |
| --- | --- | --- | --- |
| bilibili | `POST /api/bilibili/cookie` ⚠️ 不在 `/api/sources/` 命名空间 | **config.toml 明文**（`config.py:386`） | 活体 / 无（依端点） |
| douyin | `POST /api/sources/dy/cookie` | env `OPENBILICLAW_DOUYIN_COOKIE` → `data/douyin_cookie.json` | **无，无脑存** |
| twitter | `POST /api/sources/x/cookie` | env `OPENBILICLAW_X_COOKIE` → `data/x_cookie.json` | 结构校验但**仍先落盘** |
| reddit | `POST /api/sources/reddit/cookie` | **项目外** `~/.config/rdt-cli/credential.json` | 结构校验 + **硬拒** |
| xiaohongshu | `POST /api/sources/xhs/tokens` + `/login-state` | DB `auth_state` 仅 bool + 时间戳 | N/A |
| zhihu | `POST /api/sources/zhihu/login-state` | 同上 | N/A |
| bangumi（未合并） | `POST /api/sources/bangumi/identity` | 令牌 | — |

`bilibili.cookie` 是唯一明文落 config.toml 的凭据，与代码库中反复出现的 `secrets never land in config.toml` 注释（`sources/x_auth.py:4`、`api/app.py:10993`）直接冲突。

**修正（2026-07-18 证伪复核）**：上表按路径词根统计得出"四种命名"，这个口径**漏掉了最大的一个**。`PUT /api/config`（`app.py:10864`）是第 5 种形态，而且**按平台数算它是代码库里最大的凭据写入端点**——一条路由写四个平台：

| 平台 | 行 | 落到哪 |
| --- | --- | --- |
| bilibili | `app.py:10947` | `cfg.bilibili.cookie` → config.toml |
| douyin | `app.py:11012` | `DouyinCookieManager.set_cookie(source="config-update")` → `data/douyin_cookie.json` |
| twitter | `app.py:11060` | `XCookieManager.set_cookie()` → `data/x_cookie.json` |
| reddit | `app.py:11144` | `sync_rdt_credential_from_cookie_header()` → rdt-cli 凭据库 |

它的路径叶子是 `config`，所以任何按词根白名单的扫描都看不见它。**设置页手工粘贴走的正是这条路**，D4 的校验缺失因此不是边角问题而是主路径问题。

另有两条被误分类：
- `POST /api/sources/xhs/tokens`（`app.py:8263`）与 `/observed-urls`（`app.py:8243`）写的是**逐笔记的内容访问令牌**，不是用户凭据——两者调用同一个 `_backfill_xhs_tokens`。
- `POST /api/sources/{xhs,zhihu}/login-state` 只写一个 **bool**。`storage/database.py:11723` 的注释明说："The browser extension deliberately sends only this boolean, never the `web_session` cookie value."

**修正后的事实**：真正的用户凭据写入端点是 4 条（`/api/bilibili/cookie` + `/api/sources/{dy,x,reddit}/cookie`）**加上 `PUT /api/config`**（覆盖同样这 4 个平台）= **5 种命名形态**。Phase 3 归一时 `PUT /api/config` 必须一并纳入，否则改完仍有一条绕过统一校验的主路径。

### D6. 前端两份状态映射表已经漂移

- 桌面 `web/desktop/assets/js/app.js:5737` `SOURCE_ACCESS_STATE`：14 键，**无** `syncing`。
- 插件 `extension/popup/popup.js:7077` `SOURCE_STATUS_DOT`（14 键，**无** `expired`）与 `:7093` `SOURCE_STATUS_LABEL`（15 键，含 `expired: "凭据失效"`）。

即 popup 收到 `expired` 会渲染红色语义的文案配灰色圆点（fallback `#9aa0a6`）。

**修正（2026-07-18 证伪复核）**：上面这个漂移**测的是死键**。后端实际只会发出 11 个状态（`app.py:8822-9198` 字面量 + `storage/x_health.py:52-56`）：`ok / ready / no_auth / unverified / missing / missing_cookie / expired_cookie / rate_limited / partial / stale / blocked`——`syncing`、`expired`、`login_required`、`error` **一个都发不出来**。三张表对这 11 个可达状态的标签**逐字节一致**。

真正**可达**的分歧是另外两个，原诊断没抓到：

1. **`no_auth` 与 `unverified` 在 popup 里同色**。desktop 区分 `no_auth → tone "public"`、`unverified → tone "pending"`（`app.js:5740-5741`），popup 两者都是 `#9aa0a6`（`popup.js:7081-7082`）。**这两个状态都真的会发**（YouTube 的 `no_auth` 在 `app.py:8941`，xhs/douyin/reddit 的 `unverified` 在 `:8888`/`:8927`/`:9098`）。于是插件里"这个源不需要登录"和"这个源状态不明"长得一模一样。
2. **未知状态的兜底文案不同**：desktop 渲染 `"状态未知"`（`app.js:5791`），popup 渲染**空字符串**（`popup.js:7133`）——插件上会出现一个没有任何说明的灰点。

另外副本数也低估了：还有**第 4 份映射在后端 Python 里**（`_x_state_detail`，`app.py:8779-8787`，把 X 的健康态映射成中文文案），任何只扫前端的统计都看不见它。相邻的 saved-sync 枚举另有 6 份展示映射（含 `saved-sync-core.js:138` 一个**完全匿名的内联对象字面量**），且与本枚举共用 `login_required` / `rate_limited` 两个键。

违反 I4。**确认事实（漂移真实存在，但可达面与原诊断所指不同）**。

### D7. 平台源没有任何验证动作，与模型设置形成刺眼对比

设置页「模型」tab 有三个按钮：`测试 LLM`、`测试 Embedding`、`测试备选 Provider`。「平台源」tab **一个都没有**（实测 DOM 枚举确认）。粘贴 cookie → 保存 → 没有任何回执告诉用户这份凭据能不能用。全仓无 `/api/sources/*/verify` 类端点（`openapi.json` 105 个端点中零命中）。

**修正（2026-07-18 证伪复核）**：路由确实不存在，但"没有验证能力"是错的结论——**B 站的活体验证早就有，只是没被叫作 verify，也没有 UI 入口**：

| 入口 | 出网调用 |
| --- | --- |
| `POST /api/bilibili/cookie`（`app.py:3481`） | `validate_cookie()`，且 `validate_with_bilibili` **默认 True**（`models.py:454`），每次插件同步都在活体验证 |
| **`GET /api/init-status`**（`app.py:2600`） | `prereqs.bilibili_check()` → `validate_cookie()`（`init_prereqs.py:237`，TTL 60s/10s） |
| `POST /api/init`（`app.py:2951`） | 同上 |
| CLI `auth login` / `auth status`（`cli.py:10609` / `:10627`） | 同上 |

顺带暴露一个设计异味：**`GET /api/init-status` 是只读 GET 却会出网**，引导页每次轮询都可能触发 B 站请求（TTL 兜着）。Phase 2 把验证收进显式 POST 后，这条隐式出网路径应当收敛。

**修正后的事实**：能力上是 1/7（B 站），不是 0/7；抖音的探针 D11 已写好但**尚未接线**（`douyin_login_probe.py:86`，零调用点）。所以 Phase 2 不是"从零造验证能力"，而是三件事：给已有能力一个统一名字、补齐其余平台、在设置页开一个入口。这降低了 Task 6 的风险，但也意味着**必须复用 `bilibili_check` 的 TTL 缓存而不是另起一套**，否则同一个凭据会有两条各自缓存的验证路径——那正是 D3 的翻版。

### D8. 协议层不含认证，且存在两套并行体系 —— 这是「没章法」的机械根因

`sources/protocol.py:48` 的 `SourceAdapter` Protocol 只有两个成员：`source_type` 与 `fetch`。**没有** `check_login` / `is_authenticated` / `validate_credential` 的统一契约。

更麻烦的是只有 3 个平台走 adapter registry（bilibili / xiaohongshu / twitter，接线于 `api/runtime_context.py:783/797/823`），另外 4 个（douyin / youtube / zhihu / reddit）走 `runtime/*_producer.py` 独立路径；`sources/web_adapter.py:25` 的 `WebSourceAdapter` 实现了协议但从未注册。

因此语义统一只能发生在最上层：`api/app.py:8789-9212` 一个 **424 行**的 if/elif 聚合器手工拍平 7 种异构状态。新增平台必须改这个巨型函数，且**没有任何机制强制它对齐** —— `docs/platform-source-integration.md:44` 其实已经写了登录判定原则，但无强制点，全靠手抄上一个平台。**确认事实**。

### D9. 登录检查方法散落 9 处，返回类型 5 种

`bilibili/auth.py:101` `validate_cookie → AuthStatus`；`bilibili/auth.py:65` `is_authenticated → bool`（仅判非空）；`sources/reddit_tasks.py:942` `local_reddit_credential_status → RedditCommandStatus`；`storage/x_health.py:157` `XSourceHealthStore.get → dict`；`storage/database.py:11739/11765` `get_{xhs,zhihu}_login_state → tuple[bool, str]`。douyin 与 youtube **没有**登录检查方法。**确认事实**。

### D10. 移动 Web 无平台源设置（四端契约缺口）

`src/openbiliclaw/web/js/app.js:100` 的 `openMobileSettings` 只暴露 saved-sync 自动开关，无任何平台源状态或凭据入口。移动端确实零调用 `/api/sources/*`（`web/js/api.js` 路径全枚举确认）。违反 I6 与 CLAUDE.md pitfall #5。

**修正（2026-07-18 证伪复核）**——"移动端完全没有"这个框定有三处不准：

1. **移动端其实已经在渲染 per-platform 登录态**，只是数据来自另一条链路：`web/js/views/saved.js:32` 把 `login_required` 映射成 `["需要登录", "warning", true]`，`:187` 渲染"请登录对应平台后重试。"，`PLATFORM_NAMES`（`:38`）覆盖全部 7 个平台。信号源是 saved-sync 任务结果，不是 `/api/sources/status`。**这正是 D10 提到的第二套登录判定在移动端的出口**——用户在手机上看得到"该平台需要登录"，却在同一个 App 里找不到任何地方去修它。
2. **移动端已经接通了凭据写入端点**：`web/js/api.js:134` 导出 `updateConfig` → `PUT /api/config`，也就是 D5 里那条写 4 个平台凭据的路由。目前只用来传 `{saved_sync: {...}}`，但管道是通的——Task 13 因此比预想的轻，不需要新端点。
3. **前端不止两个，是三个**：setup 引导页（`web/setup/index.html`，挂载在 `/setup`）也有平台源设置——7 平台启用清单 `INIT_SOURCE_OPTIONS`（`:265`）、B 站凭据存在性检查 `checkBili`（`:433`）、状态文案表 `INIT_REASON_TEXT`（`:274`），并轮询 `/api/init-status`。Phase 4 抽共享模块时必须把它算进去，否则统一完仍剩一份手抄副本。

另注：`saved_sync` 的逐条目 `login_required`（`saved_sync/adapters/bilibili.py:52`）是**第二套**登录判定，落在 `extension_native_save_jobs` 账本，完全不回流 `/api/sources/status`。本 spec 不合并这两套（收藏同步的登录态是逐条目的、语义确实不同），但 Phase 1 需在 detail 里可选透出，避免用户看到"来源已就绪"而收藏同步却报未登录。**确认事实**。

### D11. 抖音其实**有**干净的登录判定端点 —— 后端的核心假设已被实验推翻

`app.py:3577-3583` 的 docstring 是抖音"永远 `unverified`"的全部理由：

> Unlike Bilibili, Douyin direct-cookie discovery currently has no stable nav endpoint that cleanly
> distinguishes "logged out" from "soft anti-bot returned HTTP 200 with empty data".

**这个说法不成立。** 2026-07-18 用本机真实 cookie（`data/douyin_cookie.json`，57 项，含 `sessionid` / `sessionid_ss` / `sid_tt`）做对照实验，实验组 = 完整 cookie，对照组 = 剥离 12 个登录 cookie 后的游客态，同一签名器 / UA / 时刻，唯一变量是登录 cookie：

| 端点 | 实验组（已登录） | 对照组（游客） |
| --- | --- | --- |
| `/aweme/v1/web/user/profile/self/` | `status_code=0` + 非空 `user.uid` / `nickname` | **`status_code=8`,`status_msg="用户未登录"`** |
| `/aweme/v1/web/collects/list/` | `status_code=0` | **`status_code=8`,`status_msg="用户未登录"`** |
| `/aweme/v1/web/aweme/favorite/` | `status_code=0`，返回 7 条真实点赞 | `status_code=None`（结构异常，不适合做判定） |

区分信号不是"空数据"，而是明确的错误码 + 中文消息，恰恰是 docstring 声称不存在的那种干净信号。

**探针适用性实测**：`/aweme/v1/web/user/profile/self/` 连续 3 次调用 `status_code` 与非空 `uid`/`nickname` 完全稳定，延迟 260 / 299 / 428 ms（均值 329ms）。亚秒级、无副作用（只读自己的资料），适合作为 verify 探针。

**排除的错误假设**：`/aweme/v1/web/query/user/` 返回的 `user_uid`（12 位）在实验组与对照组**完全相同**，说明它是设备级标识（由 `ttwid` / `odin_tt` 驱动）而非账号级——初测曾误判此端点可用，对照实验推翻。选定端点时必须用剥离对照，不能只看"有没有返回 uid"。

**影响**：抖音的 `verify_method` 从 `none` 升级为 `live_probe`（与 B 站同级），`logged_in` 不再恒为 `False`。用户当前的抖音**确实处于登录态**，UI 却显示"接入：状态待验证"——这是可修的误报，不是平台限制。

### D12. 附带确认、本 spec 不修的缺陷

- **Bangumi 开关吃掉接入状态**：`feat/bangumi-source` 分支上，Bangumi 停用时接入 badge 显示「来源未启用」，把 `enabled` 塞进了 `state`，凭据情况不可见。其余 7 平台同样停用却正常显示。该分支未合并，Phase 1 的正交化天然修复它——合并时按新契约适配即可。
- **`x_cookie_sync_requested` 死代码**：扩展 `extension/src/background/cookie-sync.ts:565` 有处理分支，后端从不发送（`app.py:3883-3936` 只对 xhs/zhihu/bilibili/douyin/reddit 发）。
- **`auth_method="qrcode"` 死配置**：`config.py:35` 白名单接受，全仓无实现。
- **X / Reddit 脱离代理管控**：X 走 `twitter_cli` + `curl_cffi` 黑盒，Reddit 走 `subprocess.run(["rdt", ...])`，均不接 `[network]` 策略，`tests/test_network_proxy_isolation.py` 只 pin 了 bilibili/douyin/ollama。违反 CLAUDE.md pitfall #1，**另开 issue**。

## Priority classification

| Phase | 内容 | Tier | 理由 |
| --- | --- | --- | --- |
| 0 | 契约冻结测试 + 指标脚本 | **MUST** | 424 行聚合器无针对性测试，不先锁现状改必炸 |
| 1 | 后端状态正交化（**纯新增字段**，`state`/`logged_in` 行为不变） | **MUST** | 修 D1/D2/D3；零破坏，老客户端不受影响 |
| 2 | `POST /api/sources/{k}/verify` + 三端测试按钮 | **MUST** | 修 D7；用户当下最痛的点 |
| 3 | 凭据写入端点归一 + 写入即校验 | RECOMMENDED | 修 D4/D5；触及落盘路径，需 Phase 0 测试网兜底 |
| 4 | 前端共享渲染 + 表单 schema 驱动 | RECOMMENDED | 修 D6/I4；消灭两份漂移的 map |
| 5 | 存储归一（B 站 cookie 迁出 config.toml）+ 移动端补齐 | OPTIONAL | 修 D5/D10；破坏性最大，可独立延后 |

**依赖**：0 → 1 → {2, 3} → 4 → 5。Phase 2 与 3 可并行。

- **Wave A（可独立交付、零破坏）**：Phase 0 + 1 + 2。做完用户就能看到诚实的状态和可点的验证按钮，`state` 字段行为完全未变，任何老客户端不受影响。**在此停止是安全的**。
- **Wave B（触及写入与渲染）**：Phase 3 + 4。老端点转发保留，前端切换到新契约。
- **Wave C（破坏性）**：Phase 5。涉及凭据迁移，需要 config 迁移脚本与回滚预案。

## Phase designs

### Phase 0 — 契约冻结与指标基线

**交付**：`tests/test_source_auth_contract.py` + `scripts/source_contract_metrics.py`。

先用参数化测试锁住 7 个平台**当前**的 `(state, logged_in)` 输出——注入各平台的凭据前置状态（cookie 有/无/不全、心跳新鲜/过期/缺失、health 各态、rdt 文件各态），断言现有输出。这是后续所有重构的安全网。

`scripts/source_contract_metrics.py` 输出 Goal 表左列六个数字，CI 上作为门。

**验收**：新测试覆盖 7 平台 × 每平台 ≥3 个前置状态 = ≥21 个 case，全绿；指标脚本输出与本 spec Goal 表左列完全一致（424 / 1 / 2 / 0 / 4 / 2）。

### Phase 1 — 状态正交化（纯新增）

`SourceStatusItem` 新增 `auth` 子对象，**现有 `state` / `logged_in` / `detail` / `feed_paused` 字段行为一字不改**：

```python
class SourceAuthContract(BaseModel):
    auth_required: bool                      # youtube=False，其余 True
    credential: Literal["none", "present", "invalid"]
    credential_origin: Literal["config", "env", "data_file", "extension", "external_cli", "none"]
    verification: Literal["verified", "failed", "stale", "unverified"]
    verify_method: Literal[
        "live_probe",         # 真的出网校验（bilibili nav）
        "passive_health",     # 由真实请求的异常反推（保留能力）
        "browser_heartbeat",  # 插件上报的登录 cookie 存在性（xhs / zhihu）
        "local_file",         # 只读本地凭据文件（reddit）
        "task_history",       # 由历史任务结果反推（zhihu 回落）
        "none",               # 无法离线验证（douyin）或不需要（youtube）
    ]
    verified_at: datetime | None
    verify_ttl_seconds: int | None           # 该方法的新鲜窗口；None = 不过期
    can_verify_now: bool                     # 是否有可点的 verify 动作
```

聚合器拆成每平台一个 `_auth_<slug>(ctx) -> SourceAuthContract` 纯函数（放 `api/source_auth/` 新包），`sources_status()` 只做遍历。

**设计修正（实现期发现，2026-07-18）**：原计划的 `derive_legacy_state(contract)` **不可能实现**，而这个不可能本身是 D1 最强的证据：

```
platform   credential  verification   legacy state   logged_in
bilibili   present     unverified     "ready"        True
douyin     present     unverified     "unverified"   False
```

正交状态完全相同，旧结论截然相反——B 站因为"cookie 字段齐全"获得了信任推定，抖音因为其分支被写成永不声称成功而没有。任何以正交字段为输入的函数都不可能同时产出这两个答案；旧 `state` 携带的是新字段刻意丢弃的平台特定历史。

因此 Wave A 改为：provider 同时输出正交字段与**原样承袭**的 `legacy_state` / `legacy_logged_in`（保证承诺的逐字节零变化），另配 `check_legacy_consistency()` 断言两套视图**互不矛盾**（不是相等——`ready` 合法地对应 `verified` 或 `unverified`，因为旧值从不区分）。正交字段允许比旧字段**更准确**：B 站与抖音接上活体探针后 `verification` 变为 `verified`，而 `legacy_state` 仍是 `ready` / `unverified`。用户在 Wave B 前端切换后才看到这个升级——这正是 Wave A 零破坏的由来。`legacy_*` 字段在三端切换完成后删除。

**同时修 D3**：`init_prereqs.bilibili_check()` 改用 `resolve_runtime_cookie()`，与其余三处对齐。

**逐平台 `verify_method` 映射**（诚实性由 I3 测试强制）：

| 平台 | verify_method | ttl | 说明 |
| --- | --- | --- | --- |
| bilibili | `live_probe` | 60s ok / 10s fail | 复用 `init_prereqs` 的 TTL 缓存，首次接入 sources/status |
| **douyin** | **`live_probe`** | 60s ok / 10s fail | **D11 实测升级**：`/aweme/v1/web/user/profile/self/`，已登录 `status_code=0`+非空 uid，未登录 `status_code=8` "用户未登录"，均值 329ms |
| twitter | `live_probe` | None | `twitter-cli.fetch_me()` 只读账户状态；后台请求仍写入 `x_source_health` |
| xiaohongshu | `browser_heartbeat` | 72h | 沿用 `_xhs_login_fresh_hours`；后端零 cookie，**架构上无法后端活体验证**，verify 只能走插件往返 |
| zhihu | `browser_heartbeat` → `task_history` | 72h / None | 回落路径需在字段上体现，不能冒充心跳 |
| reddit | `local_file` | 7d | 沿用 `_RDT_CREDENTIAL_TTL_SECONDS` |
| youtube | `none` + `auth_required=False` | — | 公开源，唯一合法的 `none` |

**验收**：Phase 0 全部 case 输出不变（`state`/`logged_in` 逐字节一致）；新增 `test_auth_dimensions_are_orthogonal` 与 `test_verify_method_matches_actual_io` 全绿；`sources_status()` ≤ 140 行；CLI 登录后 `/api/init-status` 与 `/api/sources/status` 对 B 站结论一致（D3 回归测试）。

### Phase 2 — verify 动作

```
POST /api/sources/{slug}/verify
→ 200 SourceAuthContract + {changed: bool, message: str}
```

按 `verify_method` 分派：`live_probe` 真发请求（绕过 TTL 缓存，X 使用只读 `fetch_me()`）；`passive_health` 保留给没有安全主动探针的来源，返回最近一次真实请求的结论并标注时间；`browser_heartbeat` 通过 WS 向插件发 `*_sync_requested` 并等待至多 5s；`local_file` 重读文件；`none` 立即返回 `verification="unverified"` 并在 `message` 说明原因。

**并发与限流**：每平台 10s 去抖，避免用户狂点触发风控。

三端各加一个「测试连接」按钮，渲染同一份返回。

**验收**：7 平台全部可点（含 douyin 返回诚实的"不可离线验证"）；`live_probe` 平台在 mock 下断言确实出网；10s 内重复调用返回缓存且 `changed=false`；三端按钮共用后端返回，无本地文案硬编码。

### Phase 3 — 写入端点归一 + 写入即校验

```
POST /api/sources/{slug}/credential
body: {kind: "cookie" | "token" | "login_state", value: str | bool, source: str}
→ 200 {accepted: bool, error_code: str | None, auth: SourceAuthContract}
```

统一流程：**结构校验 → 落盘 → 广播 → 立即执行 Phase 2 的 verify → 把 verify 结果一并返回**。这同时消除了"保存后零回执"。

老端点（`/api/bilibili/cookie`、`/api/sources/{dy,x,reddit}/cookie`、`/api/sources/xhs/tokens`、`/api/sources/zhihu/login-state` 等）保留为内部转发，不改变其现有响应结构。

**修 D4**：`PUT /api/config` 中的凭据字段改为委托同一校验函数，与 POST 路径强度一致。

**验收**：`test_write_paths_have_equal_validation` 对每个平台断言两条路径对同一无效输入给出相同 `error_code`；老端点契约测试全绿（响应结构不变）；无效 cookie 在 douyin 之外的平台不再静默落盘。

### Phase 4 — 前端共享渲染 + 表单 schema 驱动

`GET /api/sources/credentials` 每项增加 `form` 描述符：

```python
class CredentialFormSpec(BaseModel):
    kind: Literal["cookie_textarea", "token_input", "extension_only", "none"]
    label: str
    placeholder: str
    env_var: str | None
    required_keys: list[str]        # ["SESSDATA", "bili_jct", "DedeUserID"]
    actions: list[FormAction]       # verify / clear / open_login_window …
    help_text: str
```

抽 `web/shared/source-status.js`（后端静态目录下，desktop 与 popup 同时引用），删除两份 `SOURCE_ACCESS_STATE` / `SOURCE_STATUS_DOT`。状态文案与色调由后端 `auth` 契约驱动。

**验收**：指标脚本 per-platform 分支 = 0、map 副本 = 1；三端截图对照同一状态渲染一致；`no_auth` 有独立视觉（不再与 `unverified` 共用灰色）。

### Phase 5 — 存储归一与移动端补齐（OPTIONAL）

B 站 cookie 迁出 config.toml 到 `data/bilibili_cookie.json`（与 douyin/x 对齐），config 侧只保留 `cookie_env`；提供启动时一次性迁移 + 回滚开关。移动 Web 补平台源状态只读视图与凭据表单。

**验收**：迁移后 `config.toml` 无明文凭据；旧 config 自动迁移且可回滚；移动端三端截图一致。

## Expected impact

| Lever | Measured effect |
| --- | --- |
| Phase 1 | 用户能分辨"验证过"与"只是填了"；D3 的双页面矛盾消失；**抖音从"永远待验证"翻转为可活体验证（D11）**，后端可活体验证的平台 1 → 2；聚合器 424 → ≤140 行 |
| Phase 2 | 凭据从"填完不知道对不对"变为一键可验；canonical registry 中每个平台都有回执（Phase 当时 7/7） |
| Phase 3 | 无效凭据不再静默落盘；两条写入路径校验强度统一 |
| Phase 4 | 前端 per-platform 分支 6 → 0；map 副本 2 → 1，D6 漂移根除 |
| Phase 5 | config.toml 不再存明文凭据；四端契约补齐 |

## Documentation obligations

按 CLAUDE.md「Documentation Requirements」：

- `docs/modules/api.md` — `/api/sources/*` 新端点与 `SourceAuthContract` 字段表（Phase 1/2/3）
- `docs/modules/config.md` — Phase 5 的 `[bilibili].cookie` 迁移
- `docs/platform-source-integration.md` — **§0 增加「接入契约必填项」小节**（`auth_required` / `verify_method` / 表单描述符），§4 配置页清单同步新端点。这是本 spec 的强制对齐点：新平台不填这些字段就无法通过 Phase 0 的参数化测试
- `docs/changelog.md` — 每个 Phase 一条 bullet
- `docs/architecture.md` + `docs/spec.md` §3 + README CN/EN 架构图 — 仅当 Phase 1 的 `api/source_auth/` 新包构成模块边界变化时同步
- `docs/modules/extension.md` — Phase 2 的 WS `*_sync_requested` 等待语义、Phase 4 的共享渲染模块
