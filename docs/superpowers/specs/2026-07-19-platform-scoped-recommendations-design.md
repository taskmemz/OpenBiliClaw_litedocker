# PC Web 平台定向推荐与候选库存设计

**日期：** 2026-07-19
**范围：** PC Web 推荐页、推荐 API、RecommendationEngine、候选池读取与库存统计

## 1. 问题

PC Web 推荐页当前的平台 chip 只是对已经装入 `state.videos` 的推荐卡做本地过滤。
用户切到“知乎”后点击“换一批”或“加载更多”，前端仍请求全平台推荐，后端也仍从
全平台候选池选片；响应里如果没有知乎内容，当前 Tab 会继续显示空白。

这造成三个不一致：

1. Tab 看起来像平台入口，实际只是结果过滤器。
2. “换一批 / 加载更多”无法表达用户当前明确的平台意图。
3. Tab 不展示候选池真实库存，用户无法区分“当前页没装入”与“该平台暂时没货”。

## 2. 目标

- “全部”继续使用现有跨平台推荐逻辑，行为与接口调用保持向后兼容。
- 选中具体平台后，“换一批”和“加载更多”必须把 canonical
  `source_platform` 传到后端；返回的每一条推荐都必须属于该平台。
- 平台限定只缩小候选集合；后续 curator 评分、疲劳惩罚、MMR、多样性约束、
  推荐文案读取、推荐历史写入和 shown 消费逻辑全部复用现有实现。
- Tab 展示当前 canonical candidate pool 中每个平台“可立即推荐”的剩余数量。
- 数量与平台定向选片使用同一套 servability 口径，不能一个显示有货、另一个从不同
  集合取片。
- PC Web 在平台间切换时保留其它平台本会话已加载的卡片；单平台“换一批”只替换
  该平台的本地批次。
- 单平台库存不足时复用现有 runtime 补货入口，不新增第二套 discovery 算法。

## 3. 非目标

- 不改变 discovery 的平台抓取、评估、准入阈值、来源份额或限流算法。
- 不新增配置字段，不改变 `pool_target_count` 或 source share 的含义。
- 不把搜索框文本传给推荐引擎；搜索仍只过滤当前已加载卡片。
- 不改变移动 Web、扩展 popup / side panel 或 CLI 的推荐交互。它们没有本需求所指的
  PC Web 平台 Tab，继续调用不带平台参数的兼容路径。
- 切换 Tab 本身不自动消费候选池；只有“换一批”、手动“加载更多”或已经启用的滚动
  自动续页会产生定向推荐请求。
- 不做视觉重设计；沿用现有 `.chip` 视觉，只增加紧凑计数和可访问状态。

## 4. 术语与权威口径

### 4.1 平台

请求与库存统一使用 `sources.platforms` 的 canonical slug，例如：

- `bilibili`
- `xiaohongshu`
- `douyin`
- `youtube`
- `twitter`
- `zhihu`
- `reddit`
- `bangumi`

别名只能在 API 边界被归一化；引擎和存储层只消费 canonical 值。非法或未知平台请求
返回 422，不静默回退到“全部”或 B 站。

### 4.2 可推库存

Tab 数量不是：

- 当前 DOM 卡片数；
- 推荐历史表中的未反馈数量；
- raw discovery candidate 数；
- 缺文案、缺分类、不可跳转或仍处于近期已看窗口的 pending 数。

它必须与当前 `count_pool_candidates()` / `serve()` 的 canonical available 定义一致：
fresh、非 dislike、达到 admission floor、文案与分类齐全、链接可用、未被 delight
claim、未进入推荐历史、未处于近期已看窗口，并遵循当前 topic window。

后端暴露以下不变量：

```text
total_available == sum(by_platform.values())
strict_platform_candidates(platform)
  ⊆ canonical_available_candidates_for(platform)
```

“全部”数量来自同一份 snapshot 的 `total_available`。库存读取失败时，前端保留上一次
成功值；首次尚未成功读取时显示未知态，不把失败伪装成 `0`。

## 5. 用户体验

### 5.1 Tab 集合与顺序

始终先显示“全部”。其余 Tab 是以下集合的并集：

1. 当前配置中已启用的平台；
2. 当前库存 snapshot 中数量大于 0 的平台；
3. 当前会话已经加载卡片所属的平台。

已知平台按现有 `sourceFilterDefinitions` 顺序排列；其它值按稳定字典序排列。若当前
Tab 因配置热更新或库存变化不再存在，则回退到“全部”。

配置快照（`/api/config`）与库存快照并行读取；前者偶发失败时按有界退避重试
（1s / 2s / 4s / 8s），成功后 Tab 并集收敛——已启用但零库存的平台不会因一次
瞬断而永久缺席。

