# 设置保存后台应用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 配置成功写入后立即返回 202，由后台队列完成热重载，并让桌面 Web 分阶段显示“正在保存”“正在后台应用”和最终结果。

**Architecture:** `PUT /api/config` 保留现有校验、快照和写盘事务，但统一调用现有 `_enqueue_config_apply()`，删除同步热重载分支。桌面 Web 用一个小型保存阶段状态机消费 202、`/api/config/apply-status` 与运行时事件，dirty 状态始终优先，避免旧修订覆盖新编辑。

**Tech Stack:** Python 3.12、FastAPI、asyncio、pytest/httpx、原生 JavaScript/CSS、Node test、Playwright Chromium。

## Global Constraints

- 配置成功写盘统一返回 HTTP 202、`apply_state="queued"` 和单调递增的 `apply_revision`。
- 精确 applying 文案必须是 `配置已保存，正在后台应用…`。
- 不修改配置字段、配置文件格式、认证方式和浏览器扩展设置页交互。
- 保留现有安全排空、latest-wins、失败回滚、`config_reloaded` 与 `config_reload_failed` 事件。
- dirty 展示优先于后台 applying/applied/failed 状态，旧修订不得覆盖新编辑或更新修订。
- 不使用持续轮询；只在 202 后和实时连接建立/重连时读取 apply status。
- 所有代码注释使用中文；Git 提交信息使用中文。
- 只推送 `agent/settings-save-background-apply` 分支，不创建 PR。

## 文件结构

- `src/openbiliclaw/api/app.py`：统一配置写盘后的后台应用入口，删除同步 fast path。
- `tests/test_api_config_transactional.py`：锁定 202、写盘先于响应、后台最终应用、latest-wins 和异步回滚。
- `src/openbiliclaw/web/desktop/assets/js/app.js`：保存阶段状态机、修订过滤、apply status 恢复和运行时事件收敛。
- `src/openbiliclaw/web/desktop/assets/css/app.css`：saving/applying/applied/failed 状态的轻量视觉区分。
- `tests/test_desktop_web_update_status.py`：桌面 Web 状态机静态契约。
- `tests/test_desktop_web_settings_save_e2e.py`：真实 Chromium 验证 202 后立即解除忙态及事件竞态。
- `extension/tests/popup-api.test.ts`：验证共享客户端按 `response.ok` 接受 202。

---

### Task 1: 后端写盘后统一异步应用

**Files:**
- Modify: `tests/test_api_config_transactional.py:43-430`
- Modify: `src/openbiliclaw/api/app.py:16590-16805`

**Interfaces:**
- Consumes: `_enqueue_config_apply(item: _QueuedConfigApply) -> None`、`GET /api/config/apply-status`。
- Produces: `PUT /api/config -> HTTP 202`，响应携带 `apply_state="queued"` 与 `apply_revision: int`。

- [ ] **Step 1: 写空闲通道也必须立即返回的失败测试**

在 `tests/test_api_config_transactional.py` 增加异步测试，用 `asyncio.Event` 阻塞真正的运行时重建入口：

```python
@pytest.mark.asyncio
async def test_put_config_idle_lane_returns_after_persist_before_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("OPENBILICLAW_PROJECT_ROOT", str(tmp_path))
    save_config(_valid_config(), config_path)
    app = create_app(memory_manager=object(), database=object(), soul_engine=object())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_rebuild(self: RuntimeContext, new_config: Config) -> None:
        entered.set()
        await release.wait()
        self.config = new_config

    monkeypatch.setattr(RuntimeContext, "rebuild_from_config", blocked_rebuild)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await asyncio.wait_for(
            client.put(
                "/api/config",
                json={"llm": {"openai": {"model": "gpt-4.1-mini"}}},
            ),
            timeout=1,
        )
        assert response.status_code == 202
        assert response.json()["apply_state"] == "queued"
        assert load_config(config_path).llm.openai.model == "gpt-4.1-mini"
        await asyncio.wait_for(entered.wait(), timeout=1)

        release.set()
        for _ in range(200):
            status = (await client.get("/api/config/apply-status")).json()
            if status["state"] == "applied":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("后台配置修订未完成")

    assert status["applied_revision"] == response.json()["apply_revision"]
```

- [ ] **Step 2: 运行测试并确认红灯来自同步 fast path**

Run: `PIP_CONFIG_FILE=/dev/null .venv/bin/pytest tests/test_api_config_transactional.py::test_put_config_idle_lane_returns_after_persist_before_rebuild -q`

