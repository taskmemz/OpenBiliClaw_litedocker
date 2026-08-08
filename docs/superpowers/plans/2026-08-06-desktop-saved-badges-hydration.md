# Desktop Saved Badges Hydration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让桌面 Web 刷新后在用户点击 Tab 前就显示“稍后再看”和“收藏”的真实数量徽标。

**Architecture:** 复用现有 `syncWatchLaterButtons()` 与 `syncFavoriteButtons()`，让它们返回请求 Promise，并加入 `hydrateFromBackend()` 的次级并行水合任务。每个列表用代次栅栏阻止较旧的启动响应覆盖用户进入列表后得到的新快照。保存列表接口失败继续由函数内部吞吐，不阻塞推荐首页；不新增后端接口或持久化状态。

**Tech Stack:** 原生 JavaScript、Python pytest、Playwright Chromium、`ThreadingHTTPServer` 测试桩

## Global Constraints

- 同时修复桌面 Web 的“稍后再看”和“收藏”数量徽标。
- 数量为零时继续隐藏徽标。
- 保存列表读取失败时静默降级，不阻塞推荐列表、运行状态或其他首屏内容。
- 不修改保存列表 API、数据库、移动 Web、小红书候选或来源占比。
- GitHub 提交作者、提交者和推送账号必须是 `RainPot`。

---

### Task 1: 用真实浏览器锁定首次水合行为并完成最小修复

**Files:**
- Modify: `tests/test_desktop_web_issue_98_e2e.py:68-90,128-260,432`
- Modify: `src/openbiliclaw/web/desktop/assets/js/app.js:2802-2828,9126-9138`

**Interfaces:**
- Consumes: `fetchDesktopSaved(listKind) -> Promise<{items: Array, total: number}>`、`updateSavedBadge(badgeId, total)`、`hydrateFromBackend()` 的 `secondaryPromises`
- Produces: `syncWatchLaterButtons() -> Promise<void>`、`syncFavoriteButtons() -> Promise<void>`、`desktopSavedBadgeSyncGenerations: {watch_later: number, favorite: number}`；首次水合完成前发起两个保存列表请求并更新对应徽标，较旧的启动响应不能覆盖后续完整刷新

- [ ] **Step 1: 扩展 Chromium 测试桩并写失败测试**

在 `Issue98Stub.__init__()` 增加保存列表请求记录：

```python
self.saved_list_reads: list[str] = []
```

在 `Handler.do_GET()` 中、通用 404 之前增加两个保存列表响应：

```python
if path == "/api/saved/watch_later":
    state.saved_list_reads.append("watch_later")
    return _json_response(self, {"items": [], "total": 3})
if path == "/api/saved/favorite":
    state.saved_list_reads.append("favorite")
    return _json_response(self, {"items": [], "total": 2})
```

增加真实浏览器测试；测试中不点击任何侧栏按钮：

```python
def test_saved_badges_hydrate_before_tabs_are_opened(
    issue_98_server: tuple[str, Issue98Stub],
    chromium_page: Page,
) -> None:
    base_url, stub = issue_98_server

    chromium_page.goto(f"{base_url}/web/", wait_until="domcontentloaded")

    watch_later_badge = chromium_page.locator("#watchLaterCountBadge")
    favorites_badge = chromium_page.locator("#favoritesCountBadge")
    expect(watch_later_badge).to_be_visible(timeout=3000)
    expect(watch_later_badge).to_have_text("3")
    expect(favorites_badge).to_be_visible(timeout=3000)
    expect(favorites_badge).to_have_text("2")
    assert set(stub.saved_list_reads) == {"watch_later", "favorite"}
```

再增加失败降级测试，确认两个启动请求失败时首页仍能显示推荐：

