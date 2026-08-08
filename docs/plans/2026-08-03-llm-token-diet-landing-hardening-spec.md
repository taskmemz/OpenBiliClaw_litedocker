# LLM Token Diet Landing Hardening Spec

**Created:** 2026-08-03
**Status:** implementation contract frozen; clean-commit acceptance required
**Scope:** `perf/llm-token-diet` landing correctness, replay evidence, evaluation-cache
correctness, reason normalization, integration with current `main`, and release verification.

## 1. Context

`perf/llm-token-diet` 已完成 compact evaluation profile、per-item long-tail recall、
embedding prefilter、bounded eval cache、candidate coalescing、profile views、chat core-memory
split 与 eval reason diet。分支曾实现 body-text caps，但严格真实回放已将其否决并回滚。

当前实现还不能作为可合入证据：

1. 2026-07-18 记录的 replay `PASS` 已被后续规格明确作废，分支内没有新的有效 artifact。
2. replay 读取 raw `soul.json`，没有复现生产的 user overrides 与 active speculations。
3. embedding 空向量/部分失败会被生产 recall 逻辑静默降级，replay 不能据此证明
   compact + recall 确实被执行。
4. A/B 的实际 provider / instance / model 只被平铺记录，没有按 pair/run 归属，也没有
   阻止非模型实验在两臂之间发生 route drift。
5. body-text cap 曾缺少可执行的 model-visible input gate；补齐对照后的 Reddit 100×3
   回放证明它造成显著质量回归，因此生产和正式 replay arm 都必须删除该变更。
6. batch eval cache key 在 recall 生成前命中；实际 recalled labels、embedding namespace、
   prompt-visible content/source context 没有形成完整的可复现输入闭包。
7. reason diet 只依赖 prompt 约束；parser/runtime 不会把 `<0.5` 的 reason 强制清空，
   也不会把 `>=0.5` 的 reason 截到 30 个 Unicode code points。
8. 分支落后当前 `main`，现有测试结果不能替代 rebase 后的集成验证。

本规格是上述三个历史规格的 landing 修正版；发生冲突时，以本规格的验收门为准：

- `2026-07-05-llm-token-diet-spec.md`
- `2026-07-18-profile-views-spec.md`
- `2026-07-18-eval-reason-diet-spec.md`

## 2. Goals

### G1. Replay 证明生产等价

Replay 使用与生产 evaluator 相同的 effective profile、negative exemplars、prompt caps、
embedding model namespace、route 和 output ceiling。基础设施降级必须显式进入 artifact；
会改变实验语义的降级必须让 gate 失败，不能变成零分或无 recall 的正常观测。

### G2. 保留变更有可执行对照，被否决变更有可复核证据

同一个脚本必须支持两个仍在生产候选范围内的变更：

- `compact`：legacy full profile/no recall 对比 production compact + recall；
- `reason-diet`：production inputs 下，legacy unconditional reason 对比 production reason diet。
- `reason-off`（replay-only 诊断）：production reason diet 对比完全省略成功评估结果的
  `reason` 字段；不改变生产默认，并额外审计实际 token usage 与
  `topic_group/style_key/franchise_key` 的 fill/agreement。

两类实验使用冻结 snapshot、重复 A/A 与 A/B、同一 admission policy，并产出独立 JSON。
历史 `body-cap` 对照使用相同契约完成后必须保留失败 artifact 与回滚结论，但不再作为脚本的
正式 arm，也不要求在完整正文的最终代码上制造无意义的两臂差异。

### G3. Eval cache 覆盖决定 prompt 的稳定输入

缓存命中必须只发生在“同一 evaluator 语义输入”上。至少覆盖：

- prompt-visible content fields 的确定性 digest（包括完整 body、去重后的 description、metrics、
  tags、platform/type/strategy 与 effective source context）；
- compact profile + recall pool digest；
- negative exemplars digest；
- recall/embedding namespace；
- cache schema version。

当 recall 发生临时/部分失败时，本次 degraded score 不得写入可在恢复后命中的正常 cache。

### G4. Reason 契约由 runtime 兜底

- `score < 0.5`：最终 `relevance_reason == ""`；
- `score >= 0.5`：strip 后最多 30 个 Unicode code points；
- missing/`None` reason 仍归一化为 `""`；
- 非字符串仍按现有 malformed-member retry 处理；
- single 和 batch evaluator 共用同一纯函数；
- cache 与持久化只接收归一化后的值。

Prompt 约束继续负责减少模型实际生成的 output tokens；runtime 归一化负责保证持久化契约，
两者缺一不可。