Expected: FAIL，PUT 因等待 `release` 而触发 1 秒超时，证明空闲路径仍同步等待热重载。

- [ ] **Step 3: 用现有队列替换同步 fast path**

在 `src/openbiliclaw/api/app.py` 完成以下最小改动：

```python
_enqueue_config_apply(item)
queued_response = ConfigUpdateResponse(
    ok=True,
    config=_config_to_response(cfg, issues, mask_keys=True),
    message=f"配置已保存到 {saved_path}，正在后台应用。",
    reloaded=False,
    rollback_applied=False,
    restart_required=False,
    apply_state="queued",
    apply_revision=item.revision,
)
return JSONResponse(
    status_code=202,
    content=queued_response.model_dump(mode="json"),
)
```

删除 `_runtime_lane_busy_for_config_apply()` 条件分支之后的同步 `set_outbound_proxy`、`await _apply_runtime_config_revision(item)` 和同步回滚响应分支；后台队列中的代理更新、成功状态、失败回滚和事件发布保持唯一实现。

- [ ] **Step 4: 更新旧同步事务测试为异步合约**

调整同文件现有测试：

- 成功测试断言 202、`reloaded is False`、写盘和 `.bak` 已完成，再轮询到 applied。
- feedback cutover 测试在 202 后轮询 applied 再检查调用次数。
- 失败、TimeoutError、日志和回滚失败测试改为轮询 failed；删除只属于已移除同步回滚路径的 `config_persistence_corrupted` 断言，改为锁定后台失败状态及最后成功配置恢复。
- 并发保存测试断言两个响应都是 202，最终配置与 applied revision 对应最新请求。

- [ ] **Step 5: 运行后端事务测试**

Run: `PIP_CONFIG_FILE=/dev/null .venv/bin/pytest tests/test_api_config_transactional.py -q`

Expected: 所有测试 PASS，且测试数量大于 0。

- [ ] **Step 6: 提交后端任务**

```bash
git add src/openbiliclaw/api/app.py tests/test_api_config_transactional.py
git commit -m "fix: 配置写盘后转为后台应用"
```

---

### Task 2: 桌面 Web 保存阶段状态机

**Files:**
- Modify: `tests/test_desktop_web_update_status.py:30-50`
- Modify: `src/openbiliclaw/web/desktop/assets/js/app.js:1-50, 8645-8700, 8790-8840, 10080-10200`
- Modify: `src/openbiliclaw/web/desktop/assets/css/app.css:1375-1380`

**Interfaces:**
- Consumes: `ConfigUpdateResponse.apply_state`、`apply_revision`、`GET /api/config/apply-status`、运行时事件 `revision`。
- Produces: `refreshConfigApplyStatus()`、`applyConfigApplyStatus(snapshot)`、统一的保存栏 `data-save-state` 与文案。

- [ ] **Step 1: 写保存阶段静态契约失败测试**

扩展 `test_desktop_web_settings_understands_background_config_apply()`，锁定以下字符串和调用关系：

```python
assert 'configApplyStatus: "/config/apply-status"' in js
assert 'let settingsSavePhase = "idle";' in js
assert "let settingsPendingApplyRevision = 0;" in js
assert "正在保存配置…" in js
assert "配置已保存，正在后台应用…" in js
assert 'bar.toggleAttribute("aria-busy",' in js
assert "function applyConfigApplyStatus" in js
assert "function refreshConfigApplyStatus" in js
assert "void refreshConfigApplyStatus();" in js
assert 'event.type === "config_reloaded"' in js
```

- [ ] **Step 2: 运行静态测试确认红灯**

Run: `PIP_CONFIG_FILE=/dev/null .venv/bin/pytest tests/test_desktop_web_update_status.py::test_desktop_web_settings_understands_background_config_apply -q`

Expected: FAIL，首先缺少 `configApplyStatus` 端点映射。

- [ ] **Step 3: 实现保存栏阶段渲染**

在设置保存栏逻辑附近增加：

```javascript
let settingsSavePhase = "idle";
let settingsPendingApplyRevision = 0;

function renderSettingsDirty() {
  const bar = $("#settingsSaveBar");
  const msg = $("#settingsSaveMsg");
  const discard = $("#settingsDiscardBtn");
  const save = $("#settingsSaveBtn");
  const count = settingsDirtyFields.size;
  const phase = count > 0
    ? "dirty"
    : settingsSaveInFlight ? "saving" : settingsSavePhase;
  const messages = {
    idle: "没有未保存的修改",
    saving: "正在保存配置…",
    applying: "配置已保存，正在后台应用…",
    applied: "配置已应用",
    failed: "配置应用失败，已恢复上一次生效配置",
  };
  if (bar) {
    bar.dataset.dirty = count > 0 ? "true" : "false";
    bar.dataset.saveState = phase;
    bar.toggleAttribute("aria-busy", phase === "saving" || phase === "applying");
  }
  if (msg) msg.textContent = phase === "dirty" ? `已修改 ${count} 项，未保存` : messages[phase];
  if (discard) discard.disabled = settingsSaveInFlight || count === 0;
  if (save) save.disabled = settingsSaveInFlight || count === 0;
}
```

