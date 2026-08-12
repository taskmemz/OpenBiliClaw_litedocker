"""Runtime Linux.do discovery producer.

The producer follows the same extension-backed contract as Zhihu: enqueue one
durable task per enabled discovery mode, wake the installed browser extension,
wait for the canonical terminal result, normalize topic rows, and hand them to
the shared candidate pipeline.  Search keywords participate in the unified
claim/used/failed/rollback lifecycle.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib import error, request

from openbiliclaw.runtime.keyword_fetch import PLATFORM_LINUXDO
from openbiliclaw.runtime.pool_gate import candidate_pool_full_for_source
from openbiliclaw.runtime.producer_cadence import (
    ledger_available,
    producer_ran_within,
    record_producer_run,
)
from openbiliclaw.sources.linuxdo_tasks import (
    LINUXDO_TASK_RESULT_GRACE_SECONDS,
    LinuxdoTaskQueue,
    linuxdo_discovery_items_to_contents,
    linuxdo_task_timeout_seconds,
    recent_linuxdo_creator_urls,
    recent_linuxdo_related_urls,
)

logger = logging.getLogger(__name__)

LINUXDO_SOURCE_ORDER = ("search", "hot", "feed", "creator", "related")
LINUXDO_SOURCE_STRATEGIES: dict[str, str] = {
    "search": "linuxdo-search",
    "hot": "linuxdo-hot",
    "feed": "linuxdo-feed",
    "creator": "linuxdo-creator",
    "related": "linuxdo-related",
}
_TRANSIENT_RESULT_MARKERS = (
    "429",
    "rate_limit",
    "rate limit",
    "timeout",
    "timed_out",
    "network",
    "transport",
    "temporar",
    "unavailable",
    "offline",
    "challenge",
    "access_blocked",
    "response_too_large",
    "invalid_response",
    "http_error",
    "extension_disconnected",
    "login_required",
    "not_logged_in",
)


@dataclass
class LinuxdoDiscoveryProducer:
    """Throttle and invoke plugin-backed Linux.do discovery."""

    task_queue: Any
    soul_engine: Any
    enabled: bool = True
    sources: tuple[str, ...] = ("search",)
    min_interval_minutes: int = 3
    daily_search_budget: int = 0
    daily_hot_budget: int = 0
    daily_feed_budget: int = 0
    daily_creator_budget: int = 0
    daily_related_budget: int = 0
    wait_seconds: float = 180.0
    poll_interval_seconds: float = 0.5
    max_items_per_keyword: int = 20
    max_seed_count: int = 5
    candidate_pipeline: Any | None = None
    candidate_evaluation_owned_by_coordinator: bool = False
    keyword_fetch: Any | None = None
    creator_seed_loader: Any | None = None
    related_seed_loader: Any | None = None
    kick: Any | None = None
    database: Any | None = None
    _last_run_at: datetime | None = field(default=None, init=False)
    _last_skip_reason: str = field(default="", init=False)

    async def produce_if_due(self, *, limit: int | None = None) -> dict[str, object]:
        """Run one discovery cycle when enabled, due and under pool quota."""
        if not self.enabled:
            return self._skip("disabled")
        if not self._is_due():
            return self._skip("throttled")
        if self._candidate_pool_full():
            return self._skip("pool_full")

        try:
            profile = await self.soul_engine.get_profile()
        except Exception as exc:
            logger.debug("linuxdo producer: soul profile unavailable: %s", exc)
            return self._skip("no_profile")
        if profile is None:
            return self._skip("no_profile")

        requested_limit = min(300, max(1, int(limit or self.max_items_per_keyword)))
        source_modes = _normalize_sources(self.sources)
        coordinator = self.keyword_fetch
        all_items: list[dict[str, Any]] = []
        branch_items: dict[str, list[dict[str, Any]]] = {}
        branch_task_ids: dict[str, str] = {}
        branch_budget_caps: dict[str, int | None] = {}
        keyword_ids: dict[str, int] = {}
        branch_errors: list[dict[str, str]] = []
        search_claimed: list[Any] = []
        search_task_result: dict[str, Any] = {}
        search_raw_items: list[dict[str, Any]] = []
        enqueued_task_count = 0
        executed_task_count = 0
        skipped_reasons: list[str] = []

        for source in source_modes:
            payload: dict[str, object] = {}
            claimed: list[Any] = []
            daily_budget = self._daily_budget_for(source)
            remaining_budget = self._remaining_budget(source, daily_budget)
            if remaining_budget == 0:
                skipped_reasons.append("budget_exhausted")
                continue
            branch_budget_caps[source] = remaining_budget

            if source == "search":
                seed_limit = max(1, int(self.max_seed_count))
                if coordinator is not None and bool(
                    getattr(coordinator, "should_claim", lambda: False)()
                ):
                    claimed = list(coordinator.claim(PLATFORM_LINUXDO, seed_limit))[:seed_limit]
                    keywords = [
                        str(item.keyword).strip() for item in claimed if str(item.keyword).strip()
                    ]
                    keyword_ids.update(
                        {str(item.keyword).strip(): int(item.id) for item in claimed}
                    )
                else:
                    keywords = _fallback_profile_keywords(profile, seed_limit)
                if not keywords:
                    skipped_reasons.append("no_keywords")
                    continue
                payload = {
                    "keywords": keywords,
                    "max_items_per_keyword": requested_limit,
                    "max_items": requested_limit,
                    "hydrate_topic_details": True,
                }
                if keyword_ids:
                    payload["source_keyword_ids"] = {
                        keyword: keyword_ids[keyword]
                        for keyword in keywords
                        if keyword in keyword_ids
                    }
            elif source in {"hot", "feed"}:
                payload = {"max_items": requested_limit}
            elif source == "creator":
                creator_urls = await self._load_seed_values(self.creator_seed_loader)
                if not creator_urls:
                    creator_urls = _same_run_creator_urls(all_items)
                if not creator_urls:
                    skipped_reasons.append("no_creator_seeds")
                    continue
                payload = {
                    "creator_urls": creator_urls[: max(1, int(self.max_seed_count))],
                    "max_items_per_creator": requested_limit,
                }
            elif source == "related":
                related_urls = await self._load_seed_values(self.related_seed_loader)
                if not related_urls:
                    related_urls = _same_run_related_urls(all_items)
                if not related_urls:
                    skipped_reasons.append("no_related_seeds")
                    continue
                payload = {
                    "related_urls": related_urls[: max(1, int(self.max_seed_count))],
                    "max_items_per_seed": requested_limit,
                    "max_items": requested_limit,
                    "hydrate_topic_details": True,
                }

            if source in {"search", "hot", "feed", "creator"}:
                payload["cursor_contract"] = "page-offset-v1"
                payload["start_cursors"] = self._start_cursors(source, payload)

            task_id = self._enqueue_task(source, payload, daily_budget=daily_budget)
            if task_id is None:
                if source == "search" and coordinator is not None:
                    self._rollback_claims(coordinator, claimed)
                skipped_reasons.append("budget_exhausted")
                continue

            enqueued_task_count += 1
            branch_task_ids[source] = task_id
            await self._kick_dispatcher()
            task_result = await self._wait_for_task(task_id)
            if str(task_result.get("_last_status", "")).strip() == "in_progress" or str(
                task_result.get("_task_status", "")
            ).strip() in {"completed", "failed"}:
                executed_task_count += 1
            terminal_status = _task_terminal_status(task_result)
            if terminal_status in {"ok", "empty"}:
                self._commit_page_cursors(source, payload, task_result)
            if terminal_status in {"failed", "degraded", "timeout"}:
                branch_errors.append(
                    {
                        "source": source,
                        "status": terminal_status,
                        "error": _task_result_error_summary(task_result),
                    }
                )
            raw_items = task_result.get("items")
            items = (
                [item for item in raw_items if isinstance(item, dict)]
                if isinstance(raw_items, list)
                else []
            )
            all_items.extend(items)
            branch_items[source] = items
            if source == "search" and coordinator is not None and claimed:
                search_claimed = list(claimed)
                search_task_result = task_result
                search_raw_items = items

        if enqueued_task_count == 0:
            return self._skip(_aggregate_skip_reason(skipped_reasons))

        # A completed extension cycle must advance cadence even when every
        # branch returns an evidence-backed empty page.  Otherwise the refresh
        # loop immediately enqueues the same browser work again.
        if executed_task_count > 0:
            self._stamp_run(executed_task_count)
        branch_contents: dict[str, list[Any]] = {}
        for source in source_modes:
            normalized = linuxdo_discovery_items_to_contents(
                branch_items.get(source, []),
                source_keyword_ids=keyword_ids,
            )
            cap = branch_budget_caps.get(source)
            branch_contents[source] = normalized if cap is None else normalized[:cap]
        retention_order = _rotated_source_order(source_modes, self._retention_cursor())
        contents = _round_robin_contents(
            branch_contents,
            retention_order,
            requested_limit,
        )
        retained_by_source: dict[str, int] = {}
        for content in contents:
            source = _strategy_source(content.source_strategy)
            if source:
                retained_by_source[source] = retained_by_source.get(source, 0) + 1
        if contents:
            last_source = _strategy_source(contents[-1].source_strategy)
            setter = getattr(self.task_queue, "set_discovery_cursor", None)
            if last_source and callable(setter):
                with suppress(Exception):
                    setter(last_source)
        if coordinator is not None and search_claimed:
            retained_search_ids = {
                content.content_id
                for content in contents
                if content.source_strategy == LINUXDO_SOURCE_STRATEGIES["search"]
            }
            retained_search_items = [
                item
                for item in search_raw_items
                if f"topic:{str(item.get('topic_id', '')).strip()}" in retained_search_ids
            ]
            self._finish_search_claims(
                coordinator,
                search_claimed,
                retained_search_items,
                search_raw_items,
                search_task_result,
            )
        if not contents:
            self._record_retained_counts(branch_task_ids, {})
            if branch_errors:
                return {
                    "discovered": 0,
                    "reason": _aggregate_branch_failure_reason(branch_errors),
                    "branch_errors": branch_errors,
                }
            return {"discovered": 0, "reason": "empty"}

        source_counts: dict[str, int] = {}
        for content in contents:
            source_counts[content.source_strategy] = (
                source_counts.get(content.source_strategy, 0) + 1
            )
        result_payload: dict[str, object] = {
            "discovered": len(contents),
            "source_counts": source_counts,
            "reason": "degraded" if branch_errors else "ok",
        }
        if branch_errors:
            result_payload["branch_errors"] = branch_errors
        charged_by_source = dict(retained_by_source)
        if self.candidate_pipeline is not None:
            enqueued = 0
            charged_by_source = {source: 0 for source in branch_task_ids}
            for strategy in _ordered_strategies(source_modes):
                grouped = [item for item in contents if item.source_strategy == strategy]
                if not grouped:
                    continue
                inserted = int(
                    self.candidate_pipeline.enqueue_candidates(
                        grouped,
                        source_context=strategy,
                    )
                )
                enqueued += inserted
                source = _strategy_source(strategy)
                if source:
                    charged_by_source[source] = charged_by_source.get(source, 0) + inserted
            result_payload["enqueued"] = enqueued
            self._record_retained_counts(branch_task_ids, charged_by_source)
            if enqueued > 0 and not self.candidate_evaluation_owned_by_coordinator:
                drain_result = await self.candidate_pipeline.drain_pending(
                    profile=profile,
                    batch_size=requested_limit,
                )
                result_payload.update(drain_result)
        else:
            self._record_retained_counts(branch_task_ids, charged_by_source)
        return result_payload

    def _record_retained_counts(
        self,
        task_ids: dict[str, str],
        counts: dict[str, int],
    ) -> None:
        """Charge per-mode budgets only for candidates that survived final admission."""

        recorder = getattr(self.task_queue, "record_retained", None)
        if not callable(recorder):
            return
        for source, task_id in task_ids.items():
            with suppress(Exception):
                recorder(task_id, max(0, int(counts.get(source, 0))))

    def _enqueue_task(
        self,
        task_type: str,
        payload: dict[str, object],
        *,
        daily_budget: int,
    ) -> str | None:
        expire = getattr(self.task_queue, "expire_stale_pending", None)
        if callable(expire):
            with suppress(Exception):
                expire((task_type,), older_than_seconds=max(60.0, self.wait_seconds))
        task_payload = dict(payload)
        task_payload.setdefault(
            "request_interval_seconds",
            max(0.0, float(self.poll_interval_seconds)),
        )
        return cast(
            "str | None",
            self.task_queue.enqueue_with_id(
                task_type,
                task_payload,
                daily_budget=max(0, int(daily_budget)),
            ),
        )

    def _daily_budget_for(self, source: str) -> int:
        values = {
            "search": self.daily_search_budget,
            "hot": self.daily_hot_budget,
            "feed": self.daily_feed_budget,
            "creator": self.daily_creator_budget,
            "related": self.daily_related_budget,
        }
        return int(values.get(source, 0))

    def _remaining_budget(self, source: str, daily_budget: int) -> int | None:
        loader = getattr(self.task_queue, "remaining_budget", None)
        if not callable(loader):
            return None if daily_budget <= 0 else max(0, int(daily_budget))
        try:
            return cast("int | None", loader(source, max(0, int(daily_budget))))
        except Exception:
            logger.debug("linuxdo producer: budget ledger read failed", exc_info=True)
            return 0

    def _retention_cursor(self) -> str:
        loader = getattr(self.task_queue, "discovery_cursor", None)
        if not callable(loader):
            return ""
        with suppress(Exception):
            return str(loader() or "").strip()
        return ""

    def _start_cursors(
        self,
        source: str,
        payload: dict[str, object],
    ) -> dict[str, dict[str, int]]:
        loader = getattr(self.task_queue, "discovery_page_cursor", None)
        inputs = _cursor_inputs(source, payload)
        if not callable(loader):
            return {key: {"page": 0, "offset": 0} for key in inputs}
        cursors: dict[str, dict[str, int]] = {}
        for key in inputs:
            with suppress(Exception):
                value = loader(source, "" if key == "default" else key)
                if isinstance(value, dict):
                    cursors[key] = {
                        "page": max(0, int(value.get("page", 0))),
                        "offset": max(0, int(value.get("offset", 0))),
                    }
            cursors.setdefault(key, {"page": 0, "offset": 0})
        return cursors

    def _commit_page_cursors(
        self,
        source: str,
        payload: dict[str, object],
        task_result: dict[str, Any],
    ) -> None:
        setter = getattr(self.task_queue, "set_discovery_page_cursor", None)
        raw = task_result.get("next_cursors")
        if not callable(setter) or not isinstance(raw, dict):
            return
        allowed = set(_cursor_inputs(source, payload))
        for key, position in raw.items():
            normalized_key = str(key).strip()
            if normalized_key not in allowed or not isinstance(position, dict):
                continue
            with suppress(Exception):
                setter(
                    source,
                    "" if normalized_key == "default" else normalized_key,
                    position,
                )

    async def _load_seed_values(self, loader: Any | None) -> list[str]:
        if loader is None:
            return []
        try:
            result = loader()
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.debug("linuxdo producer: seed loader failed", exc_info=True)
            return []
        if not isinstance(result, list | tuple | set):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for value in result:
            if not isinstance(value, str | int | float) or isinstance(value, bool):
                continue
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    def _finish_search_claims(
        self,
        coordinator: Any,
        claimed: list[Any],
        retained_items: list[dict[str, Any]],
        raw_items: list[dict[str, Any]],
        task_result: dict[str, Any],
    ) -> None:
        retained_keywords = {
            str(item.get("search_keyword", "")).strip()
            for item in retained_items
            if isinstance(item.get("search_keyword"), str)
            and str(item.get("search_keyword", "")).strip()
        }
        raw_keywords = {
            str(item.get("search_keyword", "")).strip()
            for item in raw_items
            if isinstance(item.get("search_keyword"), str)
            and str(item.get("search_keyword", "")).strip()
        }
        transient = _is_transient_task_result(task_result)
        used = [item for item in claimed if str(item.keyword).strip() in retained_keywords]
        remaining = [item for item in claimed if item not in used]
        overflow = [item for item in remaining if str(item.keyword).strip() in raw_keywords]
        empty = [item for item in remaining if item not in overflow]
        if used:
            coordinator.mark_used(used)
        self._rollback_claims(coordinator, overflow)
        if used or overflow:
            if transient:
                self._rollback_claims(coordinator, empty)
            elif empty:
                coordinator.mark_failed(empty)
            return
        if transient:
            self._rollback_claims(coordinator, claimed)
        else:
            coordinator.mark_failed(claimed)

    @staticmethod
    def _rollback_claims(coordinator: Any, claimed: list[Any]) -> None:
        for item in claimed:
            coordinator.rollback(item)

    async def _wait_for_task(self, task_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        pickup_deadline = loop.time() + max(0.0, float(self.wait_seconds))
        execution_deadline: float | None = None
        task: dict[str, Any] | None = None
        while True:
            raw_task = self.task_queue.get(task_id)
            task = dict(raw_task) if isinstance(raw_task, dict) else None
            status = str((task or {}).get("status", "")).strip()
            if status in {"completed", "failed"}:
                break
            if status == "in_progress" and execution_deadline is None:
                try:
                    raw_payload = json.loads(str((task or {}).get("payload_json") or "{}"))
                except json.JSONDecodeError:
                    raw_payload = {}
                payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
                execution_deadline = (
                    loop.time()
                    + linuxdo_task_timeout_seconds(str((task or {}).get("type", "")), payload)
                    + LINUXDO_TASK_RESULT_GRACE_SECONDS
                )
            deadline = execution_deadline if execution_deadline is not None else pickup_deadline
            if loop.time() >= deadline:
                error_code = (
                    "extension_result_timeout" if status == "in_progress" else "stale_pending"
                )
                fail = getattr(self.task_queue, "fail", None)
                if callable(fail):
                    with suppress(Exception):
                        fail(task_id, error=error_code)
                return {
                    "_task_status": "timeout",
                    "_last_status": status or "pending",
                    "error": error_code,
                }
            await asyncio.sleep(max(0.01, float(self.poll_interval_seconds)))

        try:
            parsed = json.loads(str((task or {}).get("result_json") or "{}"))
        except json.JSONDecodeError:
            parsed = {}
        result = dict(parsed) if isinstance(parsed, dict) else {}
        result.setdefault("_task_status", str((task or {}).get("status", "")))
        return result

    async def _kick_dispatcher(self) -> None:
        kick = self.kick or kick_linuxdo_task_dispatcher
        try:
            result = kick()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("linuxdo producer: task dispatcher kick failed", exc_info=True)

    def _stamp_run(self, discovered: int) -> None:
        record_producer_run(getattr(self, "database", None), "linuxdo", int(discovered))
        self._last_run_at = datetime.now(UTC)

    def _is_due(self) -> bool:
        if self.min_interval_minutes <= 0:
            return True
        database = getattr(self, "database", None)
        if ledger_available(database):
            return not producer_ran_within(database, "linuxdo", self.min_interval_minutes)
        if self._last_run_at is None:
            return True
        return datetime.now(UTC) - self._last_run_at >= timedelta(minutes=self.min_interval_minutes)

    def _candidate_pool_full(self) -> bool:
        return candidate_pool_full_for_source(
            self.candidate_pipeline,
            "linuxdo",
            logger=logger,
            label="linuxdo producer",
        )

    def _skip(self, reason: str) -> dict[str, object]:
        if reason != self._last_skip_reason:
            logger.info("linuxdo producer skip: reason=%s", reason)
        self._last_skip_reason = reason
        return {"discovered": 0, "reason": reason}


def build_linuxdo_discovery_producer(
    *,
    config: Any,
    database: Any,
    soul_engine: Any,
    candidate_pipeline: Any | None = None,
    keyword_fetch: Any | None = None,
    kick: Any | None = None,
    require_scheduler: bool = True,
) -> LinuxdoDiscoveryProducer | None:
    """Build a Linux.do producer for daemon or explicit CLI execution.

    Runtime callers keep ``require_scheduler=True`` so the scheduler master
    switch remains authoritative.  An explicit ``discover`` command passes
    ``False``: manually requested work must not be disabled by a daemon-only
    switch.
    """
    linuxdo_cfg = getattr(getattr(config, "sources", None), "linuxdo", None)
    if linuxdo_cfg is None or not bool(getattr(linuxdo_cfg, "enabled", False)):
        return None
    scheduler = getattr(config, "scheduler", None)
    if require_scheduler and not bool(getattr(scheduler, "enabled", True)):
        return None
    if not hasattr(database, "conn"):
        logger.info("linuxdo producer disabled: database does not expose sqlite connection")
        return None

    wait_seconds = float(getattr(linuxdo_cfg, "wait_seconds", 0) or 180.0)
    return LinuxdoDiscoveryProducer(
        task_queue=LinuxdoTaskQueue(database),
        database=database,
        soul_engine=soul_engine,
        enabled=True,
        sources=_normalize_sources(getattr(linuxdo_cfg, "source_modes", LINUXDO_SOURCE_ORDER)),
        min_interval_minutes=int(getattr(linuxdo_cfg, "min_interval_minutes", 3)),
        daily_search_budget=int(getattr(linuxdo_cfg, "daily_search_budget", 0)),
        daily_hot_budget=int(getattr(linuxdo_cfg, "daily_hot_budget", 0)),
        daily_feed_budget=int(getattr(linuxdo_cfg, "daily_feed_budget", 0)),
        daily_creator_budget=int(getattr(linuxdo_cfg, "daily_creator_budget", 0)),
        daily_related_budget=int(getattr(linuxdo_cfg, "daily_related_budget", 0)),
        wait_seconds=wait_seconds,
        poll_interval_seconds=max(
            0.1,
            float(getattr(linuxdo_cfg, "request_interval_seconds", 3)),
        ),
        max_items_per_keyword=max(
            1,
            int(getattr(linuxdo_cfg, "max_items_per_keyword", 20)),
        ),
        max_seed_count=max(1, int(getattr(linuxdo_cfg, "max_seed_count", 5))),
        candidate_pipeline=candidate_pipeline,
        keyword_fetch=keyword_fetch,
        creator_seed_loader=lambda: recent_linuxdo_creator_urls(database, limit=10),
        related_seed_loader=lambda: recent_linuxdo_related_urls(database, limit=10),
        kick=kick,
    )


def kick_linuxdo_task_dispatcher() -> None:
    """Best-effort wake-up for the connected extension dispatcher."""
    req = request.Request(
        "http://127.0.0.1:8420/api/sources/linuxdo/kick",
        method="POST",
        data=b"",
    )
    with suppress(error.URLError, TimeoutError, OSError):
        request.urlopen(req, timeout=1.0).close()


def _fallback_profile_keywords(profile: Any, limit: int) -> list[str]:
    preferences = getattr(profile, "preferences", None)
    interests = list(getattr(preferences, "interests", []) or [])
    out: list[str] = []
    seen: set[str] = set()
    for interest in interests:
        name = str(getattr(interest, "name", "") or interest).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _task_terminal_status(result: dict[str, Any]) -> str:
    marker = str(result.get("_openbiliclaw_terminal_status", "")).strip().lower()
    if marker:
        return marker
    task_status = str(result.get("_task_status", "")).strip().lower()
    if task_status == "failed":
        return "failed"
    if task_status == "timeout":
        return "timeout"
    return "ok"


def _aggregate_branch_failure_reason(errors: list[dict[str, str]]) -> str:
    text = " ".join(f"{row.get('status', '')} {row.get('error', '')}" for row in errors).lower()
    if "login_required" in text or "not_logged_in" in text:
        return "login_required"
    if "429" in text or "rate_limit" in text or "rate limit" in text:
        return "rate_limited"
    if "timeout" in text:
        return "timeout"
    if "challenge" in text or "access_blocked" in text or "http_403" in text:
        return "blocked"
    return "failed"


def _same_run_creator_urls(items: list[dict[str, Any]]) -> list[str]:
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_author_url

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value = linuxdo_author_url(item)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _same_run_related_urls(items: list[dict[str, Any]]) -> list[str]:
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_topic_id

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        topic_id = linuxdo_topic_id(item)
        value = f"https://linux.do/t/{topic_id}" if topic_id else ""
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _normalize_sources(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, list | tuple | set):
        raw = [str(part).strip() for part in value]
    else:
        raw = ["search"]
    selected = {source for source in raw if source in LINUXDO_SOURCE_ORDER}
    if not selected:
        selected.add("search")
    return tuple(source for source in LINUXDO_SOURCE_ORDER if source in selected)


def _ordered_strategies(sources: tuple[str, ...]) -> list[str]:
    return [
        LINUXDO_SOURCE_STRATEGIES[source]
        for source in sources
        if source in LINUXDO_SOURCE_STRATEGIES
    ]


def _cursor_inputs(source: str, payload: dict[str, object]) -> tuple[str, ...]:
    if source in {"hot", "feed"}:
        return ("default",)
    field = "keywords" if source == "search" else "creator_urls"
    values = payload.get(field)
    if source not in {"search", "creator"} or not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _strategy_source(strategy: str) -> str:
    normalized = str(strategy or "").strip()
    for source, candidate in LINUXDO_SOURCE_STRATEGIES.items():
        if candidate == normalized:
            return source
    return ""


def _rotated_source_order(sources: tuple[str, ...], cursor: str) -> tuple[str, ...]:
    ordered = tuple(source for source in LINUXDO_SOURCE_ORDER if source in sources)
    if not ordered or cursor not in ordered:
        return ordered
    start = (ordered.index(cursor) + 1) % len(ordered)
    return ordered[start:] + ordered[:start]


def _round_robin_contents(
    branches: dict[str, list[Any]],
    sources: tuple[str, ...],
    limit: int,
) -> list[Any]:
    """Fairly retain candidates across branches with stable global dedupe."""
    retained: list[Any] = []
    seen: set[str] = set()
    offsets = {source: 0 for source in sources}
    while len(retained) < max(0, int(limit)):
        progressed = False
        for source in sources:
            rows = branches.get(source, [])
            while offsets[source] < len(rows):
                candidate = rows[offsets[source]]
                offsets[source] += 1
                content_id = str(getattr(candidate, "content_id", "") or "").strip()
                if not content_id or content_id in seen:
                    continue
                seen.add(content_id)
                retained.append(candidate)
                progressed = True
                break
            if len(retained) >= max(0, int(limit)):
                break
        if not progressed:
            break
    return retained


def _aggregate_skip_reason(reasons: list[str]) -> str:
    if not reasons:
        return "no_sources"
    if all(reason == "budget_exhausted" for reason in reasons):
        return "budget_exhausted"
    return reasons[0]


def _is_transient_task_result(result: dict[str, Any]) -> bool:
    values = [
        result.get("_task_status"),
        result.get("error"),
        result.get("error_code"),
        result.get("reason"),
    ]
    debug = result.get("debug")
    if isinstance(debug, dict):
        values.extend(debug.get(key) for key in ("code", "error", "reason"))
        for group_key in ("input_errors", "scope_errors"):
            group = debug.get(group_key)
            if isinstance(group, dict):
                values.extend(group.values())
    joined = " ".join(str(value).strip().lower() for value in values if value is not None)
    return any(marker in joined for marker in _TRANSIENT_RESULT_MARKERS)


def _task_result_error_summary(result: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("error", "error_code", "reason"):
        value = str(result.get(key, "")).strip()
        if value:
            values.append(value)
    debug = result.get("debug")
    if isinstance(debug, dict):
        for group_key in ("input_errors", "scope_errors"):
            group = debug.get(group_key)
            if not isinstance(group, dict):
                continue
            values.extend(
                f"{str(name).strip()}={str(value).strip()}"
                for name, value in group.items()
                if str(value).strip()
            )
    return "; ".join(values)[:500]


# Readable alias for callers that preserve the product's ``Linux.do`` case.
LinuxDoDiscoveryProducer = LinuxdoDiscoveryProducer
