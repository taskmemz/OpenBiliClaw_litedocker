"""Tests for embedding cache and service helpers."""

from pathlib import Path

import pytest

from openbiliclaw.llm.embedding import (
    EmbeddingCache,
    EmbeddingService,
    build_embedding_provenance,
    image_embedding_cache_key,
    normalize_embedding_endpoint,
)
from openbiliclaw.llm.gemini_provider import GeminiProvider


class _FakeEmbedProvider:
    """Minimal ``SupportsEmbed`` double with controllable behaviour."""

    def __init__(
        self, *, vector: list[float] | None = None, error: Exception | None = None
    ) -> None:
        self._vector = [0.1, 0.2, 0.3] if vector is None else vector
        self._error = error
        self.calls: list[str] = []

    async def embed(self, text: str, *, model: str = "") -> list[float]:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return list(self._vector)


def test_embedding_endpoint_normalization_redacts_secret_url_parts() -> None:
    endpoint = "HTTPS://user:api-secret@Relay.Example:443/v1/?token=secret#fragment"

    assert normalize_embedding_endpoint(endpoint) == "https://relay.example/v1"
    assert "secret" not in normalize_embedding_endpoint(endpoint)
    assert normalize_embedding_endpoint("user:secret@relay.example/v1?token=secret") == (
        "relay.example/v1"
    )


async def test_embedding_l2_namespace_isolated_by_endpoint_and_stable(
    tmp_path: Path,
) -> None:
    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()
    common = {
        "logical_provider": "openai_compatible",
        "model": "embed-v1",
        "output_dimensionality": 3,
        "persistent_cache": cache,
    }
    first_provider = _FakeEmbedProvider(vector=[0.1, 0.2, 0.3])
    second_provider = _FakeEmbedProvider(vector=[0.7, 0.8, 0.9])
    first = EmbeddingService(
        first_provider,
        endpoint="HTTPS://user:secret@Relay.Example:443/v1/?token=secret#fragment",
        **common,
    )
    second = EmbeddingService(
        second_provider,
        endpoint="https://other.example/v1",
        **common,
    )
    same_config = EmbeddingService(
        _FakeEmbedProvider(),
        endpoint="https://relay.example/v1/",
        **common,
    )

    assert first.embedding_fingerprint != second.embedding_fingerprint
    assert first.cache_model_namespace != second.cache_model_namespace
    assert first.embedding_fingerprint == same_config.embedding_fingerprint
    assert first.cache_model_namespace == same_config.cache_model_namespace
    assert "secret" not in first.cache_model_namespace

    assert await first.embed("same text") == [0.1, 0.2, 0.3]
    assert second.lookup_cached("same text") == []
    assert await second.embed("same text") == [0.7, 0.8, 0.9]
    assert len(first_provider.calls) == 1
    assert len(second_provider.calls) == 1
    # A fresh service with the same normalized provenance can reuse the L2
    # row, which pins cross-process stability without sharing other endpoints.
    assert await same_config.embed("same text") == [0.1, 0.2, 0.3]


def test_embedding_provenance_isolated_by_provider_model_and_dimension() -> None:
    base = build_embedding_provenance("openai", "https://relay.example/v1", "embed-v1", 3)
    other_provider = build_embedding_provenance("gemini", "https://relay.example/v1", "embed-v1", 3)
    other_model = build_embedding_provenance("openai", "https://relay.example/v1", "embed-v2", 3)
    other_dimension = build_embedding_provenance(
        "openai", "https://relay.example/v1", "embed-v1", 4
    )

    assert len({base, other_provider, other_model, other_dimension}) == 4


class _FakeImageEmbedProvider(_FakeEmbedProvider):
    """Text + image embedding double for multimodal path tests."""

    supports_image_embedding = True

    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        image_vector: list[float] | None = None,
        error: Exception | None = None,
        image_error: Exception | None = None,
    ) -> None:
        super().__init__(vector=vector, error=error)
        self._image_vector = [0.9, 0.1, 0.0] if image_vector is None else image_vector
        self._image_error = image_error
        self.image_calls: list[tuple[int, str, str]] = []

    @staticmethod
    def is_multimodal_embedding_model(model: str) -> bool:
        return "embedding-2" in (model or "").lower()

    async def embed_image(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/jpeg",
        model: str = "",
    ) -> list[float]:
        self.image_calls.append((len(image_bytes), mime_type, model))
        if self._image_error is not None:
            raise self._image_error
        return list(self._image_vector)


async def test_probe_true_when_provider_returns_vector() -> None:
    provider = _FakeEmbedProvider(vector=[0.1, 0.2])
    service = EmbeddingService(provider, model="bge-m3")

    assert await service.probe() is True
    assert provider.calls  # the provider was actually hit


async def test_probe_false_when_provider_returns_empty() -> None:
    # Empty vector = transient/upstream failure (e.g. bge-m3 not pulled).
    provider = _FakeEmbedProvider(vector=[])
    service = EmbeddingService(provider, model="bge-m3")

    assert await service.probe() is False


async def test_probe_false_when_provider_raises() -> None:
    provider = _FakeEmbedProvider(error=RuntimeError("404 Not Found"))
    service = EmbeddingService(provider, model="bge-m3")

    assert await service.probe() is False


