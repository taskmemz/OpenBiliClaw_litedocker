#!/usr/bin/env python3
"""Quantified gate for the platform-source auth-contract refactor.

Prints the six baseline numbers of the "Goal" table in
``docs/plans/2026-07-18-source-auth-contract-spec.md`` and, with ``--check``,
fails while any of them is still off target.

Baseline on ``main@5d8d8889`` is ``424 / 1 / 2 / 0 / 4 / 2``. Two rows read
differently from the spec's first draft, both because the original rule measured
something other than what it claimed:

* metric 1 is 424, not 467 -- the old rule counted to the next route decorator
  and swallowed three unrelated helpers parked in between;
* metric 2 is 1, not 6 -- the old rule counted the whole desktop bundle,
  including recommendation-card rendering, which is not part of this contract.

Every metric
documents its counting rule next to the code that implements it: **the rule is
the metric**. Redefining one silently invalidates the comparison against the
spec, so prefer adding a new metric over changing an existing one -- and if a
rule genuinely must move, update the spec's Goal table in the same commit.

Standard library only, so this runs in any CI job that has a Python 3.11+.

Usage::

    PYTHONPATH="$PWD/src" "$SOURCE_SKILL_PYTHON" scripts/source_contract_metrics.py --check
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_family_inventory() -> tuple[tuple[str, ...], dict[str, str]]:
    """Derive the metric denominator and route aliases from the canonical registry."""
    path = REPO_ROOT / "src/openbiliclaw/sources/platforms.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, str] = {}
    rules: ast.expr | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
            if any(
                isinstance(target, ast.Name) and target.id == "SOURCE_FAMILY_RULES"
                for target in node.targets
            ):
                rules = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.target.id] = node.value.value
            if node.target.id == "SOURCE_FAMILY_RULES":
                rules = node.value

    if not isinstance(rules, (ast.Tuple, ast.List)):
        raise RuntimeError("SOURCE_FAMILY_RULES must be a literal tuple/list for metrics")

    def _string(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    families: list[str] = []
    aliases: dict[str, str] = {}
    for rule in rules.elts:
        if (
            not isinstance(rule, ast.Call)
            or not isinstance(rule.func, ast.Name)
            or rule.func.id != "SourceFamilyRule"
        ):
            raise RuntimeError("every SOURCE_FAMILY_RULES entry must be a literal SourceFamilyRule")
        keywords = {item.arg: item.value for item in rule.keywords if item.arg}
        family_node = keywords.get("family")
        family = _string(family_node) if family_node is not None else None
        if family is None:
            raise RuntimeError("every SourceFamilyRule needs a statically readable family keyword")
        if family in families:
            raise RuntimeError(f"duplicate canonical source family in metrics registry: {family}")
        families.append(family)
        aliases[family] = family
        alias_node = keywords.get("platform_aliases")
        if (
            not isinstance(alias_node, ast.Call)
            or not isinstance(alias_node.func, ast.Name)
            or alias_node.func.id != "frozenset"
            or len(alias_node.args) != 1
            or not isinstance(alias_node.args[0], (ast.Set, ast.Tuple, ast.List))
        ):
            raise RuntimeError(
                f"SourceFamilyRule {family!r} needs a literal frozenset platform_aliases"
            )
        for alias_node_item in alias_node.args[0].elts:
            alias = _string(alias_node_item)
            if alias is None:
                raise RuntimeError(f"SourceFamilyRule {family!r} has a non-literal platform alias")
            owner = aliases.get(alias)
            if owner is not None and owner != family:
                raise RuntimeError(
                    f"platform alias {alias!r} is shared by {owner!r} and {family!r}"
                )
            aliases[alias] = family
    if not families:
        raise RuntimeError("SOURCE_FAMILY_RULES contains no statically readable families")
    return tuple(dict.fromkeys(families)), aliases


# Unlike the original fixed eight-entry table, this changes automatically when
# a new canonical family is registered, so a templated verify route cannot make
# an 8/8 metric conceal a ninth platform missing from the surrounding contract.
PLATFORMS, API_SLUG_ALIASES = _source_family_inventory()


def _literal_registry_keys(relative: str, name: str) -> set[str]:
    """Read string keys from one top-level literal registry assignment."""
    path = REPO_ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    value: ast.expr | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value = node.value
            break
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            value = node.value
            break
    if not isinstance(value, ast.Dict):
        raise RuntimeError(f"{relative}:{name} must be a top-level literal dict for metrics")
    return {
        key.value
        for key in value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


API_DIR = Path("src/openbiliclaw/api")
APP_PY = Path("src/openbiliclaw/api/app.py")
DESKTOP_APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js")
POPUP_JS = Path("extension/popup/popup.js")
MOBILE_APP_JS = Path("src/openbiliclaw/web/js/app.js")
SHARED_JS_DIR = Path("src/openbiliclaw/web/shared")

# The three HTML surfaces of the four-surface contract (CLAUDE.md pitfall #5).
# The fourth surface, the CLI, is not a frontend file -- hence the spec's "/3".
FRONTEND_SURFACES: tuple[Path, ...] = (DESKTOP_APP_JS, POPUP_JS, MOBILE_APP_JS)

Direction = Literal["at_most", "at_least"]


class MetricError(RuntimeError):
    """A metric lost its anchor in the source tree (renamed route, moved file)."""


@dataclass(frozen=True)
class Metric:
    """One row of the spec's Goal table."""

    key: str
    label: str
    value: int
    target: int
    direction: Direction
    rule: str
    evidence: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        if self.direction == "at_most":
            return self.value <= self.target
        return self.value >= self.target

    @property
    def target_text(self) -> str:
        return f"<= {self.target}" if self.direction == "at_most" else f">= {self.target}"