### G5. 在当前 main 上给出可复核的 landing 结论

完成 rebase、冲突语义审查、静态检查、全量测试、真实 replay gates 和配置/CLI smoke；
所有结果记录到 landing 文档或 artifact，不能沿用 rebase 前的绿色结果。

## 3. Non-goals

- 不把 embedding prefilter 从 `shadow` 自动切到 `enforce`；enforce 仍需独立线上 shadow 数据。
- 不重新设计 discovery keyword/inspiration 算法。
- 不改变 admission 评分 rubric、阈值语义或 recommendation 排序。
- 不在本任务中持久化 eval score cache；仍为进程内 LRU。
- 不承诺消除 provider 本身的 nondeterminism；通过同日 repeated A/A envelope 测量它。

## 4. Design invariants

### I1. Effective profile parity

Replay profile 等于生产 `SoulEngine.get_profile()` 的可观察结果：

1. load current onion profile；
2. apply `profile_overrides.json`；
3. attach active interest speculations；
4. freeze serialized profile and digest before any arm runs。

Artifact 记录 raw/effective profile digest、override presence 和 active-speculation count，不写入
任何 secret 或完整私人画像正文。

### I2. Embedding completeness is auditable

Replay 的 embedding wrapper 记录每个 request 的 model namespace、非空向量、维度与异常。

- provider exception、空向量、NaN/Inf、同一 namespace 维度漂移：实验失败；
- tail-interest pool 为空：合法的 zero-recall case，记录 `eligible_tail_count=0`；
- 向量完整但没有兴趣超过 similarity threshold：合法，记录 injected label count 为 0；
- compact/reason-diet acceptance 默认要求可用的生产 embedding service；若生产配置
  明确禁用 embedding，则必须通过显式 `--allow-no-embedding` 运行，artifact 标为 degraded，
  不能作为 compact + recall 的 landing 证据。

### I3. Route equivalence is enforced

每个 LLM call 带以下 replay attribution：

- pair kind (`control` / `treatment`)
- repeat index
- logical run (`A1`, `A2`, `A`, `B`)
- actual provider / instance / model

非 `model=...` 实验要求每个 logical run 内 route 唯一，且 A/A/A/B 使用相同 route。
显式 model 实验仅允许 treatment B 使用目标 route；control A/A 和 treatment A 仍必须一致。
route 为空、混用或意外 failover 都让 gate 失败。

### I4. Replay failures are not quality observations

Timeout、LLM/embedding exception、缺失 parsed member、score vector 长度不符、snapshot drift、
route drift、artifact write failure 均以非零退出。不得转换为 score 0 后继续统计。

明确的瞬时 provider rate limit 可以在 registry cooldown 后对同一 chunk 做有界重试；重试前
必须恢复该 chunk 的评估输出字段，失败调用必须留在 route audit，且只有后续成功调用使用同一
实际 route 时才能视为 recovered。分类以 provider adapter 的第一个规范化
`LLMRateLimitError` 为边界，不用 SDK raw cause 中的通用 metadata 重新解释已经映射的 HTTP
429；明确映射的 HTTP 402、余额/计费错误和其它异常不重试。重试预算按 chunk 独立，前一 chunk
恢复成功时不能耗掉下一 chunk 的预算。真实 clean-commit run 证明两次 cooldown 不足以覆盖
gateway 协议修复链中的持续 429，因此 bounded schedule 固定为 65 / 130 / 260 / 520 秒、最多
四次；schedule 必须进入 artifact，不能无限重试。

### I5. Body-cap rejection is binding

历史 `body-cap` 对照在 prompt construction 层比较原始正文与 production 200 head + `…` +
100 tail，并保持 description/body dedup 关系。Reddit 100×3 结果为：treatment flip-rate 中位数
`18% > 8%` control ceiling，Spearman 中位数 `0.192031 < 0.632378` floor，admission delta
中位数 `-11pp < -3pp` floor；42 / 100 条正文受影响且仅保留 12.95% 字符。该失败结论约束
最终实现：discovery single/batch eval 与 recommendation legacy/recovery/single/batch expression
必须保留完整 `body_text`，正式 replay CLI 不再暴露已回滚的 `body-cap` arm。失败 artifact
`data/eval/profile-diet-body-cap-rejected-11f77a64.json` 只作隐私安全的诊断证据。

### I6. Cache determinism and degradation

Recall selection 被视为以下稳定函数：

`f(content_prompt_digest, recall_pool_digest, embedding_namespace)`。