每个 Tab 显示：

```text
全部 37
B 站 18
知乎 7
Reddit 0
```

计数使用 tabular numerals，计数徽标不引发布局跳动。按钮具有明确的 selected 状态、
可见键盘 focus 和包含完整平台名、库存数的 accessible name；不能只靠颜色表达选中态。

### 5.2 切换平台

- 切换只改变当前视图，不发推荐请求。
- 如果本会话已经加载过该平台卡片，立即显示它们。
- 如果没有卡片但库存大于 0，空态说明可以“加载更多推荐”。
- 如果没有卡片且库存为 0，空态说明该平台暂时没有新候选、后台可继续补货。
- 搜索词只影响卡片显示，不影响 Tab 库存数字和后端平台参数。

### 5.3 换一批

请求发出前捕获当时选中的平台，避免用户在请求期间切 Tab 后把响应写进错误批次。

- “全部”：请求不带 `source_platform`，成功后替换整个 `state.videos`，保持旧行为。
- 具体平台：请求携带 `source_platform`；排除当前会话中该平台已经加载的内容。成功后
  只替换 `state.videos` 中该平台的卡片，其它平台卡片保留。
- 当前卡片始终作为 `excluded_bvids` 提交，这是换批本身的默认硬去重语义，不由开关控制。
- 非空换批成功后，后端只记录一条中性的 `reshuffle` 批次事件；不把当前卡片逐条提交为
  `dismiss`，桌面端也不再展示“换一批时忽略当前”开关。
- 后端返回空数组时保留现有卡片，不制造空屏。

### 5.4 加载更多与自动续页

- 手动与自动续页调用同一 `append` 路径，均携带请求开始时的选中平台。
- 排除列表覆盖当前会话已加载 ID，响应按稳定 recommendation key 去重后追加。
- 平台 Tab 下所有新增卡片必须匹配该平台；“全部”保持混合推荐。
- 自动续页的库存 gate 使用当前平台数量；不能因为全局还有库存就在一个 `0` 库存平台
  上反复空请求。
- 手动按钮在平台库存为 0 时仍可触发一次请求，让后端复用现有补货机制；自动续页不空转。

### 5.5 库存更新

以下时机刷新平台库存 snapshot：

- PC Web 首次 hydrate；
- 换一批或加载更多完成后；
- 收到 `pool_status` / `refresh.pool_updated` 等库存变化事件后（去抖、单飞）；
- 初次读取失败后的既有有界恢复流程或用户重试。

成功 snapshot 才覆盖旧值。库存更新只重绘 Tab / 空态与自动续页 gate，不重载或覆盖已经
append 的推荐卡片。

## 6. API 契约

### 6.1 请求模型

`POST /api/recommendations/reshuffle`

```json
{
  "excluded_bvids": ["BV..."],
  "source_platform": "zhihu"
}
```

`POST /api/recommendations/append`

```json
{
  "excluded_bvids": ["BV..."],
  "source_platform": "zhihu"
}
```

`source_platform` 是可选 additive 字段。省略或空字符串表示“全部”，旧客户端行为不变。
非空值在 Pydantic 边界归一化与校验。

响应继续使用现有 `RecommendationReshuffleResponse`，避免让老客户端必须理解新字段。

### 6.2 平台库存

新增只读接口：

`GET /api/recommendations/platform-availability`

```json
{
  "total_available": 37,
  "by_platform": {
    "bilibili": 18,
    "xiaohongshu": 5,
    "zhihu": 7,
    "reddit": 7
  }
}
```

- 读取使用独立 SQLite read snapshot / serve worker，不把共享连接直接扔进线程。
- `by_platform` 使用 canonical source-family 归类，兼容 legacy strategy 前缀。
- 零库存平台可以省略；前端对已启用但缺失的键显示 `0`。
- 读取异常返回可诊断的 5xx；前端保留旧 snapshot，不接受“失败即全零”。

### 6.3 补货

具体平台请求返回不足 `limit` 时，API 复用现有
`request_replenishment(..., force=True)` / source deficit 规划。它只负责唤醒已有补货链路，
不在 HTTP 请求内同步抓平台，也不承诺本次立即补满。

## 7. 引擎与存储设计

### 7.1 Storage

平台候选读取必须复用 canonical available 集合，而不是先生成全平台推荐再过滤结果。
`get_pool_candidates_for_platform()` 应满足：

- 与 `count_pool_candidates()` 相同的 servability、近期已看、linkability、delight 和
  topic-window 守卫；