def _read(path: Path) -> str:
    full = REPO_ROOT / path
    try:
        return full.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        raise MetricError(f"缺少统计所需的文件：{path}") from exc


def _read_optional(path: Path) -> str:
    """Read a file that only exists after a later phase lands (empty if absent)."""
    full = REPO_ROOT / path
    return full.read_text(encoding="utf-8") if full.is_file() else ""


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


# Route decorators are single-expression calls; capture the whole call text so
# keyword arguments such as ``deprecated=True`` stay attached to their path.
_ROUTE_DECORATOR = re.compile(r"@(?:app|router)\.(?P<verb>get|post|put|delete)\(")
_MAX_DECORATOR_CHARS = 600


def _balanced_call(source: str, open_idx: int) -> str:
    """Return the ``(...)`` slice starting at *open_idx*, parens balanced.

    Deliberately naive about parentheses inside string literals: route
    decorators are plain ``("/path", response_model=X)`` calls, and the
    character cap keeps a malformed match from scanning the whole file.
    """
    depth = 0
    for idx in range(open_idx, min(len(source), open_idx + _MAX_DECORATOR_CHARS)):
        char = source[idx]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[open_idx : idx + 1]
    return source[open_idx : open_idx + _MAX_DECORATOR_CHARS]


def _iter_routes(source: str) -> Iterator[tuple[str, str, str, int]]:
    """Yield ``(verb, path, call_text, offset)`` for each route decorator."""
    for match in _ROUTE_DECORATOR.finditer(source):
        call = _balanced_call(source, match.end() - 1)
        path_match = re.match(r'\(\s*"([^"]+)"', call)
        if path_match is None:
            continue
        yield match.group("verb"), path_match.group(1), call, match.start()


def _api_sources() -> list[tuple[Path, str]]:
    """Every Python file of the API package, sorted for deterministic output."""
    return [
        (path.relative_to(REPO_ROOT), path.read_text(encoding="utf-8"))
        for path in sorted((REPO_ROOT / API_DIR).rglob("*.py"))
    ]


# --------------------------------------------------------------------------
# Metric 1 -- size of the /api/sources/status handler block
# --------------------------------------------------------------------------

_SOURCES_STATUS_ANCHOR = '@app.get("/api/sources/status"'
_DEF_LINE = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?def\s")