CSS 只使用现有变量，为 applying、applied 和 failed 增加边框/文案颜色，不增加图片依赖或大面积动画。

- [ ] **Step 4: 实现修订感知的状态收敛**

增加 `ENDPOINTS.configApplyStatus`，并实现：

```javascript
function applyConfigApplyStatus(snapshot) {
  const requested = Number(snapshot?.requested_revision || 0);
  const applied = Number(snapshot?.applied_revision || 0);
  if (settingsPendingApplyRevision > 0 && requested < settingsPendingApplyRevision) return;
  if (["queued", "applying"].includes(snapshot?.state)) {
    settingsPendingApplyRevision = Math.max(settingsPendingApplyRevision, requested);
    settingsSavePhase = "applying";
  } else if (snapshot?.state === "applied" && applied >= settingsPendingApplyRevision) {
    settingsPendingApplyRevision = 0;
    settingsSavePhase = "applied";
  } else if (snapshot?.state === "failed" && requested >= settingsPendingApplyRevision) {
    settingsPendingApplyRevision = 0;
    settingsSavePhase = "failed";
  }
  renderSettingsDirty();
}

async function refreshConfigApplyStatus() {
  const snapshot = await requestJson(ENDPOINTS.configApplyStatus, { cache: "no-store" });
  if (snapshot) applyConfigApplyStatus(snapshot);
}
```

实现时对 `settingsPendingApplyRevision === 0` 的 applied/failed 快照保持 idle，避免页面首次打开就展示历史终态；queued/applying 快照仍恢复等待状态。

- [ ] **Step 5: 接入保存响应、事件和重连**

- 提交开始时在 `settingsSaveInFlight=true` 后立即渲染 `saving`。
- 202 响应先调用 `applyConfig(result.config)`，再记录 `apply_revision`、设置 `settingsSavePhase="applying"` 并渲染，确保 `applyConfig` 的“配置已从后端加载”不会成为最终状态。
- 删除保存成功后的 `void hydrateFromBackend()`，改为 `void refreshConfigApplyStatus()`。
- `config_reloaded`/`config_reload_failed` 根据事件 revision 收敛状态；旧 revision 直接忽略。
- WebSocket `open` 回调调用 `void refreshConfigApplyStatus()`，补偿断线事件。

- [ ] **Step 6: 运行桌面静态回归测试**

Run: `PIP_CONFIG_FILE=/dev/null .venv/bin/pytest tests/test_desktop_web_update_status.py tests/test_desktop_web_multimodal_settings.py tests/test_desktop_web_list_stability.py -q`

Expected: 所有测试 PASS，且测试数量大于 0。

- [ ] **Step 7: 提交前端状态机任务**

```bash
git add src/openbiliclaw/web/desktop/assets/js/app.js src/openbiliclaw/web/desktop/assets/css/app.css tests/test_desktop_web_update_status.py
git commit -m "feat: 增加配置后台应用状态反馈"
```

---

### Task 3: 真实浏览器与扩展兼容回归

**Files:**
- Create: `tests/test_desktop_web_settings_save_e2e.py`
- Modify: `extension/tests/popup-api.test.ts:1038-1080`

**Interfaces:**
- Consumes: Task 1 的 202 响应结构，Task 2 的 DOM 状态与 `window.WebSocket` 事件入口。
- Produces: 浏览器级保存状态回归和扩展 202 接受性回归。

- [ ] **Step 1: 写桌面 Web E2E 失败测试**

创建本地 `ThreadingHTTPServer`，提供桌面静态资源、最小 hydrate API、`PUT /api/config` 202 响应与 `/api/config/apply-status`。用测试注入的 FakeWebSocket 推送事件，核心断言为：

测试桩维护 `revision=7`、`apply_state="applying"` 和当前占比。`PUT /api/config` 读取 JSON 后递增 revision，并固定返回：

