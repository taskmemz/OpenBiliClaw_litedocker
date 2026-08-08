"""Opt-in real-request acceptance for immediate dislike output exclusion.

The harness uses an isolated temporary MemoryManager/SQLite database while
loading the machine's configured provider/source routes. It deliberately
proves both sides of the product boundary:

1. a same-topic query still reaches the real Bilibili transport after the
   dislike is durable;
2. the matching recommendation disappears from the real FastAPI endpoint
   immediately, without Soul rebuild or recommendation-cache TTL expiry.

Only hashes, route metadata, counts, timestamps, and assertion booleans are
written to the artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from openbiliclaw.api.app import create_app  # noqa: E402
from openbiliclaw.bilibili.api import BilibiliAPIClient  # noqa: E402
from openbiliclaw.bilibili.auth import resolve_runtime_cookie  # noqa: E402
from openbiliclaw.config import load_config  # noqa: E402
from openbiliclaw.discovery.keyword_digest import profile_kw_digest  # noqa: E402
from openbiliclaw.discovery.strategies.search import SearchStrategy  # noqa: E402
from openbiliclaw.llm import build_llm_registry  # noqa: E402
from openbiliclaw.llm.service import LLMService, module_overrides_from_config  # noqa: E402
from openbiliclaw.memory.manager import MemoryManager  # noqa: E402
from openbiliclaw.runtime.keyword_planner import KeywordPlanner  # noqa: E402
from openbiliclaw.soul.engine import SoulEngine  # noqa: E402
from openbiliclaw.soul.profile import OnionProfile  # noqa: E402
from openbiliclaw.storage.database import Database  # noqa: E402

INCIDENT_QUERY = "腰椎保护 实操训练"
DISLIKE_TOPIC = "运动康复"
SAFE_BVID = "dislike-output-safe-control"
OPT_IN_ENV = "OPENBILICLAW_RUN_REAL_DISLIKE_OUTPUT_EVAL"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_route(registry: Any) -> dict[str, str]:
    provider = str(getattr(registry, "default_provider", "") or "")
    model = ""
    try:
        provider_obj = registry.get(provider)
        model = str(getattr(provider_obj, "_model", "") or getattr(provider_obj, "model", ""))
    except Exception:
        model = ""
    return {
        "provider": provider,
        "provider_type": str(getattr(registry, "provider_type", lambda _name: "")(provider)),
        "model": model,
    }


@contextmanager
def _serve_real_http(app: Any) -> Any:
    """Serve the isolated app on a real loopback TCP socket."""

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        name="openbiliclaw-real-dislike-eval-api",
        daemon=True,
    )
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    try:
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{base_url}/api/health", timeout=1.0, trust_env=False)
                if response.status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("isolated_api_start_timeout")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        server_socket.close()


@dataclass
class _RecordingBilibiliClient:
    client: BilibiliAPIClient
    calls: list[dict[str, object]] = field(default_factory=list)

    async def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        order: str = "totalrank",
    ) -> list[dict[str, Any]]:
        record: dict[str, object] = {
            "query_hash": _digest(keyword),
            "started_at": _now(),
            "page": int(page),
            "page_size": int(page_size),
        }
        self.calls.append(record)
        try:
            rows = await self.client.search(
                keyword,
                page=page,
                page_size=page_size,
                order=order,
            )
        except Exception as exc:
            record.update(
                status="error",
                error_type=type(exc).__name__,
                completed_at=_now(),
            )
            raise
        record.update(status="returned", result_count=len(rows), completed_at=_now())
        return rows

    def search_cooldown_remaining(self) -> float:
        return self.client.search_cooldown_remaining()

    async def close(self) -> None:
        await self.client.close()


def _seed_recommendation(
    database: Database,
    *,
    bvid: str,
    content_id: str,
    title: str,
    topic: str,
    source_platform: str = "bilibili",
) -> int:
    database.cache_content(
        bvid,
        content_id=content_id,
        source_platform=source_platform,
        content_url=f"https://www.bilibili.com/video/{content_id}",
        title=title,
        topic_key=topic,
        topic_group=topic,
        pool_topic_label=topic,
        relevance_score=0.90,
        source="real-bilibili-search",
    )
    return database.insert_recommendation(
        bvid,
        confidence=0.90,
        expression="isolated acceptance row",
        topic=topic,
    )


async def _run(config_path: Path | None) -> dict[str, object]:
    config = load_config(config_path)
    registry = build_llm_registry(config)
    started_at = _now()

    with tempfile.TemporaryDirectory(prefix="openbiliclaw-dislike-output-eval-") as temp_dir:
        isolated_dir = Path(temp_dir)
        database = Database(isolated_dir / "openbiliclaw.db")
        database.initialize()
        memory = MemoryManager(isolated_dir, database=database)
        memory.initialize()
        soul_layer = memory.get_layer("soul")
        soul_layer.data.update(OnionProfile().to_dict())
        soul_layer.save()

        llm_service = LLMService(
            registry=registry,
            memory=memory,
            module_overrides=module_overrides_from_config(config),
            concurrency=config.llm.concurrency,
        )
        soul = SoulEngine(
            llm=registry,
            memory=memory,
            database=database,
            module_overrides=module_overrides_from_config(config),
            llm_concurrency=config.llm.concurrency,
            profile_consolidation_enabled=False,
        )

        # Make one genuine external request first so the recommendation row is
        # backed by a real source result, not a mocked transport.
        runtime_cookie = resolve_runtime_cookie(
            data_dir=config.data_path,
            configured_cookie=config.bilibili.cookie,
        )
        recording_client = _RecordingBilibiliClient(
            BilibiliAPIClient(
                cookie=runtime_cookie,
                proxy=config.bilibili.proxy or None,
            )
        )
        strategy = SearchStrategy(
            bilibili_client=recording_client,
            llm_service=llm_service,
            llm_evaluation=False,
            page_size=3,
            max_pages=1,
        )
        before_profile = await soul.get_profile()
        raw_items = await strategy.discover(
            before_profile,
            limit=3,
            queries=[INCIDENT_QUERY],
        )
        if not raw_items:
            await recording_client.close()
            raise RuntimeError("real_bilibili_search_returned_no_candidates")
        digest_before_dislike = profile_kw_digest(before_profile)
        database.insert_pending_keywords(
            "bilibili",
            [INCIDENT_QUERY],
            digest_before_dislike,
        )
        real_item = raw_items[0]
        real_content_id = str(real_item.content_id or real_item.bvid).strip()
        real_storage_bvid = str(real_item.bvid).strip()
        _seed_recommendation(
            database,
            bvid=real_storage_bvid,
            content_id=real_content_id,
            title=real_item.title,
            topic=DISLIKE_TOPIC,
        )
        _seed_recommendation(
            database,
            bvid=SAFE_BVID,
            content_id="dislike-output-safe-control",
            title="SQLite query planner internals",
            topic="数据库",
            source_platform="test-control",
        )

        app = create_app(memory_manager=memory, database=database, soul_engine=soul)
        with (
            _serve_real_http(app) as base_url,
            httpx.Client(
                base_url=base_url,
                timeout=10.0,
                trust_env=False,
            ) as client,
        ):
            first = client.get("/api/recommendations")
            first.raise_for_status()
            first_ids = [str(item.get("bvid", "")) for item in first.json().get("items", [])]

            # Commit the flat preference only. Deliberately do not rebuild Soul and
            # do not await semantic purge or the recommendation snapshot TTL.
            preference = memory.get_layer("preference")
            preference.data["disliked_topics"] = [DISLIKE_TOPIC]
            preference.save()

            # Search again after the dislike is durable. Ordinary dislikes must not
            # expire the already-pending query or suppress its outbound request.
            effective_profile = await soul.get_profile()
            digest_after_dislike = profile_kw_digest(effective_profile)
            planner = KeywordPlanner(
                llm_service=llm_service,
                database=database,
                config=config,
                soul_engine=soul,
            )
            planner._reconcile_pending_inventory(
                digest=digest_after_dislike,
                hints_by_platform={"bilibili": {"avoid_topics": []}},
            )
            claimed_rows = database.claim_keywords("bilibili", 1)
            claimed_query = str(claimed_rows[0].get("keyword", "")) if claimed_rows else ""
            searched_after_dislike = await strategy.discover(
                effective_profile,
                limit=3,
                queries=[claimed_query] if claimed_query else [],
            )
            if claimed_rows:
                database.mark_keyword_used(int(claimed_rows[0]["id"]))
            await recording_client.close()

            second = client.get("/api/recommendations")
            second.raise_for_status()
            second_ids = [str(item.get("bvid", "")) for item in second.json().get("items", [])]

        real_visible_before = real_storage_bvid in first_ids
        safe_visible_before = SAFE_BVID in first_ids
        real_hidden_after = real_storage_bvid not in second_ids
        safe_visible_after = SAFE_BVID in second_ids
        search_calls = [
            call
            for call in recording_client.calls
            if call.get("query_hash") == _digest(INCIDENT_QUERY)
        ]
        search_after_dislike_ok = len(search_calls) >= 2 and bool(searched_after_dislike)
        pending_query_survived = (
            digest_before_dislike != digest_after_dislike and claimed_query == INCIDENT_QUERY
        )
        passed = all(
            (
                real_visible_before,
                safe_visible_before,
                real_hidden_after,
                safe_visible_after,
                search_after_dislike_ok,
                pending_query_survived,
            )
        )
        return {
            "status": "passed" if passed else "assertion_failed",
            "started_at": started_at,
            "finished_at": _now(),
            "provider_route": _safe_route(registry),
            "query_hash": _digest(INCIDENT_QUERY),
            "dislike_digest": _digest(DISLIKE_TOPIC),
            "transport": {
                "source": "bilibili",
                "authenticated_route": bool(runtime_cookie),
                "calls": recording_client.calls,
                "same_topic_request_count": len(search_calls),
                "post_dislike_result_count": len(searched_after_dislike),
            },
            "api": {
                "transport": "loopback_tcp_http",
                "before_count": len(first_ids),
                "after_count": len(second_ids),
            },
            "assertions": {
                "real_candidate_visible_before_dislike": real_visible_before,
                "safe_control_visible_before_dislike": safe_visible_before,
                "real_candidate_hidden_immediately_after_dislike": real_hidden_after,
                "safe_control_remains_visible": safe_visible_after,
                "same_topic_real_search_still_runs_after_dislike": search_after_dislike_ok,
                "pending_query_survives_dislike_digest_change": pending_query_survived,
                "soul_rebuild_not_required": True,
                "snapshot_ttl_wait_not_required": True,
            },
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real Bilibili + isolated FastAPI immediate-dislike acceptance."
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional config.toml path.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/immediate-dislike-output-real-eval.json"),
        help="Privacy-safe JSON artifact path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get(OPT_IN_ENV, "").strip() != "1":
        report = {
            "status": "skipped",
            "reason": f"set {OPT_IN_ENV}=1 to authorize real external requests",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    try:
        report = asyncio.run(_run(args.config))
    except Exception as exc:
        report = {
            "status": "environmental_failure",
            "error_type": type(exc).__name__,
            "finished_at": _now(),
        }
        safe_error_codes = {
            "isolated_api_start_timeout",
            "real_bilibili_search_returned_no_candidates",
        }
        if str(exc) in safe_error_codes:
            report["error_code"] = str(exc)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