async def test_probe_bypasses_cache_and_hits_provider_each_call() -> None:
    # A cached success must never mask a provider that later goes down, so
    # probe() issues a real provider call instead of reading the cache.
    provider = _FakeEmbedProvider(vector=[0.5, 0.5])
    service = EmbeddingService(provider, model="bge-m3")

    await service.probe()
    await service.probe()

    assert len(provider.calls) == 2


def test_embedding_cache_get_rejects_non_list_payload(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()
    cache.conn.execute(
        "INSERT INTO embedding_cache (text_key, vector, model) VALUES (?, ?, ?)",
        ("bad-object", '{"oops": 1}', ""),
    )
    cache.conn.commit()

    assert cache.get("bad-object") is None


def test_embedding_cache_get_rejects_non_numeric_vectors(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()
    cache.conn.execute(
        "INSERT INTO embedding_cache (text_key, vector, model) VALUES (?, ?, ?)",
        ("bad-vector", '[1, "oops", 3]', ""),
    )
    cache.conn.commit()

    assert cache.get("bad-vector") is None


def test_embedding_cache_close_is_idempotent(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()

    cache.close()
    cache.close()

    with pytest.raises(RuntimeError, match="not initialized"):
        _ = cache.conn


def test_embedding_cache_is_thread_safe_across_threads(tmp_path: Path) -> None:
    # Regression: discovery candidate post-processing and recommendation prewarm
    # touch the cache from worker threads other than the one that opened it. A
    # bare sqlite3 connection (check_same_thread=True) raises "SQLite objects
    # created in a thread can only be used in that same thread".
    import threading

    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()  # connection opened on this (main) thread

    errors: list[Exception] = []
    results: dict[str, object] = {}

    def worker() -> None:
        try:
            cache.put("k", [0.1, 0.2, 0.3], model="bge-m3")
            results["get"] = cache.get("k")
            results["count"] = cache.count()
        except Exception as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == [], f"cache raised across threads: {errors}"
    assert results["get"] == [0.1, 0.2, 0.3]
    assert results["count"] == 1


def test_gemini_multimodal_embedding_model_detection() -> None:
    assert GeminiProvider.is_multimodal_embedding_model("gemini-embedding-2")
    assert GeminiProvider.is_multimodal_embedding_model("gemini-embedding-2-preview")
    assert not GeminiProvider.is_multimodal_embedding_model("gemini-embedding-001")
    assert not GeminiProvider.is_multimodal_embedding_model("bge-m3")
    assert not GeminiProvider.is_multimodal_embedding_model("")


async def test_embed_image_inactive_when_multimodal_disabled() -> None:
    provider = _FakeImageEmbedProvider()
    service = EmbeddingService(
        provider,
        model="gemini-embedding-2",
        multimodal_enabled=False,
    )

    assert service.supports_image_embedding is True
    assert service.image_embedding_active() is False
    assert await service.embed_image(b"fake-jpeg-bytes") == []
    assert provider.image_calls == []


async def test_embed_image_inactive_for_text_only_model() -> None:
    provider = _FakeImageEmbedProvider()
    service = EmbeddingService(
        provider,
        model="gemini-embedding-001",
        multimodal_enabled=True,
    )

    assert service.supports_image_embedding is False
    assert await service.embed_image(b"fake-jpeg-bytes") == []
    assert provider.image_calls == []


async def test_embed_image_caches_and_reuses_vector(tmp_path: Path) -> None:
    provider = _FakeImageEmbedProvider(image_vector=[0.2, 0.4, 0.6])
    cache = EmbeddingCache(tmp_path / "embedding-cache.db")
    cache.initialize()
    service = EmbeddingService(
        provider,
        model="gemini-embedding-2",
        cache_model="gemini-embedding-2#dim=1024",
        persistent_cache=cache,
        multimodal_enabled=True,
    )
    image = b"\xff\xd8\xff" + b"cover-bytes-demo"

    first = await service.embed_image(image, mime_type="image/jpeg")
    second = await service.embed_image(image, mime_type="image/jpeg")

    assert first == [0.2, 0.4, 0.6]
    assert second == first
    assert len(provider.image_calls) == 1
    key = image_embedding_cache_key(image)
    assert service.lookup_cached_image(key) == first
    assert cache.get(key, model="gemini-embedding-2#dim=1024") == first


async def test_embed_image_skips_cache_on_empty_vector() -> None:
    provider = _FakeImageEmbedProvider(image_vector=[])
    service = EmbeddingService(
        provider,
        model="gemini-embedding-2",
        multimodal_enabled=True,
    )
    image = b"empty-result"

    assert await service.embed_image(image) == []
    assert await service.embed_image(image) == []
    assert len(provider.image_calls) == 2


async def test_text_only_provider_has_no_image_support() -> None:
    provider = _FakeEmbedProvider()
    service = EmbeddingService(
        provider,
        model="bge-m3",
        multimodal_enabled=True,
    )
    assert service.supports_image_embedding is False
    assert service.image_embedding_active() is False


async def test_document_embedding_does_not_collide_on_shared_200_char_prefix() -> None:
    provider = _FakeEmbedProvider()
    service = EmbeddingService(provider, model="bge-m3")
    first = "a" * 200 + "-first-document"
    second = "a" * 200 + "-second-document"

    await service.embed_document(first)
    await service.embed_document(second)

    assert len(provider.calls) == 2
    assert service.lookup_cached_document(first)
    assert service.lookup_cached_document(second)
