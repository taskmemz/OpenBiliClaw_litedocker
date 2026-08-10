# 平台来源接入：历史失败教训索引

这份索引解释 `platform-source-integration.md` 中硬门禁的来源。它不是让新来源照抄某个旧平台，而是帮助执行者按能力找到真实失败前例，再回到当前代码和测试确认今天仍适用的约束。

## 怎么使用历史证据

1. 先按来源契约拆出 transport、auth/identity、browser task、bootstrap/incremental、discover、storage、surface、release 等能力。
2. 每项能力分别找首次接入与后续修复；优先看 commit/PR/issue 的最终代码和回归测试，本地 Codex session 只作线索。
3. 不把旧实现当规范。检查当前文件路径、registry 和产品边界；历史注释、平台数量和已知缺口可能已经过时。
4. session 中可能含本机路径、账号、Cookie、token、响应正文或用户数据。只提炼脱敏故障形态，禁止复制秘密或把原始 session 提交进仓库。
5. 把得到的规则写进 contract gate、参数化测试或 artifact preflight。只有“记住这次别漏”不算回灌。

建议取证顺序：

```bash
git log --all -E --date=short --format='%h %ad %s' --grep='<slug|capability>'
git show --stat --oneline <commit>
rg -n '<slug|registry|task field>' src extension tests docs
```

有 GitHub/本地 session 能力时再补 PR、issue 和 session；所有判断仍需由当前代码、测试或脱敏真实 E2E 复核。

## 故障族与已固化门禁

下表的 commit、issue 和当前 test 是可移植证据；标成 `local-only lead` 的 session 只能解释门禁来源，不能单独让验收行 PASS。后续实现该能力时必须把它转成 source-specific regression、脱敏 artifact 或可引用提交，否则该项仍是 `NOT_RUN`。