模型 namespace 变化必须产生不同 key。正常 cache entry 只能由完整 recall 计算或明确的
no-recall production mode 写入。临时 embedding failure 的计算结果可以返回给本轮调用，但不写
normal cache；恢复后必须重新评估。

### I7. Cache lookup does not mutate prompt semantics

为形成 content digest 可以构造轻量、纯 deterministic 的 prompt payload；不得发起 LLM 调用。
缓存命中不应为了“验证命中”重复做所有 embedding provider 请求。允许使用稳定 namespace +
完整输入 digest 复用此前由完整 recall 产生的 entry。

### I8. Prompt-cache convention remains intact

所有 system prompt 仍为 module-level byte-stable constants；新增 replay attribution、digest、
runtime reason normalization 均不能进入生产 system prompt。Per-call data 仍只在 user message。

### I9. No hidden default regression during rebase

冲突不能用机械 ours/theirs 解决。特别检查：

- `inspiration_search_enabled` 保留当前 main 的默认；
- 当前 main 新增的 LLM timeout、source pacing、visual/danmaku/TLS 配置不能丢失；
- token-diet 新增的 `eval_prefilter_mode`、eval coalescing、route 文档不能丢失；
- tests 与文档同时保留两侧语义。

### I10. Quality failure changes the diet, not the gate

Final-commit 的首次真实 compact 100×3 replay 对 64 interests / 12 specifics 边界给出明确
失败：treatment Spearman 中位数 `0.494686 < 0.570454` control floor，admission delta 中位数
`-0.09 < -0.07` floor。该 artifact 只用于诊断，不能作为 landing PASS。

第一次修正把边界提高到 80 interests / 32 domains × 16 specifics，并让 per-item tail recall
覆盖 ranks 81..256。`11f77a64` 的冻结画像回放曾通过，但不能替代最终 commit 的重验：随后
effective profile 增长到 87 项兴趣，`397fe03e` 上的严格 100×3 compact replay 再次失败，
treatment flip-rate 中位数为 `15% > 7%` ceiling，Spearman 中位数为
`0.356586 < 0.564777` floor；admission delta 中位数 `+1pp` 通过，唯一 blocker 仍是相对质量门。
失败 artifact 归档为 `data/eval/profile-diet-compact-failed-397fe03e.json`，SHA-256
`d58bb6888276e9c0b40c821d2d450f478645771380a8b4f88d46cdbc06dadcdc`。

第二次修正边界为 96 interests / 32 domains × 16 specifics；per-item tail recall 相应只覆盖
ranks 97..256。选择这一边界的约束是：

- 当前生产画像的 87 项 interests 和全部实际 domain specifics 都保留，主要只移除 volatile
  metadata；当前序列化画像保留 `98.29%` 字符；
- mature fixture 仍减少 `53.58%` 字符，maxed fixture 的实际分层 prompt 减少 `61.17%`，
  继续通过既有“极限画像至少缩短 60%”测试；96 是所验证的 80 / 96 / 128 候选中满足该约束
  的最大边界，128 的同一分层 prompt 仅减少 `56.74%`；
- 画像继续增长到第 97 项后，长尾 recall 与 eval digest 仍覆盖 ranks 97..256；
- Spearman、flip-rate、admission floors 完全不变，保留的 compact / reason-diet 两臂在修正后的
  clean commit 重新执行。

`f26c63e5` 上的 96 / 16 clean-commit replay 又以 treatment flip-rate 中位数
`18% > 16%` control ceiling 失败；Spearman `0.616499` 和 admission delta `+10pp` 通过，
route / embedding / recall audit 均通过。artifact 归档为
`data/eval/profile-diet-compact-failed-f26c63e5.json`，SHA-256
`734fad9310a125d1763e69d3f0f89600862c1715d6a64e84ecd4f1f984231428`。usage 独立汇总同时
证明该边界没有足够收益：标准 100 条 / 4 batch 只从 `103820` 降到 `103124` prompt tokens，
即少 `696`（`0.67%`）；实际含 member-repair 的 treatment 则因 17 次调用对 14 次调用，total
tokens 反而多 `14.75%`。

因此第三次、明确以收益为目的的实验边界为 48 interests / 32 domains × 16 specifics，tail
recall 覆盖 ranks 49..256。冻结前确定性测量显示当前 93 项 effective profile 的分层 profile
block 从 `29072` 降到 `21508` 字符（`26.02%`），maxed fixture 从 `161007` 降到 `51815`
（`67.82%`）。这些只证明体积收益，不替代质量证据；48 / 16 必须从 clean commit 完整执行
同一 100×3 gate，任何阈值均不放宽。