- 使用 `_pool_source_family(source, source_platform)` 做 canonical 归类；
- 返回完整 candidate row，供原有推荐逻辑继续执行；
- 返回集合与平台库存计数保持一致。

新增 isolated async availability snapshot，确保 API 读取同一个事务视图中的总量和
平台分布，并保持 `total == sum(by_platform)`。

### 7.2 RecommendationEngine

以下入口增加默认空值的 keyword-only `source_platform`：

- `serve()` / `serve_with_result()`
- `reshuffle_recommendations()` / `reshuffle_recommendations_with_result()`
- `append_recommendations()` / `append_recommendations_with_result()`

具体平台模式：

1. snapshot 阶段只加载该平台候选；
2. 不执行跨平台 floor top-up；
3. 继续执行 disliked-topic / recently-viewed 防线；
4. 继续执行 curator score、amplification guard、embedding/MMR、topic/style/broad-topic
   多样性和 visual bonus；
5. 持久化与 shown commit 不变；
6. 结果返回前以断言或明确测试保证每条 `source_platform` 匹配请求。

空值模式不得改变现有 snapshot、platform floor、多平台排序或补货判断。

### 7.3 兼容路径

生产 async snapshot 和测试 double / third-party adapter fallback 都要支持平台参数。为避免
破坏旧 fake，只有请求实际带平台时才向不确定签名的兼容对象传新关键字；全平台调用保持
旧调用形状。

## 8. 并发、错误与安全

- 前端捕获请求开始时的平台，不读取响应到达时可能已经变化的 `state.filter`。
- `appendMoreInFlight` 继续阻止重复续页；平台库存刷新使用单飞 + pending 合并。
- API 不接受任意 SQL token；平台值先 canonicalize / validate，再进入存储查询。
- 使用参数化 SQL，不拼接平台值。
- 单平台空结果不会清空现有卡片。
- 库存接口失败不会把已有计数覆写为零。
- 后端在平台模式中发现跨平台结果时应记录错误并拒绝泄漏到响应，而不是前端静默过滤。

## 9. 测试与验收标准

### 9.1 Storage / Engine

- mixed pool 中平台库存总数与分平台数一致。
- legacy `source_strategy` 能归到正确 canonical 平台。
- 不可跳转 XHS、近期已看、delight claim、已推荐、未分类/未生成文案内容不计数也不入选。
- `serve(..., source_platform="zhihu")` 返回全为知乎，并仍经过 curator/MMR。
- `serve()` 不带平台时的既有多平台 floor 和排序测试不变。
- append / reshuffle 的 sync compatibility 与 async snapshot 路径均覆盖。

### 9.2 API

- append / reshuffle 正确转发 canonical 平台与 exclusions。
- 省略平台保持旧调用形状。
- alias 被归一化；非法平台返回 422。
- availability 接口返回 canonical map 和严格总和不变量。
- 单平台短批次唤醒已有补货入口，不同步执行 discovery。

### 9.3 PC Web

- Tab 集合来自启用配置、库存和当前卡片的并集，顺序稳定。
- 每个 Tab 显示正确数量、selected/focus/accessible state。
- 知乎 Tab 的 reshuffle / append 请求体都带 `source_platform="zhihu"`。
- 知乎请求返回后可见卡全部为知乎；切回 B 站时原 B 站本会话卡片仍在。
- “全部”请求不带平台且仍可显示混合来源。
- 请求期间切 Tab 不会错写批次。
- 平台 0 库存阻止自动续页空转，但不禁用手动补货动作。
- availability 请求失败保留旧数字。

### 9.4 真实浏览器 E2E

用真实 Chromium 驱动 PC Web，后端 stub 提供混合首屏和可变化的平台库存：

1. 验证 Tab 与数字；
2. 切换知乎并点击换一批，断言 POST body 与可见卡来源；
3. 点击加载更多，断言追加请求仍为知乎且数量变化；
4. 切回 B 站，断言保留原卡；
5. 切回“全部”，断言无平台参数的兼容路径；
6. 验证键盘 focus、selected state 和无水平溢出回归。

## 10. 文档与发布边界

本变更涉及 PC Web → API → RecommendationEngine → Storage 的参数与库存数据流，必须同步：

- `docs/modules/recommendation.md`
- `docs/modules/storage.md`
- `docs/changelog.md` 当前版本块
- `docs/architecture.md`
- `docs/spec.md`
- 若 README 顶部架构图包含该段数据流，`README.md` / `README_EN.md` 同步注释

PR 说明需明确四端范围：后端接口是 additive；只有 PC Web 新增平台 Tab 行为，移动 Web、
扩展和 CLI 因无该交互而保持现状。
