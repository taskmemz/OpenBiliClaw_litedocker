"""Deterministic, private-data-free UI server for Chrome Web Store screenshots."""

from __future__ import annotations

import json
import mimetypes
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEMO_COVER_HOST = "covers.openbiliclaw.invalid"
DEMO_COVER_DIR = ROOT / "docs/images/chrome-web-store/demo-covers"


def _cover_url(filename: str) -> str:
    return f"https://{DEMO_COVER_HOST}/{filename}"


def _demo_cover_path(raw_url: str) -> Path | None:
    parsed = urlsplit(raw_url)
    if parsed.scheme != "https" or parsed.hostname != DEMO_COVER_HOST:
        return None
    candidate = (DEMO_COVER_DIR / Path(parsed.path).name).resolve()
    try:
        candidate.relative_to(DEMO_COVER_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _recommendations() -> list[dict[str, Any]]:
    rows = (
        (
            "bilibili",
            "系统设计不是画框：从一次真实重构说起",
            "工程漫游指南",
            "系统设计",
            "01-system-design.png",
        ),
        (
            "xiaohongshu",
            "把信息流变成自己的研究工作台",
            "认真生活实验室",
            "效率方法",
            "02-research-workflow.png",
        ),
        (
            "zhihu",
            "为什么长期兴趣比短期热点更值得建模？",
            "知识花园",
            "认知科学",
            "03-cognitive-science.png",
        ),
        (
            "reddit",
            "Local-first software: what actually matters",
            "r/LocalFirst",
            "本地优先",
            "04-local-first.png",
        ),
        (
            "youtube",
            "A Visual Guide to Recommendation Systems",
            "Signal & Craft",
            "推荐系统",
            "05-recommendation-systems.png",
        ),
        (
            "douyin",
            "三分钟看懂个人知识库的数据流",
            "产品显微镜",
            "产品设计",
            "06-knowledge-flow.png",
        ),
        (
            "twitter",
            "Seven practical notes on agent memory",
            "@open_notes",
            "Agent",
            "07-agent-memory.png",
        ),
    )
    items = [
        {
            "id": index,
            "bvid": f"demo-{platform}-{index}",
            "content_id": f"demo-{platform}-{index}",
            "content_url": f"https://example.invalid/{platform}/{index}",
            "source_platform": platform,
            "title": title,
            "up_name": author,
            "topic_label": topic,
            "cover_url": _cover_url(cover),
            "content_type": "video",
            "expression": "因为你持续关注高质量、可复用的方法论，同时愿意探索相邻主题。",
            "duration": 720,
            "view": 12800 + index * 137,
            "feedback_type": "",
        }
        for index, (platform, title, author, topic, cover) in enumerate(rows, 1)
    ]
    bangumi = {
        "id": 8,
        "bvid": "demo-bangumi-8",
        "content_id": "demo-bangumi-8",
        "content_url": "https://bgm.tv/subject/8",
        "source_platform": "bangumi",
        "title": "把长期兴趣放回时间线：一部作品的重看价值",
        "up_name": "Bangumi",
        "topic_label": "动画与叙事",
        "cover_url": "",
        "content_type": "subject",
        "body_text": "一部作品隔几年再看，常会显出初见时忽略的叙事层次与时代纹理。",
        "expression": "它与你持续关注的叙事结构相连，也提供了跨年份回看的新角度。",
        "feedback_type": "",
    }
    v2ex = {
        "id": 9,
        "bvid": "demo-v2ex-9",
        "content_id": "demo-v2ex-9",
        "content_url": "https://www.v2ex.com/t/123456",
        "source_platform": "v2ex",
        "title": "现在大家怎么管理 Agent 上下文？",
        "up_name": "alice",
        "author_name": "alice",
        "topic_label": "程序员",
        "tags": ["programmer", "程序员"],
        "cover_url": "",
        "content_type": "topic",
        "body_text": "从本地持久化、摘要压缩到跨设备迁移，大家分享了各自管理 Agent 长上下文的做法。",
        "reply_count": 36,
        "expression": "你最近持续关注本地运行、上下文压缩和可迁移性，这个讨论正好交汇三者。",
        "feedback_type": "",
    }
    return [v2ex, *items, bangumi]


def _delight_items() -> list[dict[str, Any]]:
    return [
        {
            "bvid": "demo-delight-local-first",
            "content_id": "demo-delight-local-first",
            "content_url": "https://example.invalid/bilibili/delight",
            "source_platform": "bilibili",
            "title": "本地优先，不只是隐私：把主动权留在自己的设备上",
            "delight_reason": "它把隐私、可迁移性与长期可控性放进同一套产品设计里。",
            "delight_hook": "刚好连接你最近的系统设计兴趣",
            "delight_score": 0.91,
            "cover_url": _cover_url("08-delight-local-first.png"),
            "state": "pending",
            "view_count": 28600,
            "like_count": 1900,
            "comment_count": 186,
        }
    ]


def _sources_status() -> dict[str, dict[str, Any]]:
    return {
        "bilibili": {
            "enabled": True,
            "state": "ready",
            "detail": "浏览器登录凭据已同步到本地后端。",
        },
        "xiaohongshu": {
            "enabled": True,
            "state": "ready",
            "detail": "本地已保存接入信息；不代表平台页面实时验证成功。",
        },
        "douyin": {
            "enabled": True,
            "state": "unverified",
            "detail": "已保存本地凭据，尚未发起平台实时验证。",
        },
        "youtube": {"enabled": True, "state": "no_auth", "detail": "公开内容发现无需登录。"},
        "twitter": {"enabled": True, "state": "ready", "detail": "浏览器会话已同步到本地后端。"},
        "zhihu": {"enabled": True, "state": "ready", "detail": "登录状态由插件本地检查并同步。"},
        "reddit": {
            "enabled": True,
            "state": "unverified",
            "detail": "本地已保存 Reddit 接入信息（未实时访问 Reddit 验证）。",
        },
        "bangumi": {
            "enabled": True,
            "state": "no_auth",
            "detail": "公开内容发现无需登录；公开用户名仅用于可选画像初始化。",
        },
        "v2ex": {
            "enabled": True,
            "state": "ready",
            "detail": "公开发现正常；浏览器登录身份已观察，PAT 为可选增强。",
        },
    }


def _config() -> dict[str, Any]:
    enabled = {key: {"enabled": True} for key in _sources_status()}
    return {
        "config": {
            "sources": enabled,
            "scheduler": {"enabled": True, "interval_minutes": 30},
            "llm": {
                "default_provider": "ollama",
                "ollama": {"model": "qwen3:8b", "base_url": "http://127.0.0.1:11434"},
                "embedding": {"provider": "ollama", "model": "nomic-embed-text"},
            },
            "recommendation": {"count": 12},
        }
    }


def _profile() -> dict[str, Any]:
    return {
        "initialized": True,
        "personality_portrait": "偏爱结构清晰、能落到实践的深度内容，也愿意为真正的新视角留出探索空间。",
        "core_traits": ["长期主义", "好奇而审慎", "重视可验证性"],
        "deep_needs": ["理解复杂系统", "持续积累", "保持自主判断"],
        "mbti": {"type": "INTJ"},
        "values": ["真实", "创造", "独立思考"],
        "motivational_drivers": ["把知识变成能力", "发现高信号信息"],
        "likes": ["系统设计", "AI 工程", "认知科学", "产品方法"],
        "dislikes": ["标题党", "重复搬运", "缺乏证据的结论"],
        "favorite_up_users": ["工程漫游指南", "知识花园"],
        "life_stage": "持续构建个人知识与创作系统",
        "current_phase": "从信息消费走向主动研究",
        "cognitive_style": ["先看证据", "偏好一手材料", "习惯跨领域连接"],
        "style": {
            "preferred_duration": "10–30 分钟",
            "preferred_pace": "信息密度高但不赶",
            "quality_sensitivity": 0.92,
            "humor_preference": 0.38,
            "depth_preference": 0.88,
        },
        "exploration_openness": 0.72,
        "speculative_interests": [
            {
                "domain": "本地优先软件",
                "reason": "最近多次停留在隐私与数据自主相关内容。",
                "status": "active",
                "confidence": 0.78,
            }
        ],
        "speculative_avoidances": [
            {
                "domain": "纯热点复述",
                "reason": "对缺少新增信息的内容经常快速跳过。",
                "status": "active",
                "confidence": 0.81,
            }
        ],
        "recent_cognition_updates": [],
        "active_insights": [],
    }


def demo_payload(path: str) -> tuple[int, Any]:
    """Return a sanitized response for a UI API path."""

    clean_path = urlsplit(path).path
    mapping: dict[str, Any] = {
        "/api/ping": {"ok": True},
        "/api/health": {"ok": True, "initialized": True, "embedding_ready": True},
        "/api/auth/status": {"enabled": False, "authenticated": True},
        "/api/recommendations": {"items": _recommendations()},
        "/api/runtime-status": {
            "initialized": True,
            "pool_available_count": 86,
            "pool_size": 112,
            "pool_refresh_state": "idle",
            "pool_source_shares": {
                "bilibili": 0.18,
                "xiaohongshu": 0.14,
                "douyin": 0.09,
                "youtube": 0.11,
                "twitter": 0.10,
                "zhihu": 0.11,
                "reddit": 0.09,
                "bangumi": 0.08,
                "v2ex": 0.10,
            },
            "configured_sources": {key: {"enabled": True} for key in _sources_status()},
            "unread_count": 2,
        },
        "/api/init-status": {
            "initialized": True,
            "running": False,
            "can_start": False,
            "reason": "already_initialized",
            "stages": [],
            "prerequisites": {
                "bilibili_logged_in": True,
                "llm_ready": True,
                "embedding_ready": True,
                "enabled_platforms": list(_sources_status()),
            },
        },
        "/api/profile-summary": _profile(),
        "/api/profile/edit-state": {"fields": {}, "updated_at": "2026-07-12T10:00:00+08:00"},
        "/api/config": _config(),
        "/api/sources/status": _sources_status(),
        "/api/sources/credentials": {
            key: {
                "available": key != "youtube",
                "label": "本地凭据",
                "detail": "仅保存在本机，演示中不展示内容。",
            }
            for key in _sources_status()
        },
        "/api/activity-feed": {"items": [], "has_more": False, "next_cursor": ""},
        "/api/delight/pending-batch": {"items": _delight_items()},
        "/api/notifications/pending": {"items": []},
        "/api/cognition-updates/pending": {"items": []},
        "/api/chat/turns": {"items": []},
        "/api/interest-probes/pending": {"items": []},
        "/api/avoidance-probes/pending": {"items": []},
        "/api/favorites": {"items": [], "total": 0},
        "/api/watch-later": {"items": [], "total": 0},
        "/api/qr-info": {"lan_ip": "127.0.0.1", "url": "http://127.0.0.1"},
        "/api/update-status": {"current_version": "0.3.163", "update_available": False},
        "/api/autostart-status": {"supported": True, "enabled": True},
    }
    if clean_path in mapping:
        return 200, mapping[clean_path]
    if clean_path.startswith("/api/chat/turns/"):
        return 200, {"status": "completed", "answer": "这是固定演示回复。"}
    if clean_path.startswith(("/api/favorites/", "/api/watch-later/")):
        return 200, {"saved": False}
    return 404, {"error": "demo_route_not_found", "path": clean_path}


class DemoServer:
    """Serve current static UIs and deterministic API responses on loopback."""

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.requests: list[str] = []

    def __enter__(self) -> str:
        owner = self

        class DemoHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def end_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OBC-Auth")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append(self.path)
                parsed_request = urlsplit(self.path)
                path = parsed_request.path
                if path == "/api/image-proxy":
                    raw_url = parse_qs(parsed_request.query).get("url", [""])[0]
                    cover_path = _demo_cover_path(raw_url)
                    if cover_path is None:
                        return self._json({"error": "demo_cover_not_found"}, 404)
                    return self._serve_file(cover_path)
                if path in {"/web", "/web/", "/web/index.html"}:
                    return self._serve_file(ROOT / "src/openbiliclaw/web/desktop/index.html")
                if path.startswith("/web/assets/"):
                    return self._serve_file(
                        ROOT
                        / "src/openbiliclaw/web/desktop/assets"
                        / path.removeprefix("/web/assets/")
                    )
                if path.startswith("/shared/"):
                    return self._serve_file(
                        ROOT / "src/openbiliclaw/web/shared" / path.removeprefix("/shared/")
                    )
                if path in {"/m", "/m/", "/m/index.html"}:
                    return self._serve_file(ROOT / "src/openbiliclaw/web/index.html")
                if path.startswith("/m/"):
                    return self._serve_file(
                        ROOT / "src/openbiliclaw/web" / path.removeprefix("/m/")
                    )
                status, payload = demo_payload(self.path)
                self._json(payload, status)

            def do_POST(self) -> None:  # noqa: N802
                self._mutate()

            def do_PUT(self) -> None:  # noqa: N802
                self._mutate()

            def do_DELETE(self) -> None:  # noqa: N802
                self._mutate()

            def _mutate(self) -> None:
                owner.requests.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                self._json({"ok": True, "items": _recommendations()})

            def _json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with suppress(BrokenPipeError):
                    self.wfile.write(body)

            def _serve_file(self, path: Path) -> None:
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(ROOT)
                except (FileNotFoundError, ValueError):
                    return self._json({"error": "not_found"}, 404)
                body = resolved.read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_: object) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


if __name__ == "__main__":
    with DemoServer() as origin:
        print(f"Demo server: {origin}/web/")
        threading.Event().wait()
