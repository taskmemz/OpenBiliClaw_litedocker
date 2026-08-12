from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .identity import is_native_save_local_only
from .models import (
    NATIVE_SAVE_TERMINAL_STATUSES,
    NativeSaveAction,
    NativeSaveResult,
    NativeSaveStatus,
    SavedItemInput,
    SavedListKind,
    SavedMembershipResult,
    SavedSyncBatchResult,
)
from .router import InvalidNativeSaveAdapterResultError, UnsupportedNativeSaveError

if TYPE_CHECKING:
    from openbiliclaw.storage.database import Database

    from .models import NativeSaveRoute
    from .router import NativeSaveRouter

TaskStarter = Callable[[str, Coroutine[Any, Any, Any]], asyncio.Task[Any]]

_ACTIVE_STATUSES = frozenset({"pending"})
_MAX_ADAPTER_TIMEOUT_SECONDS = 240.0

logger = logging.getLogger(__name__)


def _is_transient_sqlite_lock(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


@dataclass(slots=True)
class _TaskRunLockEntry:
    lock: asyncio.Lock
    users: int = 0


class _NativeSaveClaimLostError(RuntimeError):
    """The execution lease no longer belongs to this adapter call."""


class _NativeSaveAttemptDetachedError(RuntimeError):
    """The caller returned while a fenced adapter coroutine finishes."""


class _NativeSaveDetachedCancellationError(asyncio.CancelledError):
    """Cancellation propagated after handing the live lease to a watchdog."""


class _NativeSaveTaskRunnerOwnershipLostError(RuntimeError):
    """The batch heartbeat no longer owns any active rows."""


class _NativeSaveItemHeartbeatError(RuntimeError):
    """The item heartbeat failed while adapter I/O was still live."""


class SavedSyncService:
    """Persist local membership first, then coordinate optional native saves."""

    def __init__(
        self,
        database: Database,
        router: NativeSaveRouter,
        task_starter: TaskStarter | None = None,
        *,
        claim_heartbeat_interval_seconds: float = 30.0,
        task_heartbeat_interval_seconds: float = 30.0,
        adapter_timeout_seconds: float = 240.0,
    ) -> None:
        self._database = database
        self._router = router
        self._task_starter = task_starter
        self._claim_heartbeat_interval_seconds = max(
            0.001,
            float(claim_heartbeat_interval_seconds),
        )
        self._task_heartbeat_interval_seconds = max(
            0.001,
            float(task_heartbeat_interval_seconds),
        )
        self._adapter_timeout_seconds = min(
            _MAX_ADAPTER_TIMEOUT_SECONDS,
            max(0.001, float(adapter_timeout_seconds)),
        )
        self._task_run_locks: dict[str, _TaskRunLockEntry] = {}
        self._detached_attempts: set[asyncio.Task[None]] = set()

    def save_local(
        self,
        list_kind: SavedListKind,
        item: SavedItemInput,
        note: str = "",
        auto_sync: bool = False,
    ) -> SavedMembershipResult:
        """Commit a local save before optionally creating a native-sync task."""
        self._database.upsert_saved_membership(list_kind, item, note)
        current = self._database.ensure_native_save_state(
            list_kind,
            item.item_key,
            requested_action=list_kind,
        )
        if is_native_save_local_only(item.platform):
            self._database.upsert_native_save_state(
                list_kind,
                item.item_key,
                requested_action=list_kind,
                status="unsupported",
                last_error_code="local_only_source",
                last_error_message="This source supports local saves only",
            )
            return SavedMembershipResult(
                saved=True,
                item_key=item.item_key,
                sync_status="unsupported",
            )
        if not auto_sync:
            return SavedMembershipResult(
                saved=True,
                item_key=item.item_key,
                sync_status=cast("NativeSaveStatus", str(current["status"])),
                sync_task_id=str(current["task_id"]),
            )

        created = self.create_sync_task(list_kind, [item.item_key], "auto")
        if created.items:
            created_item = created.items[0]
            return SavedMembershipResult(
                saved=True,
                item_key=item.item_key,
                sync_status=created_item.status,
                sync_task_id=created.task_id,
            )

        row = self._database.get_saved_membership(list_kind, item.item_key)
        status = cast("NativeSaveStatus", row["sync_status"] if row is not None else "pending")
        task_id = str(row["sync_task_id"]) if row is not None else ""
        return SavedMembershipResult(
            saved=True,
            item_key=item.item_key,
            sync_status=status,
            sync_task_id=task_id,
        )

    def create_sync_task(
        self,
        list_kind: SavedListKind,
        item_keys: Sequence[str],
        trigger: str,
    ) -> SavedSyncBatchResult:
        """Persist one pending task for selected eligible memberships."""
        selected_keys: Sequence[str] | None
        if item_keys:
            selected_keys = tuple(
                dict.fromkeys(item_key.strip() for item_key in item_keys if item_key.strip())
            )
            if not selected_keys:
                raise ValueError("item_keys must contain at least one non-blank key")
        else:
            selected_keys = None
        if self._selection_is_exclusively_local_only(list_kind, selected_keys):
            raise ValueError("local-only saved items cannot create native sync tasks")
        task_id = str(uuid.uuid4())
        self._database.release_stale_pending_native_sync_tasks(list_kind, selected_keys)
        self._database.reconcile_stale_native_save_claims_for_list(
            list_kind,
            selected_keys,
        )
        snapshot_rows = self._database.create_native_sync_task_snapshot(
            list_kind,
            selected_keys,
            task_id,
            trigger,
        )
        items = [self._result_from_row(row) for row in snapshot_rows]
        has_live_items = any(bool(row["is_live"]) for row in snapshot_rows)

        result = SavedSyncBatchResult(task_id=task_id, items=tuple(items))
        if has_live_items and self._task_starter is not None:
            coro = self.run_sync_task(task_id)
            try:
                self._task_starter(f"saved-sync:{trigger}:{task_id}", coro)
            except BaseException:
                coro.close()
                self._database.release_native_sync_task(task_id)
                self._database.discard_native_sync_task(task_id)
                raise
        return result

    def _selection_is_exclusively_local_only(
        self,
        list_kind: SavedListKind,
        item_keys: Sequence[str] | None,
    ) -> bool:
        selected = set(item_keys) if item_keys is not None else None
        matched: set[str] = set()
        offset = 0
        while True:
            rows = self._database.list_saved_memberships(list_kind, limit=200, offset=offset)
            if not rows:
                break
            for row in rows:
                item_key = str(row.get("item_key", ""))
                if selected is not None and item_key not in selected:
                    continue
                matched.add(item_key)
                if not is_native_save_local_only(str(row.get("source_platform", ""))):
                    return False
            if selected is not None and matched == selected:
                return bool(matched)
            if len(rows) < 200:
                break
            offset += len(rows)
        return bool(matched) and (selected is None or matched == selected)

    def validate_native_save_selection(
        self,
        list_kind: SavedListKind,
        item_key: str,
    ) -> tuple[SavedItemInput, NativeSaveRoute]:
        """Resolve one existing membership without creating or claiming a task."""
        normalized_key = item_key.strip()
        row = self._database.get_saved_membership(list_kind, normalized_key)
        if row is None:
            raise ValueError("saved membership does not exist")
        item = self._item_from_row(row)
        if item.item_key != normalized_key:
            raise ValueError("saved membership identity is not canonical")
        _adapter, route = self._router.route(item.platform, list_kind)
        return item, route

    async def run_sync_task(self, task_id: str) -> SavedSyncBatchResult:
        """Execute persisted task rows sequentially within each platform group."""
        task_id = self._validated_task_id(task_id)
        entry = self._task_run_locks.setdefault(
            task_id,
            _TaskRunLockEntry(lock=asyncio.Lock()),
        )
        entry.users += 1
        try:
            async with entry.lock:
                runner_id = str(uuid.uuid4())
                if not self._database.claim_native_sync_task_runner(task_id, runner_id):
                    return self.get_sync_task(task_id)
                self._database.reconcile_stale_native_save_claims(task_id)
                task_heartbeat = asyncio.create_task(self._heartbeat_sync_task(task_id, runner_id))
                work: asyncio.Future[list[None]] = asyncio.gather(
                    *(
                        self._run_platform_group(group, runner_id)
                        for group in self._group_task_rows(task_id)
                    )
                )
                waiters: set[asyncio.Future[Any]] = {work, task_heartbeat}
                try:
                    done, _ = await asyncio.wait(
                        waiters,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if task_heartbeat in done:
                        try:
                            await task_heartbeat
                        except _NativeSaveTaskRunnerOwnershipLostError:
                            current = self.get_sync_task(task_id)
                            if all(
                                item.status not in {"pending", "syncing"} for item in current.items
                            ):
                                work.cancel()
                                await asyncio.gather(work, return_exceptions=True)
                                return current
                            if work in done:
                                await work
                                return self.get_sync_task(task_id)
                        except Exception as exc:
                            logger.warning(
                                "Native save task heartbeat failed (%s)",
                                type(exc).__name__,
                            )
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                        raise RuntimeError("Native save task heartbeat failed") from None
                    await work
                    return self.get_sync_task(task_id)
                finally:
                    if not work.done():
                        work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    task_heartbeat.cancel()
                    await asyncio.gather(task_heartbeat, return_exceptions=True)
                    self._database.release_pending_native_sync_task(task_id, runner_id)
        finally:
            entry.users -= 1
            if entry.users == 0 and self._task_run_locks.get(task_id) is entry:
                del self._task_run_locks[task_id]

    def get_sync_task(self, task_id: str) -> SavedSyncBatchResult:
        """Reconstruct a batch entirely from persisted native-save states."""
        task_id = self._validated_task_id(task_id)
        self._database.release_stale_pending_native_sync_task(task_id)
        self._database.reconcile_stale_native_save_claims(task_id)
        rows = self._database.list_native_sync_task_items(task_id)
        items = tuple(self._result_from_row(row) for row in rows)
        return SavedSyncBatchResult(task_id=task_id, items=items)

    def has_sync_task(self, task_id: str) -> bool:
        """Return whether a durable task exists, including a zero-item batch."""
        return self._database.native_sync_task_exists(self._validated_task_id(task_id))

    def _group_task_rows(self, task_id: str) -> tuple[list[dict[str, Any]], ...]:
        rows = self._database.list_native_save_states_by_task(task_id)
        grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped_rows[str(row["source_platform"])].append(row)
        return tuple(grouped_rows.values())

    async def _run_platform_group(
        self,
        rows: list[dict[str, Any]],
        runner_id: str,
    ) -> None:
        for row in rows:
            if str(row["status"]) not in _ACTIVE_STATUSES:
                continue
            await self._run_item(row, runner_id)

    async def _run_item(self, row: dict[str, Any], runner_id: str) -> None:
        item = self._item_from_row(row)
        list_kind = cast("SavedListKind", str(row["list_kind"]))
        requested_action = cast("NativeSaveAction", str(row["requested_action"]))
        task_id = str(row["task_id"])
        execution_id = str(uuid.uuid4())
        if not self._database.claim_native_save_item(
            list_kind,
            item.item_key,
            task_id,
            runner_id,
            execution_id,
        ):
            return
        try:
            adapter, route = self._router.route(item.platform, requested_action)
        except InvalidNativeSaveAdapterResultError:
            await self._persist_result(
                list_kind,
                NativeSaveResult(
                    item_key=item.item_key,
                    status="failed",
                    resolved_action=requested_action,
                    resolved_target="",
                    error_code="invalid_adapter_result",
                    error_message="Native save adapter returned an invalid target",
                ),
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
            )
            return
        except UnsupportedNativeSaveError:
            await self._persist_result(
                list_kind,
                NativeSaveResult(
                    item_key=item.item_key,
                    status="unsupported",
                    resolved_action=requested_action,
                    resolved_target="",
                    error_code="unsupported_adapter_missing",
                    error_message="Native save is unsupported for this platform or action",
                ),
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
            )
            return
        except Exception:
            await self._persist_result(
                list_kind,
                NativeSaveResult(
                    item_key=item.item_key,
                    status="failed",
                    resolved_action=requested_action,
                    resolved_target="",
                    error_code="adapter_exception",
                    error_message="Native save failed",
                ),
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
            )
            return

        route_persisted = self._database.update_native_save_claim_route(
            list_kind,
            item.item_key,
            task_id,
            execution_id,
            resolved_action=route.resolved_action,
            resolved_target=route.resolved_target,
        )
        if not route_persisted:
            return
        try:
            adapter_result = await self._save_with_live_claim(
                adapter.save(item, route),
                list_kind=list_kind,
                item_key=item.item_key,
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
                resolved_action=route.resolved_action,
                resolved_target=route.resolved_target,
            )
            result = self._normalize_adapter_result(
                item.item_key,
                adapter_result,
                resolved_action=route.resolved_action,
                resolved_target=route.resolved_target,
            )
        except _NativeSaveDetachedCancellationError:
            raise
        except _NativeSaveItemHeartbeatError:
            raise
        except asyncio.CancelledError:
            await self._persist_result(
                list_kind,
                NativeSaveResult(
                    item_key=item.item_key,
                    status="failed",
                    resolved_action=route.resolved_action,
                    resolved_target=route.resolved_target,
                    error_code="interrupted",
                    error_message="Native save was interrupted",
                ),
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
            )
            raise
        except _NativeSaveAttemptDetachedError:
            return
        except _NativeSaveClaimLostError:
            return
        except Exception:
            result = NativeSaveResult(
                item_key=item.item_key,
                status="failed",
                resolved_action=route.resolved_action,
                resolved_target=route.resolved_target,
                error_code="adapter_exception",
                error_message="Native save failed",
            )
        await self._persist_result(
            list_kind,
            result,
            task_id=task_id,
            execution_id=execution_id,
            requested_action=requested_action,
        )

    async def _save_with_live_claim(
        self,
        save_coro: Coroutine[Any, Any, NativeSaveResult],
        *,
        list_kind: SavedListKind,
        item_key: str,
        task_id: str,
        execution_id: str,
        requested_action: NativeSaveAction,
        resolved_action: NativeSaveAction,
        resolved_target: str,
    ) -> NativeSaveResult:
        heartbeat = asyncio.create_task(
            self._heartbeat_claim(list_kind, item_key, task_id, execution_id)
        )
        save_task = asyncio.create_task(save_coro)
        try:
            done, _ = await asyncio.wait(
                {heartbeat, save_task},
                timeout=self._adapter_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            save_task.cancel()
            interrupted = NativeSaveResult(
                item_key=item_key,
                status="failed",
                resolved_action=resolved_action,
                resolved_target=resolved_target,
                error_code="interrupted",
                error_message="Native save was interrupted",
            )
            await asyncio.sleep(0)
            if save_task.done():
                heartbeat.cancel()
                await asyncio.gather(heartbeat, save_task, return_exceptions=True)
                final_result = interrupted
                if not save_task.cancelled():
                    try:
                        adapter_result = save_task.result()
                    except BaseException:
                        pass
                    else:
                        final_result = self._normalize_adapter_result(
                            item_key,
                            adapter_result,
                            resolved_action=resolved_action,
                            resolved_target=resolved_target,
                        )
                await self._persist_result(
                    list_kind,
                    final_result,
                    task_id=task_id,
                    execution_id=execution_id,
                    requested_action=requested_action,
                )
            else:
                self._detach_live_attempt(
                    heartbeat,
                    save_task,
                    list_kind=list_kind,
                    item_key=item_key,
                    task_id=task_id,
                    execution_id=execution_id,
                    requested_action=requested_action,
                    result=interrupted,
                )
            raise _NativeSaveDetachedCancellationError from None
        if not done:
            save_task.cancel()
            self._detach_live_attempt(
                heartbeat,
                save_task,
                list_kind=list_kind,
                item_key=item_key,
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
                result=NativeSaveResult(
                    item_key=item_key,
                    status="failed",
                    resolved_action=resolved_action,
                    resolved_target=resolved_target,
                    error_code="adapter_timeout",
                    error_message="Native save timed out",
                ),
            )
            raise _NativeSaveAttemptDetachedError
        if heartbeat in done:
            save_task.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            heartbeat_failure = NativeSaveResult(
                item_key=item_key,
                status="failed",
                resolved_action=resolved_action,
                resolved_target=resolved_target,
                error_code="item_heartbeat_failed",
                error_message="Native save item heartbeat failed",
            )
            await asyncio.sleep(0)
            if save_task.done():
                final_result = heartbeat_failure
                if not save_task.cancelled():
                    try:
                        adapter_result = save_task.result()
                    except BaseException:
                        pass
                    else:
                        final_result = self._normalize_adapter_result(
                            item_key,
                            adapter_result,
                            resolved_action=resolved_action,
                            resolved_target=resolved_target,
                        )
                await self._persist_result(
                    list_kind,
                    final_result,
                    task_id=task_id,
                    execution_id=execution_id,
                    requested_action=requested_action,
                )
            else:
                self._detach_live_attempt(
                    heartbeat,
                    save_task,
                    list_kind=list_kind,
                    item_key=item_key,
                    task_id=task_id,
                    execution_id=execution_id,
                    requested_action=requested_action,
                    result=heartbeat_failure,
                )
            raise _NativeSaveItemHeartbeatError("Native save item heartbeat failed") from None
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        return await save_task

    def _detach_live_attempt(
        self,
        heartbeat: asyncio.Task[None],
        save_task: asyncio.Task[NativeSaveResult],
        *,
        list_kind: SavedListKind,
        item_key: str,
        task_id: str,
        execution_id: str,
        requested_action: NativeSaveAction,
        result: NativeSaveResult,
    ) -> None:
        watchdog = asyncio.create_task(
            self._finish_detached_attempt(
                heartbeat,
                save_task,
                list_kind=list_kind,
                item_key=item_key,
                task_id=task_id,
                execution_id=execution_id,
                requested_action=requested_action,
                result=result,
            )
        )
        self._detached_attempts.add(watchdog)
        watchdog.add_done_callback(self._consume_detached_attempt)

    async def _finish_detached_attempt(
        self,
        heartbeat: asyncio.Task[None],
        save_task: asyncio.Task[NativeSaveResult],
        *,
        list_kind: SavedListKind,
        item_key: str,
        task_id: str,
        execution_id: str,
        requested_action: NativeSaveAction,
        result: NativeSaveResult,
    ) -> None:
        retrying_heartbeat = asyncio.create_task(
            self._heartbeat_claim_with_retry(
                list_kind,
                item_key,
                task_id,
                execution_id,
            )
        )
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        final_result = result
        try:
            adapter_result = await save_task
        except BaseException:
            pass
        else:
            final_result = self._normalize_adapter_result(
                result.item_key,
                adapter_result,
                resolved_action=result.resolved_action,
                resolved_target=result.resolved_target,
            )
        finally:
            retrying_heartbeat.cancel()
            await asyncio.gather(retrying_heartbeat, save_task, return_exceptions=True)
        await self._persist_result(
            list_kind,
            final_result,
            task_id=task_id,
            execution_id=execution_id,
            requested_action=requested_action,
        )

    def _consume_detached_attempt(self, task: asyncio.Task[None]) -> None:
        self._detached_attempts.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _normalize_adapter_result(
        item_key: str,
        adapter_result: object,
        *,
        resolved_action: NativeSaveAction,
        resolved_target: str,
    ) -> NativeSaveResult:
        invalid_result = NativeSaveResult(
            item_key=item_key,
            status="failed",
            resolved_action=resolved_action,
            resolved_target=resolved_target,
            error_code="invalid_adapter_result",
            error_message="Native save adapter returned a nonterminal status",
        )
        if not isinstance(adapter_result, NativeSaveResult):
            return invalid_result
        try:
            if (
                not isinstance(adapter_result.status, str)
                or adapter_result.status not in NATIVE_SAVE_TERMINAL_STATUSES
            ):
                return invalid_result
            error_code = (
                adapter_result.error_code if isinstance(adapter_result.error_code, str) else ""
            )
            error_message = (
                adapter_result.error_message
                if isinstance(adapter_result.error_message, str)
                else ""
            )
            return NativeSaveResult(
                item_key=item_key,
                status=adapter_result.status,
                resolved_action=resolved_action,
                resolved_target=resolved_target,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            return invalid_result

    @staticmethod
    def _validated_task_id(value: str) -> str:
        task_id = value.strip()
        if not task_id:
            raise ValueError("task_id must not be blank")
        return task_id

    async def _heartbeat_claim(
        self,
        list_kind: SavedListKind,
        item_key: str,
        task_id: str,
        execution_id: str,
    ) -> None:
        while True:
            await asyncio.sleep(self._claim_heartbeat_interval_seconds)
            try:
                alive = await asyncio.to_thread(
                    self._database.heartbeat_native_save_claim,
                    list_kind,
                    item_key,
                    task_id,
                    execution_id,
                )
            except Exception as exc:
                if _is_transient_sqlite_lock(exc):
                    continue
                raise
            if not alive:
                return

    async def _heartbeat_claim_with_retry(
        self,
        list_kind: SavedListKind,
        item_key: str,
        task_id: str,
        execution_id: str,
    ) -> None:
        delay = self._claim_heartbeat_interval_seconds
        while True:
            await asyncio.sleep(delay)
            try:
                alive = await asyncio.to_thread(
                    self._database.heartbeat_native_save_claim,
                    list_kind,
                    item_key,
                    task_id,
                    execution_id,
                )
            except Exception:
                delay = min(max(delay * 2.0, 0.01), 1.0)
                continue
            if not alive:
                return
            delay = self._claim_heartbeat_interval_seconds

    async def _heartbeat_sync_task(self, task_id: str, runner_id: str) -> None:
        while True:
            await asyncio.sleep(self._task_heartbeat_interval_seconds)
            try:
                alive = await asyncio.to_thread(
                    self._database.heartbeat_native_sync_task,
                    task_id,
                    runner_id,
                )
            except Exception as exc:
                if _is_transient_sqlite_lock(exc):
                    continue
                raise
            if alive == 0:
                raise _NativeSaveTaskRunnerOwnershipLostError

    async def _persist_result(
        self,
        list_kind: SavedListKind,
        result: NativeSaveResult,
        *,
        task_id: str,
        execution_id: str,
        requested_action: NativeSaveAction,
    ) -> None:
        delay = 0.05
        while True:
            try:
                await asyncio.to_thread(
                    self._database.complete_native_save_claim,
                    list_kind,
                    result.item_key,
                    task_id,
                    execution_id,
                    requested_action=requested_action,
                    resolved_action=result.resolved_action,
                    resolved_target=result.resolved_target,
                    status=result.status,
                    last_error_code=result.error_code,
                    last_error_message=result.error_message,
                )
                return
            except Exception as exc:
                if not _is_transient_sqlite_lock(exc):
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 1.0)

    @staticmethod
    def _item_from_row(row: dict[str, Any]) -> SavedItemInput:
        return SavedItemInput(
            source_platform=str(row["source_platform"]),
            content_id=str(row["content_id"]),
            content_url=str(row["content_url"]),
            content_type=str(row["content_type"]),
            title=str(row["title"]),
            author_name=str(row["author_name"]),
            cover_url=str(row["cover_url"]),
        )

    @staticmethod
    def _result_from_row(row: dict[str, Any]) -> NativeSaveResult:
        requested_action = cast("NativeSaveAction", str(row["requested_action"]))
        resolved_action = cast(
            "NativeSaveAction",
            str(row["resolved_action"]) or requested_action,
        )
        return NativeSaveResult(
            item_key=str(row["item_key"]),
            status=cast("NativeSaveStatus", str(row["status"])),
            resolved_action=resolved_action,
            resolved_target=str(row["resolved_target"]),
            error_code=str(row["last_error_code"]),
            error_message=str(row["last_error_message"]),
        )