```python
def test_saved_badge_hydration_failure_does_not_block_home(
    issue_98_server: tuple[str, Issue98Stub],
    chromium_page: Page,
) -> None:
    base_url, _ = issue_98_server
    failed_reads: list[str] = []

    def fail_saved_list(route: Any) -> None:
        if "/status?" in route.request.url:
            route.continue_()
            return
        failed_reads.append(route.request.url)
        route.abort("failed")

    chromium_page.route("**/api/saved/**", fail_saved_list)
    chromium_page.goto(f"{base_url}/web/", wait_until="domcontentloaded")

    expect(chromium_page.locator("#videoGrid .video-card")).to_have_count(3, timeout=3000)
    chromium_page.wait_for_timeout(1000)
    expect(chromium_page.locator("#watchLaterCountBadge")).to_be_hidden()
    expect(chromium_page.locator("#favoritesCountBadge")).to_be_hidden()
    assert len(failed_reads) == 2
```

增加响应乱序测试：在浏览器内延迟首个“稍后再看”列表 Promise，使点击 Tab 触发的
第二个请求先返回 `total = 4`，再释放首个请求的 `total = 3`，最终徽标必须保持 `4`。

- [ ] **Step 2: 运行测试并确认旧代码失败**

Run:

```bash
pytest -q tests/test_desktop_web_issue_98_e2e.py -k 'saved_badge or older_badge' -v
```

Expected: 三个测试均 FAIL；成功场景中的徽标仍为 hidden，失败场景中的
`failed_reads` 仍为空，响应乱序场景会把新徽标 `4` 覆盖回旧值 `3`。

- [ ] **Step 3: 实现最小启动水合改动**

让两个现有同步函数返回各自的请求 Promise，保留原有失败吞吐：

```javascript
const desktopSavedBadgeSyncGenerations = { watch_later: 0, favorite: 0 };

function syncWatchLaterButtons() {
  const generation = desktopSavedBadgeSyncGenerations.watch_later;
  return fetchDesktopSaved("watch_later").then((data) => {
    if (generation !== desktopSavedBadgeSyncGenerations.watch_later) return;
    const saved = new Set((data?.items || []).map((it) => desktopSavedItem(it).item_key));
    document.querySelectorAll('.video-card [data-action="watch-later"]').forEach((btn) => {
      const card = btn.closest(".video-card");
      const item = state.videos.find((row) => String(row.bvid || row.content_id) === card?.dataset?.bvid);
      if (!item) return;
      const on = saved.has(desktopSavedItem(item).item_key);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    updateSavedBadge("watchLaterCountBadge", data?.total);
  }).catch(() => {});
}

function syncFavoriteButtons() {
  const generation = desktopSavedBadgeSyncGenerations.favorite;
  return fetchDesktopSaved("favorite").then((data) => {
    if (generation !== desktopSavedBadgeSyncGenerations.favorite) return;
    const saved = new Set((data?.items || []).map((it) => desktopSavedItem(it).item_key));
    document.querySelectorAll('.video-card [data-action="favorite"]').forEach((btn) => {
      const card = btn.closest(".video-card");
      const item = state.videos.find((row) => String(row.bvid || row.content_id) === card?.dataset?.bvid);
      if (!item) return;
      const on = saved.has(desktopSavedItem(item).item_key);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    updateSavedBadge("favoritesCountBadge", data?.total);
  }).catch(() => {});
}
```

`refreshWatchLater()` 和 `refreshFavorites()` 开始完整刷新时推进对应代次；两个启动
同步函数捕获发请求时的代次，响应返回后若代次已变化则直接退出，不更新按钮或徽标。

将两个 Promise 加入 `hydrateFromBackend()` 的次级资源列表：

```javascript
const secondaryPromises = [
  pendingConfirmationsPromise,
  syncWatchLaterButtons(),
  syncFavoriteButtons(),
  requestJson(ENDPOINTS.health),
  requestJson(ENDPOINTS.initStatus).then(applyInitStatusSnapshot),
  requestJson(`${ENDPOINTS.activityFeed}?limit=5`).then(applyActivitySnapshot),
  requestJson(ENDPOINTS.profile).then(applyProfileSnapshot),
  requestJson(ENDPOINTS.delightBatch).then(applyDelightSnapshot),
  requestJson(ENDPOINTS.notificationPending).then(applyNotificationSnapshot),
  requestJson(`${ENDPOINTS.chatTurns}?session=${encodeURIComponent(SHARED_CHAT_SESSION)}&scope=chat&limit=20`).then(applyChatSnapshot),
  requestJson(`${ENDPOINTS.chatTurns}?session=${encodeURIComponent(SHARED_CHAT_SESSION)}&scope=delight&limit=80`).then(applyDelightChatSnapshot),
  loadConfigSnapshot(),
  refreshPlatformAvailability(),
];
```