def measure_sources_status_lines() -> Metric:
    """Metric 1: size of the ``sources_status()`` handler (spec D8).

    Counting rule: start at the ``@app.get("/api/sources/status")`` decorator,
    locate its ``def``, then walk down to the first non-blank, non-comment line
    indented no deeper than that ``def`` -- the first line that belongs to the
    enclosing factory rather than to this handler. The span runs from the
    decorator through the last non-blank line before it, so trailing blanks are
    not counted.

    Why not "count to the next ``@app`` route decorator": three unrelated
    helpers (``_mask_source_credential``, ``_xhs_token_from_url``,
    ``_latest_xhs_token``, app.py:9214-9255) are parked between this handler and
    the next route. That rule swept their ~43 lines into the total and reported
    467; the handler really spans 8789-9212 == 424. Any rule keyed on "the next
    route" measures whatever happens to sit in the gap, so it silently drifts
    when someone adds a helper there.

    Baseline: 424.
    """
    lines = _read(APP_PY).splitlines()

    anchor: int | None = None
    for lineno, line in enumerate(lines, start=1):
        if _SOURCES_STATUS_ANCHOR in line:
            anchor = lineno
            break
    if anchor is None:
        raise MetricError(f"{APP_PY} 里找不到锚点 {_SOURCES_STATUS_ANCHOR}")

    def_lineno: int | None = None
    def_indent = 0
    for lineno in range(anchor, len(lines) + 1):
        match = _DEF_LINE.match(lines[lineno - 1])
        if match is not None:
            def_lineno = lineno
            def_indent = len(match.group("indent"))
            break
    if def_lineno is None:
        raise MetricError(f"{APP_PY}:{anchor} 装饰器之后找不到 def")

    end = len(lines)
    for lineno in range(def_lineno + 1, len(lines) + 1):
        line = lines[lineno - 1]
        stripped = line.strip()
        # Blank lines and comments never end a body: a comment sitting at
        # factory indent is prose about the *next* definition, not this one.
        if not stripped or stripped.startswith("#"):
            continue
        if _indent_of(line) <= def_indent:
            end = lineno - 1
            break
    while end > def_lineno and not lines[end - 1].strip():
        end -= 1

    return Metric(
        key="sources_status_lines",
        label="sources_status() 函数体量",
        value=end - anchor + 1,
        target=140,
        direction="at_most",
        rule=(
            'app.py 中 @app.get("/api/sources/status") 装饰器起，至函数体真正结束处'
            "（首个缩进 <= def 的非空非注释行之前的最后一个非空行）的行数；"
            "中间夹的无关辅助函数不计"
        ),
        evidence=(f"{APP_PY}:{anchor}-{end}",),
    )


# --------------------------------------------------------------------------
# Metric 2 -- per-platform branches inside the platform-source settings UI
# --------------------------------------------------------------------------

# Operand allow-list is quoted verbatim from the spec (I4). Widening it to a
# bare `=== "<platform>"` would sweep in unrelated code.
_PLATFORM_BRANCH = re.compile(
    r'(?:key|item\.state|platform)\s*===\s*"(?:' + "|".join(PLATFORMS) + r')"'
)

# Anchors for the settings region: the render functions fed by
# /api/sources/status and /api/sources/credentials. Matched by name *pattern*
# rather than a fixed name list, so a plausible rename
# (renderSourceCredentialRows -> renderCredentialRows) keeps being scanned
# instead of silently zeroing the metric.
_SETTINGS_RENDER_FUNC = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:export|async)\s+)*function\s+"
    r"(?P<name>(?:render|update)[A-Za-z_$]*(?:Source|Credential)[A-Za-z_$]*)\s*\("
)

# A sibling definition at the same indentation closes the preceding region.
_JS_DEFINITION = re.compile(r"(?:(?:export|async)\s+)*(?:function|const|let|var|class)\b")