| 故障族 | 首版后真实暴露的问题 | 现在必须前置的门禁 | 代表证据 |
| --- | --- | --- | --- |
| 中央注册漂移 | 新来源同时漏 provider/action/spec 时既有测试仍绿；API response、credentials endpoint、CLI、refresh、营销脚本各有手抄 roster | 机器可读 contract；canonical family 与 required roster 集合审计；能力例外只能来自 contract | `be582572`；当前代码审计 |
| Canonical identity | alias/strategy/host 漂移产生多种平台名；source backfill 先 LIMIT 后过滤导致跨源数据泄入 | alias/host/prefix 全映射，写库只存 canonical；source-scoped SQL 先 WHERE 后 LIMIT；item/account identity namespaced | `be582572`、`7e7c2a3c` |
| 统一 admission | 平台 producer 绕过统一相关性阈值，低质量候选进入主池 | 所有路径走 shared admission；只有精确 explore 可放宽；source share 贯穿 raw/eval/reclaim | Issue #90、`5ff3d114` |
| Upstream schema | B 站 null payload downstream 崩溃；Bangumi 真实 author/infobox shape 与 fixture 不同；YouTube markup 漂移静默 0 | 真 envelope/反例/分页 fixture；容器逐层校验；真站 contract test；错误 content-type 先分类 | `1d14c625`、`5a117d96`、`9f7ac92c` |
| 路由与渲染 | 小红书新增 search route 漏接 selector；hidden tab 不挂虚拟列表，DOM smoke 误报 0 | route/selector 统一枚举；background 以同源 response capture 为主、DOM fallback；active tab 不被抢占 | `3fa5ad52`、`6db9113d` |
| MAIN/isolated world | 页面状态只存在 MAIN world；listener 晚于响应；SPA/full navigation 使 collector 丢失 | `document_start` tap、listener-before-navigation、有界 scope replay、execution key 与 full-nav recovery | `4306828a`、`0d89dd5b` |
| 真空与假空 | 响应早于 collector、bundle 缺失、login wall、challenge 或 parse error 都曾被写成 `ok + 0` | `response_observed` 与 row count 分离；只有 affirmative empty 才是 `empty`；其它为 degraded/failed 且带脱敏诊断 | `0d89dd5b`；2026-08-09 抖音 session |
| Claim 顺序 | 来源关闭仍 claim；mutex 在 claim 后导致任务卡 `in_progress`；两个扩展 ID/profile 同时领取 | recovery → capability/online → local cross-source mutex → backend atomic lease → claim；force 不绕 active fence | `f0e14cad`、`0d89dd5b` |
| MV3 恢复 | 冷启动恢复旧任务同时又 claim 新任务；alarm/WS/reload 重入；长任务 lease 太短 | 持久 task/tab/deadline、recovery barrier、singleflight、结果消息唤醒、idle/absolute/lease 分离 | `0d89dd5b`；2026-08-09 Linux.do/Douyin sessions |
| 任务页污染 | Discourse SPA 清掉 hash marker，任务页被当普通浏览写 snapshot/profile | marker 必须真站证明跨 redirect/SPA 存活；任务前后普通事件与所有 projection sink delta=0 | 2026-08-09 Linux.do session（local-only lead；新来源须补 portable regression） |
| Staged completion | 非 2xx result 丢失、crash 后重复 merge、seen 失败却 terminal、重试替换 payload | first-final-wins canonical payload；三崩溃窗口；2xx ACK + 同 payload 有界重试；lease reclaim 重放 | `fc247efa`、`0d89dd5b` |
| Scope 与 completeness | partial/final merge 扩大 item cap；部分 scope 429 却标完整；空结果不推进或错误推进节流 | scope/cap 冻结在 task payload；partial 保留并 degraded；只有 `scope_complete` 可推导缺失/retraction | `fc247efa`、`9d926b78`；2026-08-09 V2EX session |
| Incremental admission | 两个 scheduler scan→insert 双建任务；guided init reserve 前 runtime 抢跑；force 绕单飞 | CLI/init/daemon 共用 `BEGIN IMMEDIATE` admission；六门准入；一源/多源 active fence | `2a9b0b6c`、`1cc97a35`、`tests/test_source_incremental_sync.py` |
| Incremental recovery | adoption 丢 created_at/cursor 导致完成后立即再排；seen set/sort 丢顺序；comment 回落 parent | adoption 继承完整 task identity/time/cursor；bounded ordered seen；stable child identity；成功 seed、失败不推进 | `1cc97a35`、`tests/test_source_incremental_sync.py` |
| Smoke purity | `profile_update=false` 仍可写 affinity/snapshot，导致 smoke 污染 | contract 声明所有 sink；临时 DB 逐表 delta；默认只允许 diagnostic/task records | 2026-08-09 V2EX session（local-only lead；新来源须补逐表 delta regression） |
| 身份证据 | 页面 uid 直接标 verified；404/mismatch/并发落盘失败仍绿；瞬时网络抹旧证据；旧非法 verified sticky | observed≠verified；正面匹配才升级；identity 变更清证据；锁内重读/持久化；状态转换矩阵 | `422f275c`、`4ccd9837`、`f13f5daf`、`7a587c02` |
| Cookie 权威性 | 游客 cookie 误判登录；旧 heartbeat 覆盖当前 login wall；测试因开发机已登录假成功 | 真实登录 cookie 只算 observed；当前权威否定优先；anonymous client 剥 Cookie/Authorization | `b2f00780`、`641cfdfe`；2026-08-09 sessions |
| 鉴权粒度 | 全源 `auth_required` 曾把「匿名 + optional credential」压成无需证据；对匿名 discover + 登录 bootstrap 更无法表达 | 区分 optional enhancement 与 capability-specific auth；后者先扩共享后端 readiness，未支持前必须 BLOCKED，禁止客户端补 guard | Bangumi 后续修复；当前 source-auth 契约审计 |
| 验证动作分派 | `browser_heartbeat` 执行器曾把所有非小红书 slug 默认当知乎；只加 `VERIFY_ACTIONS` 会唤醒/读取错平台 | source→heartbeat prefix 显式 registry、DB getter、extension event handler 与往返测试成组审计；未知 source fail closed | 2026-08-09 skill 无提示 forward test；`tests/test_source_auth_contract.py` |
| Credential/UI 状态 | 配置 GET/PUT 静默丢 toggle；disabled early-return 隐藏已存凭据；optional credential 没完整状态转换 | patch keep/clear/masked；enabled/auth 正交；共享 renderer；保存/登录后 live convergence | `7f72636b`、`1398826`、`d9c213b6` |
| 配置准入复制 | 五个 UI 手抄账号 admission，拒绝后端本可接收的 extension identity | backend-only account resolution；客户端只传输入和显示 verdict；跨入口契约测试 | `f26b4556` |
| Search 双轨 | producer 能 claim 关键词但 planner/inspiration 某一轨没给新平台产词；claim 多于实际执行仍全 used | merged + inspiration axis 都有生成测试；claim=consume；失败 rollback；seed 轮换与请求 timeout | `f9547de3`；Linux.do 在途审计 |
| 网络所有权 | 系统代理触发 B 站风控；X 429 固定短 cooldown；第三方 CLI 不一定吃项目代理 | transport owner 明确；国内 client/direct fetch 与外部 CLI 分测；分型持久 backoff | `df626f3`、`f740beb5`、`311fff59` |
| 图片资产 | XHS signed URL 令 cache key 抖动；后台 tab 不 lazy-load，DOM 只有 data/blob 占位；cache 写安装 CWD | canonical image identity/cache-first；page context 取 bytes；用户 data root；token 轮转命中测试 | `6899d523`、`2c541987`、`22e3dafe` |
| 时间语义 | 新抓到的老内容被当“刚发布”；评估时点/cache 未统一 | authoritative UTC `published_at`；精确 `evaluated_at`；高置信分型 bonus；跨小时/cache 测试 | `0588fe17`、`101c1984`、`0d89dd5b` |
| 跨层字段 | YouTube ID 被拼成 B 站 URL；知乎 engagement 抓到但 DTO/SQL/UI 丢；主 grid 修好而 delight card 漏 | canonical URL/source 全链路；storage/DTO/四端 surface matrix；真实 DB + DOM 证据 | `eef044b0`、`6daf0cec`、`8428a447` |
| 依赖与构建 | 本地装 extra、CI 只装 dev；Firefox tap/资产漏打包；Chrome/Firefox build 互相清资源 | 生产/CI/image 依赖一致；双 target 隔离 clean；manifest asset verifier；安装包而非 workspace E2E | `c40fe892`、Issue #78、`0d89dd5b` |
| 测试环境 | Reddit 测试读取真实 home credential；共享 editable venv 指向 main；reload 的是 store build | HOME/data/config 隔离；打印 import/build provenance；记录 artifact hash/version/ID | `9ce95713`；2026-08-09 sessions |

## 选择前例，而不是复制平台

- 匿名官方 API：Bangumi、YouTube；但前者有 optional PAT，后者没有，auth contract 不同。
- 浏览器登录任务：小红书、抖音、知乎、YouTube、Reddit；其中 background/hidden 渲染能力、scope 和 bootstrap 语义不同。
- 第三方 CLI/hybrid：X、Reddit；必须额外验证依赖、credential store 与实际网络所有权。
- search formal discovery：抖音、X、知乎、Reddit 等；必须同时参考 planner 双轨、coordinator claim 和 admission，而不是只复制 fetcher。
- 账号增量：当前共享 scheduler 的浏览器账号来源；public anonymous discovery 可以明确 `N/A`，不能为追求“统一”伪造私有任务。
- native save/image proxy/mobile deep link 都是独立 opt-in capability，不是所有 full source 的默认必需项。

最后一次遗漏审查应只拿原始 contract、diff、测试和构建/真机 artifact，让 reviewer 自己发现问题；不要把上表中“你怀疑会漏的答案”塞进 prompt，避免确认偏差。