- [ ] **Step 4: 运行针对性测试并确认转绿**

Run:

```bash
pytest -q tests/test_desktop_web_issue_98_e2e.py -k 'saved_badge or older_badge' -v
```

Expected: `3 passed`；两个接口均在首次加载阶段被请求，徽标在未点击 Tab 时显示
`3` 和 `2`，请求失败时推荐首页仍正常显示，旧启动响应不能覆盖点击后的新徽标。

- [ ] **Step 5: 运行相关回归测试和静态检查**

Run:

```bash
pytest -q tests/test_desktop_web_issue_98_e2e.py tests/test_desktop_web_list_stability.py
ruff check tests/test_desktop_web_issue_98_e2e.py
git diff --check
```

Expected: 所有测试通过，Ruff 和差异空白检查无错误。

- [ ] **Step 6: 在实际本地数据上进行浏览器验收**

使用当前 OpenBiliClaw 数据目录启动本分支后端，刷新 `/web`，不点击“稍后再看”，检查：

```text
#watchLaterCountBadge 可见且文本为 3
点击 #watchLaterBtn 前后徽标文本都保持 3
```

如果本地收藏数量大于零，也确认 `#favoritesCountBadge` 在点击前显示同一真实数量；若为零，确认徽标保持隐藏。

- [ ] **Step 7: 提交实现**

```bash
git add src/openbiliclaw/web/desktop/assets/js/app.js tests/test_desktop_web_issue_98_e2e.py
git commit -m "fix: 修复保存列表徽标刷新后缺失"
```

提交后检查：

```bash
git log -1 --format='%H%n%an <%ae>%n%cn <%ce>%n%s'
git status --short
```

Expected: 作者和提交者均为 `RainPot <22672916+RainPot@users.noreply.github.com>`，工作树干净。

### Task 2: 推送分支并创建 PR

**Files:**
- Read: `docs/superpowers/specs/2026-08-06-desktop-saved-badges-hydration-design.md`
- Read: `docs/superpowers/plans/2026-08-06-desktop-saved-badges-hydration.md`

**Interfaces:**
- Consumes: 已通过测试和本地验收的 `agent/saved-badges-on-load` 分支
- Produces: `RainPot/OpenBiliClaw` 上的远程分支，以及指向 `whiteguo233/OpenBiliClaw:main` 的 PR

- [ ] **Step 1: 复核身份、远程和提交范围**

```bash
git config user.name
git config user.email
git remote -v
git log --oneline upstream/main..HEAD
git diff --stat upstream/main...HEAD
```

Expected: 身份为 `RainPot`，`origin` 指向 `RainPot/OpenBiliClaw`，差异只包含设计、计划、徽标实现和对应测试。

- [ ] **Step 2: 推送功能分支**

```bash
git push -u origin agent/saved-badges-on-load
```

- [ ] **Step 3: 创建非草稿 PR**

PR 标题：

```text
fix: 修复 Web 保存列表徽标刷新后缺失
```

PR 描述必须包含：问题现象、根因、同时覆盖两个徽标的修复方式、失败降级边界、自动化测试、本地 3 条数据验收结果，以及明确声明不包含小红书来源逻辑改动。

- [ ] **Step 4: 回读远端状态**

```bash
gh pr view --json number,url,title,state,isDraft,headRefName,baseRefName,author
```

Expected: PR 为 OPEN、非草稿，head 是 `RainPot:agent/saved-badges-on-load`，base 是 `whiteguo233:main`。