def _settings_regions(path: Path) -> list[tuple[str, int, int, str]]:
    """Slice the source-settings render functions out of a frontend file.

    Yields ``(name, first_line, last_line, body)`` per region, using metric 1's
    slicing trick: from the ``function`` line to whichever comes first -- the
    next sibling definition at equal indentation, or the first non-blank line
    that dedents out of the enclosing scope. No hardcoded line ranges, so a
    region tracks its code as it moves.
    """
    source = _read_optional(path)
    if not source:
        return []

    lines = source.splitlines()
    regions: list[tuple[str, int, int, str]] = []
    for index, line in enumerate(lines):
        match = _SETTINGS_RENDER_FUNC.match(line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        end = len(lines)
        for probe in range(index + 1, len(lines)):
            candidate = lines[probe]
            if not candidate.strip():
                continue
            lead = _indent_of(candidate)
            if lead < indent or (lead == indent and _JS_DEFINITION.match(candidate.strip())):
                end = probe
                break
        regions.append((match.group("name"), index + 1, end, "\n".join(lines[index:end])))
    return regions


def measure_source_settings_platform_branches() -> Metric:
    """Metric 2: per-platform branches in the source-settings UI (spec I4).

    Counting rule: regex matches found *only inside the platform-source
    settings region* -- the access-status rows, credential forms and source
    toggles driven by ``/api/sources/status`` and ``/api/sources/credentials``.
    Counted per occurrence, not per line.

    Scope note: recommendation-card rendering is deliberately **excluded**.
    Desktop's ``contentUrl`` (4 branches) and ``recommendationMetaHtml`` (1)
    build per-platform content URLs and author links -- card display, not
    login-contract display. Counting them would drag card rendering into this
    spec's Phase 4 for no contract reason.

    All three surfaces are scanned rather than desktop alone, because I4 binds
    all three; popup and mobile simply hold zero such branches today.
    ``web/shared/`` is scanned whole because after Phase 4 that module *is* the
    settings renderer -- otherwise moving a branch there would hide it.

    Evidence lists every resolved region including the zero-hit ones, so a
    suspicious 0 (anchors stopped resolving) is distinguishable from an earned
    one. Baseline: 1 -- ``key === "xiaohongshu"`` in desktop's
    ``renderSourceCredentialRows``.
    """
    total = 0
    evidence: list[str] = []

    for path in FRONTEND_SURFACES:
        for name, start, end, body in _settings_regions(path):
            hits = list(_PLATFORM_BRANCH.finditer(body))
            total += len(hits)
            details = [f"L{start + _line_of(body, hit.start()) - 1} {hit.group(0)}" for hit in hits]
            suffix = f" [{'; '.join(details)}]" if details else ""
            evidence.append(f"{path}:{start}-{end} {name}(): {len(hits)}{suffix}")

    shared_dir = REPO_ROOT / SHARED_JS_DIR
    if shared_dir.is_dir():
        for shared in sorted(shared_dir.rglob("*.js")):
            rel = shared.relative_to(REPO_ROOT)
            hits = list(_PLATFORM_BRANCH.finditer(_read_optional(rel)))
            total += len(hits)
            evidence.append(f"{rel} (whole file): {len(hits)}")

    return Metric(
        key="source_settings_platform_branches",
        label="平台源设置区 per-platform 相等判断",
        value=total,
        target=0,
        direction="at_most",
        rule=(
            "仅平台源设置区域：三端源设置渲染函数体 + web/shared/*.js 内 "
            '(key|item.state|platform) === "<平台>" 的匹配次数；推荐卡渲染不计'
        ),
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------------
# Metric 3 -- copies of the status -> presentation mapping table
# --------------------------------------------------------------------------

# ``Object.freeze({...})`` counts: the shared module wraps its table that way,
# and a pattern demanding a bare ``= {`` silently scored it as *zero* copies —
# i.e. the gate reported "nobody hand-maintains the enum" at the exact moment
# one module legitimately did. A metric that goes quiet when the thing it
# measures moves is worse than no metric (invariant I7: a syntactic proxy is
# fine for a gate, never for a conclusion).
_STATUS_MAP_DEF = re.compile(
    r"\b(SOURCE_ACCESS_STATE|SOURCE_STATUS_DOT|SOURCE_STATUS_LABEL)\s*=\s*"
    r"(?:Object\.freeze\(\s*)?\{"
)


def measure_status_map_copies() -> Metric:
    """Metric 3: how many frontends hand-maintain the state enum (spec D6).

    Counting rule: count *files* holding at least one such definition, not the
    definitions themselves. popup.js splits its copy across ``SOURCE_STATUS_DOT``
    (14 keys) and ``SOURCE_STATUS_LABEL`` (15 keys) -- that split *is* the D6
    drift, but it is still one copy of the same knowledge. Baseline: desktop 1
    + popup 1 == 2.

    ``web/shared/`` is scanned even though it does not exist yet, so that the
    shared module Phase 4 introduces lands as the single remaining copy
    (target 1) instead of dropping the count to a misleading 0.
    """
    candidates: list[Path] = [*FRONTEND_SURFACES]
    shared_dir = REPO_ROOT / SHARED_JS_DIR
    if shared_dir.is_dir():
        candidates += [path.relative_to(REPO_ROOT) for path in sorted(shared_dir.rglob("*.js"))]

    evidence: list[str] = []
    for path in candidates:
        source = _read_optional(path)
        names = sorted({m.group(1) for m in _STATUS_MAP_DEF.finditer(source)})
        if names:
            evidence.append(f"{path}: {', '.join(names)}")

    return Metric(
        key="frontend_status_map_copies",
        label="前端状态映射表副本数",
        value=len(evidence),
        target=1,
        direction="at_most",
        rule="定义了 SOURCE_ACCESS_STATE / SOURCE_STATUS_DOT / SOURCE_STATUS_LABEL 的前端文件数",
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------------
# Metric 4 -- platforms reachable by a verify action
# --------------------------------------------------------------------------

_VERIFY_PATH = re.compile(r"^/api/sources/(?P<slug>[^/]+)/verify$")


def measure_platforms_with_verify() -> Metric:
    """Metric 4: platforms that expose a verify action (spec D7, Phase 2).

    Counting rule: scan every route in the API package for a path shaped
    ``/api/sources/<slug>/verify``.

    * A templated slug (``{slug}``) makes the route reachable for every platform,
      but a platform counts only when both ``SOURCE_AUTH_PROVIDERS`` and
      ``VERIFY_ACTIONS`` contain it. The denominator comes independently from
      the canonical source-family registry, so a newly registered ninth source
      cannot turn a missing dispatcher entry into a self-fulfilling 9/9.
    * Literal slugs are de-duplicated through ``API_SLUG_ALIASES`` so that
      ``/api/sources/dy/verify`` counts as douyin exactly once.

    Baseline: 0 -- the repo has no verify route at all.
    """
    covered: set[str] = set()
    evidence: list[str] = []
    providers = _literal_registry_keys(
        "src/openbiliclaw/api/source_auth/providers.py", "SOURCE_AUTH_PROVIDERS"
    )
    actions = _literal_registry_keys("src/openbiliclaw/api/source_auth/verify.py", "VERIFY_ACTIONS")

    for path, source in _api_sources():
        for _verb, route, _call, offset in _iter_routes(source):
            match = _VERIFY_PATH.match(route)
            if match is None:
                continue
            evidence.append(f"{path}:{_line_of(source, offset)}: {route}")
            slug = match.group("slug")
            if slug.startswith("{"):
                covered.update(PLATFORMS)
            else:
                covered.add(API_SLUG_ALIASES.get(slug, slug))

    wired = covered & providers & actions & set(PLATFORMS)
    return Metric(
        key="platforms_with_verify_endpoint",
        label="有 verify 动作的平台",
        value=len(wired),
        target=len(PLATFORMS),
        direction="at_least",
        rule=(
            "/api/sources/{平台}/verify 路由 ∩ SOURCE_AUTH_PROVIDERS ∩ VERIFY_ACTIONS；"
            "目标来自 canonical source-family registry"
        ),
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------------
# Metric 5 -- naming shapes among credential-write endpoints
# --------------------------------------------------------------------------

# Vocabulary of credential-carrying leaf segments; ``credential`` / ``token``
# are the shapes Phase 3 converges on.
#
# ``identity`` used to be in this set, on spec D5's assumption -- written while
# feat/bangumi-source was unmerged and therefore unreadable -- that
# ``POST /api/sources/bangumi/identity`` carried a 令牌. Reading the merged
# branch falsifies that: the handler takes a public uid + username scraped off a
# bgm.tv page, rejects any payload without a positive uid, and says in its own
# docstring that "no cookies or tokens are accepted here". Bangumi's
# ``access_token`` is written by ``PUT /api/config`` and ``POST /api/init``
# instead. Leaving the leaf in would report two credential-write shapes to a
# client that in fact faces one, so it is dropped: this is invariant I7's
# mandatory semantic re-check overruling the syntactic proxy, which is the only
# direction that rule permits (门可以用代理，结论不行).
_CREDENTIAL_LEAVES = frozenset(
    {"cookie", "cookies", "tokens", "token", "login-state", "credential"}
)
_DEPRECATED_KWARG = re.compile(r"deprecated\s*=\s*True")


def _credential_shape(route: str) -> str | None:
    """Return the naming shape of *route*, or None if it is not a credential write."""
    leaf = route.rstrip("/").rsplit("/", 1)[-1]
    if leaf not in _CREDENTIAL_LEAVES:
        return None
    if route.startswith("/api/sources/"):
        # Inside the namespace the leaf word alone is the shape: `cookie`,
        # `tokens` and `login-state` are the three that exist on main.
        return leaf
    # Outside the namespace every path is its own shape by definition -- a
    # client cannot derive it from the platform slug and must special-case it.
    # That is spec D5's `/api/bilibili/cookie`, the 4th shape.
    return route


def measure_credential_endpoint_shapes() -> Metric:
    """Metric 5: how many ways the API spells "write a credential" (spec D5).

    Counting rule: POST/PUT routes in the API package whose last path segment
    is in the credential vocabulary, reduced to distinct shapes.

    * POST/PUT only, so the read-only ``GET /api/sources/credentials`` listing
      never counts.
    * Routes marked ``deprecated=True`` are skipped. Phase 3 keeps all six
      legacy endpoints alive as internal forwards, so marking them deprecated
      is what makes the 4 -> 1 target reachable while the old contracts stay
      served. No route carries the flag today, so the baseline is unaffected.

    Baseline: ``cookie`` + ``tokens`` + ``login-state`` + ``/api/bilibili/cookie``
    == 4.
    """
    shapes: dict[str, str] = {}

    for path, source in _api_sources():
        for verb, route, call, offset in _iter_routes(source):
            if verb not in {"post", "put"}:
                continue
            if _DEPRECATED_KWARG.search(call):
                continue
            shape = _credential_shape(route)
            if shape is None:
                continue
            shapes.setdefault(shape, f"{path}:{_line_of(source, offset)}: {route}")

    return Metric(
        key="credential_endpoint_shapes",
        label="凭据写入端点命名形态",
        value=len(shapes),
        target=1,
        direction="at_most",
        rule="POST/PUT 凭据端点的命名形态数（sources 命名空间内按词根，命名空间外整条路径各算一种；deprecated=True 不计）",
        evidence=tuple(shapes[name] for name in sorted(shapes)),
    )


# --------------------------------------------------------------------------
# Metric 6 -- frontends that carry platform-source settings
# --------------------------------------------------------------------------

# Any one marker means the surface renders source access state: it either talks
# to the status/credentials API directly or consumes Phase 4's shared module.
_SOURCE_SETTINGS_MARKERS: tuple[str, ...] = (
    "/api/sources/status",
    "/api/sources/credentials",
    "source-status.js",
)

# Frontends expected to carry source settings. Mobile Web is a deliberate
# exclusion, not an unfinished surface (decided with the user 2026-07-19): the
# phone is a lean-back "刷推荐" surface, and supplying a cookie / clicking 测试
# 连接 belongs on the desktop or extension, where the browser session that owns
# those credentials actually lives. Mobile still *surfaces* per-platform login
# needs through saved-sync (``views/saved.js``), so a user is never blind to a
# logged-out source there — they just fix it on a full surface. CLAUDE.md
# pitfall #5 requires the exclusion be explicit; this is that statement, and it
# is why the target is 2 of 3 rather than 3 of 3.
_SETTINGS_EXPECTED_SURFACES: tuple[Path, ...] = (DESKTOP_APP_JS, POPUP_JS)


def measure_frontends_with_source_settings() -> Metric:
    """Metric 6: surfaces carrying platform-source settings (spec D10, I6).

    Counting rule: how many of the three HTML surfaces reference at least one
    marker. Target is 2 — desktop and popup — because mobile Web is an explicit
    exclusion (see ``_SETTINGS_EXPECTED_SURFACES``). Reporting mobile in the
    evidence when it unexpectedly *gains* a marker is still useful, so the scan
    stays over all three surfaces and only the target reflects the exclusion.
    """
    evidence: list[str] = []
    for path in FRONTEND_SURFACES:
        source = _read_optional(path)
        hits = [marker for marker in _SOURCE_SETTINGS_MARKERS if marker in source]
        if hits:
            evidence.append(f"{path}: {', '.join(hits)}")

    return Metric(
        key="frontends_with_source_settings",
        label="承载平台源设置的前端",
        value=len(evidence),
        target=len(_SETTINGS_EXPECTED_SURFACES),
        direction="at_least",
        rule="桌面 / popup 两端引用平台源接口或共享渲染模块的文件数（移动端有意排除，见注释）",
        evidence=tuple(evidence),
    )


def collect_metrics() -> list[Metric]:
    """The six Goal-table rows, in spec order."""
    return [
        measure_sources_status_lines(),
        measure_source_settings_platform_branches(),
        measure_status_map_copies(),
        measure_platforms_with_verify(),
        measure_credential_endpoint_shapes(),
        measure_frontends_with_source_settings(),
    ]


def _display_width(text: str) -> int:
    """Terminal columns of *text*, counting CJK glyphs as two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    filler = " " * max(0, width - _display_width(text))
    return filler + text if right else text + filler


def render_table(metrics: list[Metric]) -> str:
    header = ("#", "指标（spec Goal 表左列）", "当前", "目标", "达标")
    right = (True, False, True, True, False)
    rows = [
        (
            str(index),
            metric.label,
            str(metric.value),
            metric.target_text,
            "OK" if metric.ok else "FAIL",
        )
        for index, metric in enumerate(metrics, start=1)
    ]
    widths = [
        max(_display_width(row[column]) for row in (header, *rows)) for column in range(len(header))
    ]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(
            _pad(cell, width, right=flag)
            for cell, width, flag in zip(cells, widths, right, strict=True)
        ).rstrip()

    out = [line(header), "  ".join("-" * width for width in widths)]
    out += [line(row) for row in rows]

    passed = sum(1 for metric in metrics if metric.ok)
    out += [
        "",
        f"达标 {passed}/{len(metrics)} 项。基线（main@5d8d8889）：424 / 1 / 2 / 0 / 4 / 2。",
        "",
        "口径：",
    ]
    out += [f"  {index}. {metric.rule}" for index, metric in enumerate(metrics, start=1)]
    return "\n".join(out)


def build_payload(metrics: list[Metric]) -> dict[str, object]:
    return {
        "all_pass": all(metric.ok for metric in metrics),
        "metrics": [
            {
                "direction": metric.direction,
                "evidence": list(metric.evidence),
                "key": metric.key,
                "label": metric.label,
                "ok": metric.ok,
                "rule": metric.rule,
                "target": metric.target,
                "value": metric.value,
            }
            for metric in metrics
        ],
        "repo_root": str(REPO_ROOT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="平台来源接入契约的量化指标（spec Goal 表左列六项）。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="任一指标未达目标即退出码 1（CI 门）。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读的 JSON（含口径与证据行号）。",
    )
    args = parser.parse_args(argv)
    as_json = bool(args.json)
    checking = bool(args.check)

    try:
        metrics = collect_metrics()
    except MetricError as exc:
        print(f"指标锚点失效：{exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(build_payload(metrics), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_table(metrics))

    if checking and not all(metric.ok for metric in metrics):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