```python
return _json_response(
    self,
    {
        "ok": True,
        "config": payload,
        "message": "配置已保存，正在后台应用。",
        "reloaded": False,
        "rollback_applied": False,
        "restart_required": False,
        "apply_state": "queued",
        "apply_revision": state.revision,
    },
    status=202,
)
```

`GET /api/config/apply-status` 返回 `state`、`requested_revision`、`applied_revision`、`message`、`error` 和 `updated_at`；`GET /api/config` 至少返回 Bilibili 启用、`pool_source_shares`、一个可用 LLM 实例和 Ollama embedding。其余首屏 API 使用空列表或 initialized=true 的最小合法响应，未知路由返回 404。FakeWebSocket 必须暴露以下测试入口：

```javascript
window.__obcPushRuntime = (payload) => {
  const socket = window.__obcSockets.at(-1);
  if (!socket) throw new Error("no live runtime socket");
  socket._emit("message", { data: JSON.stringify(payload) });
};
```

随后执行核心断言：

```python
page.goto(f"{base_url}/web")
page.get_by_role("button", name="设置").click()
page.get_by_role("tab", name="平台源").click()
page.get_by_label("Bilibili 候选池占比").fill("2")
page.get_by_role("button", name="保存配置").click()

expect(page.locator("#settingsSaveMsg")).to_have_text("配置已保存，正在后台应用…")
expect(page.locator("#settingsSaveBtn")).to_have_text("保存配置")
expect(page.locator("#settingsSaveBar")).to_have_attribute("data-save-state", "applying")

page.evaluate("window.__obcPushRuntime({type: 'config_reloaded', revision: 7})")
expect(page.locator("#settingsSaveMsg")).to_have_text("配置已应用")
```

同一测试再保存修订 8，先推送旧修订 7，断言仍为 applying；填入新的本地值后推送修订 8，断言仍显示 `已修改 1 项，未保存`。

- [ ] **Step 2: 运行 E2E 并确认实现前红灯**

Run: `PIP_CONFIG_FILE=/dev/null .venv/bin/pytest tests/test_desktop_web_settings_save_e2e.py -q`

Expected: 在 Task 2 实现前失败；Task 2 完成后 PASS。若机器没有 Chromium，测试应按项目现有 `pytest.importorskip` 规则 SKIP，并保留后续本地浏览器验收。

- [ ] **Step 3: 增加扩展 202 兼容测试**

在 `extension/tests/popup-api.test.ts` 增加：

```typescript
test("updateConfig accepts persisted 202 responses", async () => {
  globalThis.fetch = (async () => ({
    ok: true,
    status: 202,
    async json() {
      return { ok: true, apply_state: "queued", apply_revision: 9 };
    },
  })) as unknown as typeof fetch;

  const result = await updateConfig({ language: "zh" });

  assert.equal(result.apply_state, "queued");
  assert.equal(result.apply_revision, 9);
});
```

- [ ] **Step 4: 运行扩展兼容测试**

Run: `cd extension && node --test --experimental-strip-types tests/popup-api.test.ts`

Expected: PASS。

- [ ] **Step 5: 运行完整相关回归**

```bash
PIP_CONFIG_FILE=/dev/null .venv/bin/pytest \
  tests/test_api_config_transactional.py \
  tests/test_desktop_web_update_status.py \
  tests/test_desktop_web_multimodal_settings.py \
  tests/test_desktop_web_list_stability.py \
  tests/test_desktop_web_settings_save_e2e.py -q
cd extension && node --test --experimental-strip-types tests/popup-api.test.ts
```

Expected: Python 与扩展测试全部 PASS，Python 测试数量大于 0；任何 SKIP 都记录具体原因。

- [ ] **Step 6: 在本地部署页做可回滚验收**

重启本地后端使其加载工作分支代码，在 `http://127.0.0.1:8420/web` 将 Bilibili 占比从原值改为临时值并保存，记录 saving → applying → applied 状态；通过 `GET /api/config` 验证写盘，再恢复原值并重复验证。不得把临时值留在用户配置中。

- [ ] **Step 7: 清理、自审并提交验收任务**

```bash
rg -n '\[DEBUG-[^]]+\]' src tests extension || true
git diff --check
git add tests/test_desktop_web_settings_save_e2e.py extension/tests/popup-api.test.ts
git commit -m "test: 覆盖配置异步保存反馈"
```

- [ ] **Step 8: 推送分支但不创建 PR**

```bash
git status -sb
git push -u origin agent/settings-save-background-apply
```

Expected: 远端分支存在，工作区干净；停止在此处，把分支、提交、测试结果和比较链接交给用户 review，不运行 `gh pr create`。