收益诊断进一步使用同一真实 100 条 cohort 构造完整 A/B prompts，并用确定性本地 embedding
让 compact 侧每条候选实际携带 3 个 `related_interests`（共 300 个标签），但用本地假评分器
避免再次消耗 evaluator LLM。四个生产尺寸 batch 的 prompt 字符从 `249918` 降到 `224580`
（`10.14%`）；`cl100k_base` 诊断 tokenizer 从 `124141` 降到 `115833` tokens，少 `8308`
（`6.69%`，DeepSeek 实际 tokenizer 仍须正式 artifact 复核）。不做 recall 时为 `9.33%`，说明
长尾质量补偿消耗约 `2.64pp` 的毛收益。

## 5. Replay artifact contract

每个 artifact 至少包含：

- git commit、dirty flag、config path digest、DB path digest；
- candidate IDs、status/platform/strategy mix、snapshot digest；
- raw/effective profile digest、negative exemplar digest；
- experiment arm、tail-interest count；
- per pair raw scores、admission decisions和 metrics；
- attributed LLM calls、actual routes、usage；
- embedding namespace、call count、vector completeness/dimensions、recall injection counts；
- gate constants、derived envelope、pass/fail 与所有 blocking reasons。

Artifact 不包含 API key、Cookie、完整 config、完整 profile 或完整候选正文。

## 6. Acceptance gates

### A. Automated correctness

- targeted replay/cache/reason tests pass；
- profile-view golden/guard tests pass；
- config/API round-trip tests pass；
- Ruff、MyPy、`git diff --check` pass；
- full `pytest` pass（允许显式 documented environment skips，不允许 failure）。

### B. Real replay

在同一 rebase 后 commit、同一 DB snapshot、同一生产 config 上分别运行：

```bash
.venv/bin/python scripts/run_profile_diet_ab.py \
  --arm-b compact --sample 100 --repeats 3 \
  --output data/eval/profile-diet-compact.json

.venv/bin/python scripts/run_profile_diet_ab.py \
  --arm-b reason-diet --sample 100 --repeats 3 \
  --output data/eval/reason-diet.json

.venv/bin/python scripts/run_profile_diet_ab.py \
  --arm-b reason-off --sample 100 --repeats 3 \
  --output data/eval/reason-off.json
```

compact 与 reason-diet 两个生产候选命令必须 exit 0，artifact 自身 `gate.passed=true`、无
blocking reasons、route 与 embedding 完整性 gate 通过。`reason-off` 是独立诊断臂：只有在
token 与质量门同时通过时才能成为生产候选；若任一门失败，则保留可复核的失败 artifact、
记录否决结论并保持生产 reason diet 不变，不反向阻塞已验证的生产方案。

`c6327506` 的 100×3 reason-off 诊断已经否决该方案：B reason field count 为 0，但 total token
增加 `31.70%`，准入差值中位数 `-5pp` 低于相对 floor `+2pp`。artifact 的 route、embedding、
recall、reason-output 与分类完整性子门均通过；最终非零退出仅来自 relative quality gate。

被否决的 body-cap 不再从最终代码重跑；其冻结失败 artifact 必须保持 `gate.passed=false`，并可
从 raw paired scores 独立复算上述质量回归。最终代码用单元/E2E 测试确认所有相关 prompt 路径
保留完整正文。

### C. Runtime/E2E smoke

- `openbiliclaw config-show` 能显示/加载新增配置；
- 使用 deterministic fake provider 完成 candidate enqueue → coalesced claim → batch eval →
  cache → admission 的端到端路径；
- 模拟 embedding 首次失败、随后恢复，确认 degraded score 不会阻止恢复后的 recall re-eval；
- 模拟相同 content ID 但 prompt-visible body/source context 改变，确认 cache miss；
- 模拟低分长 reason 与高分超长 reason，确认归一化后再缓存/持久化；
- chat core-memory stable/volatile、profile overrides 与 extractor opt-out 回归测试继续通过。

## 7. Rollback and observability

- 代码回滚以独立 commit 为单位：replay-only、cache correctness、reason normalization、docs。
- `eval_prefilter_mode` 保持 `shadow`，不把此次 landing 与 enforce rollout 绑定。
- 合入后观察至少 48 小时：
  - `openbiliclaw cost --by caller` 的 evaluation/recommendation tokens per call；
  - evaluation cache hit rate；
  - embedding failure/empty-vector 日志；
  - score/admission distribution 与 `evaluation_response_missing`；
  - recommendation 文案缺失率与用户质量反馈。

任何真实质量回归优先回滚对应 model-visible diet commit，不通过放宽 replay gate 掩盖。
