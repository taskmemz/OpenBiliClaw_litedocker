# 微博平台来源验收台账

> Integration level：`full`。微博同时提供匿名公开 discovery 与登录态、只读、init-only 的个人事件 bootstrap。静态、构建和隔离回归可以在本 worktree 完成；真实登录账号 E2E 需要用户自己的已登录浏览器，因此没有把 `NOT_RUN` 写成 `PASS`。

## Provenance

- Contract：[`docs/platform-source-contract.weibo.toml`](platform-source-contract.weibo.toml)
- Worktree：`feat/weibo-profile`，基线 `origin/main`（本台账不执行 commit、merge、push、发版或上游写操作）
- Python：`PYTHONPATH=src ../../.venv/bin/python`
- Extension：当前 worktree 的 Chrome / Firefox manifest；构建依赖未随仓库安装时，静态 Node 测试仍可直接运行
- Real account boundary：未读取用户浏览器 Cookie、未使用真实登录账号、未执行微博站内写操作

## Gate ledger

| Gate | Applicability | Status | Evidence / remaining work |
| --- | --- | --- | --- |
| Frozen contract and central registration | required | PASS | `audit_platform_source.py --contract docs/platform-source-contract.weibo.toml --check --json`：`required_missing=0`、`MISSING=0`、`PASS=43`、`N/A=6`、`MANUAL=13` |
| Anonymous transport / normalization / budgets | required | PASS | `tests/test_weibo_client.py`、`tests/test_weibo_producer.py`、`tests/test_weibo_wiring.py` and public smoke fixtures |
| Capability-specific auth/status | required | PASS | Heartbeat is boolean-only; no-heartbeat method is `none`, fresh heartbeat uses `browser_heartbeat`; capability matrix and account binding are covered by `tests/test_source_auth_contract.py` and `tests/test_api_weibo.py` |
| Durable init queue / event projection | required | PASS | `weibo_tasks` queue, staged first-final result, uid partition, duplicate suppression and favorite/follow/mention mapping are covered by source bootstrap/API tests |
| Browser task isolation and cookie boundary | required | PASS | `extension/tests/weibo-task.test.ts`, the full extension suite (`npm test`: 1387 passed), and contract tests prove same-origin task routing, query marker, bounded normalized payload, no `document.cookie`, `chrome.cookies` values or raw HTML in the task bridge |
| CLI / API / setup / config | required | PASS | `--yes-weibo` / `--no-weibo`, `/api/init` readiness gate, login-state callback, config round-trip and status surfaces pass focused tests |
| Desktop / mobile / popup recommendation surfaces | required | PASS | Existing Weibo surface suites cover source identity, author, text cards, shares, delight and local-only saved membership |
| Real anonymous upstream smoke | required | PASS | Public `m.weibo.cn/api/config` and existing anonymous search/hot/creator smoke are read-only; no user Cookie or upstream mutation |
| Live anonymous Weibo → SenseTime → recommendation | required | PASS | 2026-08-11 isolated run: real Weibo search/hot returned 4 posts (2+2), 4 candidates were evaluated, 2 were admitted, and SenseTime `deepseek-v4-flash` handled the evaluation and recommendation-expression calls; one canonical `weibo.com` recommendation was served with author/body/share count and topic label. No user database or upstream write was touched. |
| Live mobile recommendation / delight cards | required | PASS | 2026-08-11 real mobile page smoke served the admitted Weibo post and its delight card. DOM and screenshots confirm the `微博` badge, author, text/body, `🔁281`, topic/reason, and all action buttons fit the viewport: `output/playwright/weibo-live/mobile-recommendation.png` and `output/playwright/weibo-live/mobile-delight.png`. |
| Real logged-in bootstrap E2E | required | NOT_RUN | Requires a user-authorized, already logged-in Chrome/Firefox profile. Run the documented three-scope task and record uid, counts, durable event rows, second-run dedupe, and zero Cookie/raw-response egress. |
| Real LLM/profile convergence | required | NOT_RUN | Requires configured user LLM/embedding and explicit consent to import personal events. Do not use hand-written scores or synthetic account data. |
| Chrome + Firefox built artifacts | required | PASS | `npm ci`、`npm run build`、`TARGET=firefox npm run build` 均通过；`tsc --emitDeclarationOnly` 通过，Chrome/Firefox asset preflight 均报告 19 个 manifest scripts/WAR 资源存在。 |
| Chrome + Firefox real browser load | required | NOT_RUN | 需要在隔离 Chrome/Firefox profile 中加载刚生成的 `extension/dist` 与 `extension/dist-firefox`，再跑 settings、登录态心跳和任务 tab DOM smoke。 |
| Upstream state-changing actions | N/A | PASS | Contract has `mutating_actions=[]`; no like/favorite/follow/comment/repost/native-save request exists in the Weibo task executor. |
| Documentation / release mutation | required | PASS | README CN/EN, config/CLI/init/docker/store/architecture/spec/changelog and Weibo module docs updated; no version bump or release mutation. |

## Privacy and event boundary

- Public discovery uses the backend anonymous visitor client. Its `SUB` is process-memory-only and never accepts ambient `Authorization` / user `Cookie` headers.
- Personal bootstrap runs in a hidden `m.weibo.cn` task tab with browser credentials. The extension only returns normalized post/user fields, scope counts, a positive current uid, and structured errors; the backend stores a boolean heartbeat and an opaque account key, never Cookie values.
- Favorites map to `favorite`, following to `follow`, and mentions to `comment`. Keys are namespaced by scope and account, then admitted through the shared profile event ingress.
- A missing identity, login wall, challenge HTML, malformed JSON, timeout, or partial scope failure is failed/partial—not a healthy empty result. Accepted rows remain staged and retryable scopes are not silently consumed.
- Weibo native save, ordinary page behavior collection, response taps, and platform writes remain excluded. OpenBiliClaw save/later membership is local-only.

## Focused verification commands

```bash
PYTHONPATH=src ../../.venv/bin/pytest -q \
  tests/test_weibo_contract.py tests/test_weibo_wiring.py \
  tests/test_source_bootstrap.py tests/test_source_auth_contract.py \
  tests/test_api_weibo.py tests/test_cli_weibo.py tests/test_web_weibo_surfaces.py \
  tests/test_web_guided_init.py
node --test --experimental-strip-types extension/tests/weibo-task.test.ts
PYTHONPATH=src ../../.venv/bin/python scripts/audit_platform_source.py \
  --contract docs/platform-source-contract.weibo.toml --check --json
```

Latest focused result in this worktree: **436 passed, 34 skipped** (FastAPI's existing `on_event` deprecation warnings only); the full extension suite is **1387 passed**, and the TypeScript compile plus Chrome/Firefox asset preflight passed. The result is an implemented full integration with real-account and real-browser-load gates still `NOT_RUN`, not a claim that a logged-in account was tested.
