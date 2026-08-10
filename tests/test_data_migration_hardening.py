from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import openbiliclaw.storage.migration as migration
from openbiliclaw.config import Config, load_config, save_config
from openbiliclaw.storage.database import Database


def _clear_openbiliclaw_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OPENBILICLAW_"):
            monkeypatch.delenv(name, raising=False)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _initialize_database(path: Path) -> None:
    database = Database(path)
    try:
        database.initialize()
    finally:
        database.close()


def _source_archive(root: Path, *, label: str = "source") -> migration.MigrationExport:
    root.mkdir(parents=True, exist_ok=True)
    config = Config(data_dir=str(root / "data"), language="en")
    config.llm.deepseek.api_key = f"{label}-portable-key"
    config.scheduler.discovery_limit = 37
    save_config(config, root / "config.toml", autostart_authoritative=True)
    _initialize_database(root / "data" / "openbiliclaw.db")
    _write_json(root / "data" / "memory" / "profile.json", {"label": label})
    return migration.create_migration_archive(config, project_root=root)


def _target_config(root: Path, *, password: str = "target-password-hash") -> Config:
    root.mkdir(parents=True, exist_ok=True)
    config = Config(data_dir=str(root / "data"), language="zh")
    config.api.host = "127.0.0.1"
    config.api.port = 19420
    config.api.auth.enabled = True
    config.api.auth.password_hash = password
    config.api.auth.session_secret = "target-old-session-secret"
    config.api.auth.extension_access_enabled = True
    config.api.auth.extension_access_keys = ["target-old-pairing"]
    config.network.mode = "custom"
    config.network.proxy = "socks5://127.0.0.1:1080"
    save_config(config, root / "config.toml", autostart_authoritative=True)
    _initialize_database(root / "data" / "openbiliclaw.db")
    _write_json(root / "data" / "memory" / "profile.json", {"label": "old-target"})
    return config


def _cleanup_export(exported: migration.MigrationExport) -> None:
    shutil.rmtree(exported.path.parent, ignore_errors=True)


def test_live_json_copy_retries_a_changed_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "profile.json"
    destination = tmp_path / "snapshot" / "profile.json"
    _write_json(source, {"generation": 1})
    real_copyfile = migration._copy_file_bounded
    calls = 0

    def racing_copy(
        source_path: Path,
        target_path: Path,
        *,
        max_bytes: int,
    ) -> None:
        nonlocal calls
        calls += 1
        real_copyfile(source_path, target_path, max_bytes=max_bytes)
        if calls == 1:
            _write_json(source, {"generation": 2, "complete": True})

    monkeypatch.setattr(migration, "_copy_file_bounded", racing_copy)

    migration._copy_stable_private_file(source, destination)

    assert calls >= 2
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "generation": 2,
        "complete": True,
    }


def test_live_json_copy_rejects_stably_invalid_json(tmp_path: Path) -> None:
    source = tmp_path / "profile.json"
    source.write_text('{"truncated":', encoding="utf-8")

    with pytest.raises(migration.MigrationError) as error:
        migration._copy_stable_private_file(source, tmp_path / "snapshot.json")

    assert error.value.code == "invalid_source_json"


def test_sqlite_validation_encodes_uri_metacharacters_without_creating_alias(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    invalid_database = data_root / "foo?.db"
    invalid_database.write_bytes(b"not-sqlite")

    with pytest.raises(migration.MigrationError) as error:
        migration._validate_imported_databases(data_root)

    assert error.value.code == "sqlite_integrity_failed"
    assert not (data_root / "foo").exists()


@pytest.mark.parametrize("name", ["data/foo?.db", "data/foo*.db", 'data/foo".db'])
def test_archive_paths_reject_windows_invalid_characters(name: str) -> None:
    with pytest.raises(migration.MigrationError) as error:
        migration._validate_safe_relative_name(name)
    assert error.value.code == "unsafe_path"


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create these source names")
@pytest.mark.parametrize("name", ["bad:name.json", r"bad\name.json"])
def test_export_rejects_source_names_that_the_importer_cannot_restore(
    tmp_path: Path,
    name: str,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    config = Config(data_dir=str(root / "data"))
    save_config(config, root / "config.toml", autostart_authoritative=True)
    _initialize_database(root / "data" / "openbiliclaw.db")
    _write_json(root / "data" / name, {"secret": True})

    with pytest.raises(migration.MigrationError) as error:
        migration.create_migration_archive(config, project_root=root)

    assert error.value.code == "unsupported_source_path"


def test_apply_rejects_a_modified_pending_tree_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    try:
        migration.stage_migration_archive(exported.path, target, project_root=target_root)
        marker = json.loads(
            (target_root / ".openbiliclaw-migration" / "pending.json").read_text(encoding="utf-8")
        )
        staged_profile = (
            target_root
            / ".openbiliclaw-migration"
            / marker["stage_dir"]
            / "data"
            / "memory"
            / "profile.json"
        )
        _write_json(staged_profile, {"label": "tampered-after-validation"})

        result = migration.apply_pending_migration(project_root=target_root)

        assert result is not None and result.state == "failed"
        assert json.loads(
            (target_root / "data" / "memory" / "profile.json").read_text(encoding="utf-8")
        ) == {"label": "old-target"}
        assert not (target_root / ".openbiliclaw-migration" / "pending.json").exists()
        assert not (target_root / ".openbiliclaw-migration" / marker["stage_dir"]).exists()
    finally:
        _cleanup_export(exported)


def test_prepare_copy_failure_cleans_partial_sensitive_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    try:
        staged = migration.stage_migration_archive(
            exported.path,
            target,
            project_root=target_root,
        )
        real_copytree = shutil.copytree

        def fail_prepared_copy(
            source: str | os.PathLike[str],
            destination: str | os.PathLike[str],
            *args: object,
            **kwargs: object,
        ) -> str:
            destination_path = Path(destination)
            if destination_path.name.startswith(".data.import-"):
                _write_json(destination_path / "partial-secret.json", {"secret": True})
                raise OSError("injected disk-full failure")
            return str(real_copytree(source, destination, *args, **kwargs))

        monkeypatch.setattr(migration.shutil, "copytree", fail_prepared_copy)

        result = migration.apply_pending_migration(project_root=target_root)

        assert result is not None and result.state == "failed"
        assert json.loads(
            (target_root / "data" / "memory" / "profile.json").read_text(encoding="utf-8")
        ) == {"label": "old-target"}
        assert not list(target_root.glob(".data.import-*"))
        assert not list(target_root.glob(".config.toml.import-*"))
        migration_root = target_root / ".openbiliclaw-migration"
        assert not (migration_root / "pending.json").exists()
        assert not (migration_root / f"pending-{staged.migration_id}").exists()
        assert not (migration_root / "apply-journal.json").exists()
    finally:
        _cleanup_export(exported)


def test_stage_marker_failure_removes_renamed_sensitive_pending_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    real_write = migration._atomic_write_json

    def fail_marker(path: Path, payload: object) -> None:
        if path.name == "pending.json":
            raise OSError("marker write failed")
        real_write(path, payload)

    monkeypatch.setattr(migration, "_atomic_write_json", fail_marker)
    try:
        with pytest.raises(migration.MigrationError):
            migration.stage_migration_archive(exported.path, target, project_root=target_root)

        migration_root = target_root / ".openbiliclaw-migration"
        assert not (migration_root / "pending.json").exists()
        assert not list(migration_root.glob("pending-*"))
        assert not list(migration_root.glob("incoming-*"))
    finally:
        _cleanup_export(exported)


def test_startup_reconciles_unreferenced_strictly_named_stage_trees(tmp_path: Path) -> None:
    root = tmp_path / "target"
    migration_root = root / ".openbiliclaw-migration"
    orphan_pending = migration_root / f"pending-{'a' * 32}"
    orphan_incoming = migration_root / f"incoming-{'b' * 32}"
    _write_json(orphan_pending / "secret.json", {"cookie": "pending-secret"})
    _write_json(orphan_incoming / "secret.json", {"cookie": "incoming-secret"})

    assert migration.apply_pending_migration(project_root=root) is None
    assert not orphan_pending.exists()
    assert not orphan_incoming.exists()


def test_commit_cleanup_fault_reports_applied_instead_of_false_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    try:
        migration.stage_migration_archive(exported.path, target, project_root=target_root)
        marker_path = target_root / ".openbiliclaw-migration" / "pending.json"
        real_unlink = Path.unlink
        injected = False

        def fail_first_committed_marker_cleanup(
            path: Path,
            missing_ok: bool = False,
        ) -> None:
            nonlocal injected
            if path == marker_path and not injected:
                journal = json.loads(
                    (path.parent / "apply-journal.json").read_text(encoding="utf-8")
                )
                if journal.get("state") == "committed":
                    injected = True
                    raise OSError("injected committed cleanup failure")
            real_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_first_committed_marker_cleanup)

        result = migration.apply_pending_migration(project_root=target_root)

        assert injected is True
        assert result is not None and result.state == "applied"
        assert json.loads(
            (target_root / "data" / "memory" / "profile.json").read_text(encoding="utf-8")
        ) == {"label": "source"}
        assert migration.migration_status(project_root=target_root)["state"] == "applied"
        assert not marker_path.exists()
        assert not (marker_path.parent / "apply-journal.json").exists()
    finally:
        _cleanup_export(exported)


def test_apply_remerges_latest_destination_security_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root, password="password-before-stage")
    with sqlite3.connect(target_root / "data" / "openbiliclaw.db") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO auth_state (key, value) VALUES ('auth_epoch', '41')"
        )
    try:
        migration.stage_migration_archive(exported.path, target, project_root=target_root)
        latest = _target_config(target_root, password="password-changed-after-stage")
        latest.api.host = "0.0.0.0"
        latest.api.port = 28420
        latest.api.auth.trust_loopback = False
        latest.api.auth.session_secret = "session-changed-after-stage"
        latest.network.proxy = "socks5://127.0.0.1:2080"
        save_config(latest, target_root / "config.toml", autostart_authoritative=True)

        result = migration.apply_pending_migration(project_root=target_root)
        applied = load_config(target_root / "config.toml")

        assert result is not None and result.state == "applied"
        assert applied.language == "en"
        assert applied.scheduler.discovery_limit == 37
        assert applied.api.host == "0.0.0.0"
        assert applied.api.port == 28420
        assert applied.api.auth.password_hash == "password-changed-after-stage"
        assert applied.api.auth.trust_loopback is False
        assert applied.api.auth.session_secret not in {
            "target-old-session-secret",
            "session-changed-after-stage",
        }
        assert applied.api.auth.extension_access_enabled is False
        assert applied.api.auth.extension_access_keys == []
        assert applied.network.proxy == "socks5://127.0.0.1:2080"
        with sqlite3.connect(target_root / "data" / "openbiliclaw.db") as connection:
            assert connection.execute(
                "SELECT value FROM auth_state WHERE key = 'auth_epoch'"
            ).fetchone() == ("42",)
    finally:
        _cleanup_export(exported)


def test_runtime_guard_fences_shared_data_across_project_roots(tmp_path: Path) -> None:
    shared_data = tmp_path / "shared" / "user-data"
    first = migration.acquire_migration_runtime_guard(tmp_path / "one", shared_data)
    assert first is not None
    try:
        assert migration.acquire_migration_runtime_guard(tmp_path / "two", shared_data) is None
    finally:
        first.release()

    second = migration.acquire_migration_runtime_guard(tmp_path / "two", shared_data)
    assert second is not None
    second.release()


def test_runtime_guard_cannot_extend_to_a_data_dir_owned_by_another_backend(
    tmp_path: Path,
) -> None:
    recovery_data = tmp_path / "recovery-data"
    runtime_data = tmp_path / "runtime-data"
    recovery_guard = migration.acquire_migration_runtime_guard(
        tmp_path / "recovering-project",
        recovery_data,
    )
    runtime_owner = migration.acquire_migration_runtime_guard(
        tmp_path / "other-project",
        runtime_data,
    )
    assert recovery_guard is not None
    assert runtime_owner is not None
    try:
        assert recovery_guard.acquire_data_dir(runtime_data) is False
        assert recovery_guard.acquire_data_dir(recovery_data) is True
    finally:
        recovery_guard.release()
        runtime_owner.release()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs elevated privileges")
def test_runtime_guard_rejects_symlink_lock_without_touching_target(tmp_path: Path) -> None:
    shared_data = tmp_path / "shared" / "user-data"
    shared_data.parent.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch", encoding="utf-8")
    before_mode = victim.stat().st_mode
    lock_path = shared_data.parent / f".{shared_data.name}.openbiliclaw-runtime.lock"
    lock_path.symlink_to(victim)

    assert migration.acquire_migration_runtime_guard(tmp_path / "project", shared_data) is None
    assert victim.read_text(encoding="utf-8") == "do-not-touch"
    assert victim.stat().st_mode == before_mode


def test_success_keeps_only_latest_rollback_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    exports: list[migration.MigrationExport] = []
    try:
        for index in (1, 2):
            exported = _source_archive(tmp_path / f"source-{index}", label=f"source-{index}")
            exports.append(exported)
            migration.stage_migration_archive(exported.path, target, project_root=target_root)
            result = migration.apply_pending_migration(project_root=target_root)
            assert result is not None and result.state == "applied"
            target = load_config(target_root / "config.toml")

        assert len(list(target_root.glob("config.toml.pre-import-*.bak"))) == 1
        assert len(list(target_root.glob("data.pre-import-*.bak"))) == 1
        assert not list(target_root.glob("*.failed-import-*"))
        assert not list(target_root.glob(".*.import-*"))
        with sqlite3.connect(target_root / "data" / "openbiliclaw.db") as connection:
            assert (
                int(
                    connection.execute(
                        "SELECT value FROM auth_state WHERE key = 'auth_epoch'"
                    ).fetchone()[0]
                )
                == 2
            )
    finally:
        for exported in exports:
            _cleanup_export(exported)


def test_rollback_recovery_is_idempotent_after_old_targets_are_restored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    target = _target_config(root)
    migration_id = "d" * 32
    token = migration_id[:12]
    migration_root = root / ".openbiliclaw-migration"
    failed_config = root / f"config.toml.failed-import-{token}"
    failed_data = root / f"data.failed-import-{token}"
    failed_config.write_text("imported config", encoding="utf-8")
    _write_json(failed_data / "memory" / "profile.json", {"label": "imported"})

    journal = {
        "state": "rolling_back",
        "migration_id": migration_id,
        "target_config": str((root / "config.toml").resolve()),
        "target_local": str((root / "config.local.toml").resolve()),
        "target_data": str(target.data_path.resolve()),
        "prepared_config": str((root / f".config.toml.import-{token}").resolve()),
        "prepared_data": str((root / f".data.import-{token}").resolve()),
        "config_backup": str((root / f"config.toml.pre-import-{token}.bak").resolve()),
        "local_backup": str((root / f"config.local.toml.pre-import-{token}.bak").resolve()),
        "data_backup": str((root / f"data.pre-import-{token}.bak").resolve()),
        "target_config_existed": True,
        "target_local_existed": False,
        "target_data_existed": True,
        "rollback_new_config_active": True,
        "rollback_new_data_active": True,
        "rollback_config_quarantined": True,
        "rollback_data_quarantined": True,
        # Simulate a crash after backup -> target but before these journal
        # acknowledgements were persisted.
        "rollback_config_restored": False,
        "rollback_local_restored": False,
        "rollback_data_restored": False,
    }
    _write_json(migration_root / "apply-journal.json", journal)

    assert migration.apply_pending_migration(project_root=root) is None

    assert load_config(root / "config.toml").api.auth.password_hash == "target-password-hash"
    assert json.loads(
        (target.data_path / "memory" / "profile.json").read_text(encoding="utf-8")
    ) == {"label": "old-target"}
    assert not failed_config.exists()
    assert not failed_data.exists()
    assert not (migration_root / "apply-journal.json").exists()


def test_pruning_never_deletes_an_active_data_dir_named_like_a_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    target.data_dir = str(target_root / "config.toml.pre-import-cccccccccccc.bak")
    save_config(target, target_root / "config.toml", autostart_authoritative=True)
    _initialize_database(target.data_path / "openbiliclaw.db")
    _write_json(target.data_path / "memory" / "profile.json", {"label": "old-special"})
    try:
        migration.stage_migration_archive(exported.path, target, project_root=target_root)
        result = migration.apply_pending_migration(project_root=target_root)

        assert result is not None and result.state == "applied"
        assert target.data_path.is_dir()
        assert json.loads(
            (target.data_path / "memory" / "profile.json").read_text(encoding="utf-8")
        ) == {"label": "source"}
    finally:
        _cleanup_export(exported)


def test_apply_rejects_data_dir_that_aliases_a_transaction_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    target_root = tmp_path / "target"
    target = _target_config(target_root)
    try:
        staged = migration.stage_migration_archive(
            exported.path,
            target,
            project_root=target_root,
        )
        token = staged.migration_id[:12]
        collision = target_root / f"config.toml.failed-import-{token}"
        _initialize_database(collision / "openbiliclaw.db")
        _write_json(collision / "memory" / "profile.json", {"label": "must-survive"})
        target.data_dir = str(collision)
        save_config(target, target_root / "config.toml", autostart_authoritative=True)

        result = migration.apply_pending_migration(project_root=target_root)

        assert result is not None and result.state == "failed"
        assert json.loads((collision / "memory" / "profile.json").read_text(encoding="utf-8")) == {
            "label": "must-survive"
        }
    finally:
        _cleanup_export(exported)


def test_apply_never_overwrites_a_preexisting_transaction_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    root = tmp_path / "target"
    target = _target_config(root)
    try:
        staged = migration.stage_migration_archive(exported.path, target, project_root=root)
        collision = root / f"config.toml.pre-import-{staged.migration_id[:12]}.bak"
        collision.write_text("must-not-overwrite", encoding="utf-8")

        result = migration.apply_pending_migration(project_root=root)

        assert result is not None and result.state == "failed"
        assert "事务文件名已被占用" in result.message
        assert collision.read_text(encoding="utf-8") == "must-not-overwrite"
    finally:
        _cleanup_export(exported)


def test_recovery_refuses_to_touch_a_data_dir_without_its_runtime_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    target = _target_config(root)
    migration_id = "e" * 32
    token = migration_id[:12]
    migration_root = root / ".openbiliclaw-migration"
    prepared_config = root / f".config.toml.import-{token}"
    prepared_data = root / f".data.import-{token}"
    prepared_config.write_text("sensitive prepared config", encoding="utf-8")
    _write_json(prepared_data / "memory" / "profile.json", {"secret": True})
    _write_json(
        migration_root / "apply-journal.json",
        {
            "state": "preparing",
            "migration_id": migration_id,
            "target_config": str((root / "config.toml").resolve()),
            "target_local": str((root / "config.local.toml").resolve()),
            "target_data": str(target.data_path.resolve()),
            "prepared_config": str(prepared_config.resolve()),
            "prepared_data": str(prepared_data.resolve()),
            "config_backup": str((root / f"config.toml.pre-import-{token}.bak").resolve()),
            "local_backup": str((root / f"config.local.toml.pre-import-{token}.bak").resolve()),
            "data_backup": str((root / f"data.pre-import-{token}.bak").resolve()),
        },
    )

    with pytest.raises(migration.MigrationError) as error:
        migration.apply_pending_migration(
            project_root=root,
            locked_data_dir=root / "different-data",
        )

    assert error.value.code == "migration_lock_mismatch"
    assert prepared_config.exists()
    assert prepared_data.exists()
    assert (migration_root / "apply-journal.json").exists()


def test_committed_recovery_rejects_an_old_healthy_generation(tmp_path: Path) -> None:
    root = tmp_path / "target"
    target = _target_config(root)
    migration_id = "1" * 32
    token = migration_id[:12]
    migration_root = root / ".openbiliclaw-migration"
    _write_json(
        migration_root / "apply-journal.json",
        {
            "state": "committed",
            "migration_id": migration_id,
            "target_config": str((root / "config.toml").resolve()),
            "target_local": str((root / "config.local.toml").resolve()),
            "target_data": str(target.data_path.resolve()),
            "prepared_config": str((root / f".config.toml.import-{token}").resolve()),
            "prepared_data": str((root / f".data.import-{token}").resolve()),
            "config_backup": str((root / f"config.toml.pre-import-{token}.bak").resolve()),
            "local_backup": str((root / f"config.local.toml.pre-import-{token}.bak").resolve()),
            "data_backup": str((root / f"data.pre-import-{token}.bak").resolve()),
            # Both active targets are healthy, but neither carries the committed
            # imported generation promised by this journal.
            "active_config_sha256": "0" * 64,
            "active_auth_epoch": 1,
        },
    )

    with pytest.raises(migration.MigrationError) as error:
        migration.apply_pending_migration(project_root=root)

    assert error.value.code == "apply_generation_mismatch"
    assert json.loads(
        (target.data_path / "memory" / "profile.json").read_text(encoding="utf-8")
    ) == {"label": "old-target"}
    assert (migration_root / "apply-journal.json").exists()


def test_resurrected_marker_for_an_applied_generation_is_cleaned_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    root = tmp_path / "target"
    target = _target_config(root)
    try:
        staged = migration.stage_migration_archive(exported.path, target, project_root=root)
        first = migration.apply_pending_migration(project_root=root)
        assert first is not None and first.state == "applied"
        config_backup = Path(first.config_backup)
        data_backup = Path(first.data_backup)
        original_config_backup = config_backup.read_bytes()
        original_profile_backup = (data_backup / "memory" / "profile.json").read_bytes()

        migration_root = root / ".openbiliclaw-migration"
        resurrected_stage = migration_root / f"pending-{staged.migration_id}"
        _write_json(resurrected_stage / "secret.json", {"secret": "stale-stage"})
        _write_json(
            migration_root / "pending.json",
            {
                "migration_id": staged.migration_id,
                "source_version": staged.source_version,
                "stage_dir": resurrected_stage.name,
            },
        )

        assert migration.migration_recovery_data_dir(project_root=root) == target.data_path
        repeated = migration.apply_pending_migration(
            project_root=root,
            locked_data_dir=target.data_path,
        )

        assert repeated is not None and repeated.state == "applied"
        assert config_backup.read_bytes() == original_config_backup
        assert (data_backup / "memory" / "profile.json").read_bytes() == original_profile_backup
        assert not resurrected_stage.exists()
        assert not (migration_root / "pending.json").exists()
    finally:
        _cleanup_export(exported)


def test_crash_after_applied_receipt_before_commit_can_retry_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    root = tmp_path / "target"
    target = _target_config(root)
    real_write = migration._atomic_write_json

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_committed_journal(path: Path, payload: object) -> None:
        if (
            path.name == "apply-journal.json"
            and isinstance(payload, dict)
            and payload.get("state") == "committed"
        ):
            raise SimulatedPowerLoss
        real_write(path, payload)

    try:
        migration.stage_migration_archive(exported.path, target, project_root=root)
        monkeypatch.setattr(migration, "_atomic_write_json", crash_before_committed_journal)
        with pytest.raises(SimulatedPowerLoss):
            migration.apply_pending_migration(project_root=root)

        migration_root = root / ".openbiliclaw-migration"
        assert (
            json.loads((migration_root / "status.json").read_text(encoding="utf-8"))["state"]
            == "applied"
        )
        assert (
            json.loads((migration_root / "apply-journal.json").read_text(encoding="utf-8"))["state"]
            == "applying"
        )

        monkeypatch.setattr(migration, "_atomic_write_json", real_write)
        recovered = migration.apply_pending_migration(project_root=root)

        assert recovered is not None and recovered.state == "applied"
        assert not (migration_root / "apply-journal.json").exists()
        assert not (migration_root / "pending.json").exists()
    finally:
        _cleanup_export(exported)


def test_committed_generation_rejects_a_resurrected_local_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_openbiliclaw_environment(monkeypatch)
    exported = _source_archive(tmp_path / "source")
    root = tmp_path / "target"
    target = _target_config(root)
    try:
        staged = migration.stage_migration_archive(exported.path, target, project_root=root)
        result = migration.apply_pending_migration(project_root=root)
        assert result is not None and result.state == "applied"
        (root / "config.local.toml").write_text(
            '[general]\nlanguage = "zh"\n',
            encoding="utf-8",
        )
        migration_root = root / ".openbiliclaw-migration"
        stage_dir = migration_root / f"pending-{staged.migration_id}"
        stage_dir.mkdir()
        _write_json(
            migration_root / "pending.json",
            {
                "migration_id": staged.migration_id,
                "source_version": staged.source_version,
                "stage_dir": stage_dir.name,
            },
        )

        with pytest.raises(migration.MigrationError) as error:
            migration.apply_pending_migration(
                project_root=root,
                locked_data_dir=target.data_path,
            )

        assert error.value.code == "apply_generation_mismatch"
        assert stage_dir.exists()
        assert (migration_root / "pending.json").exists()
    finally:
        _cleanup_export(exported)


def test_large_live_file_is_rejected_before_copying_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"0123456789")
    copied = False

    def unexpected_copy(_source: Path, _destination: Path, *, max_bytes: int) -> None:
        nonlocal copied
        copied = True

    monkeypatch.setattr(migration, "_copy_file_bounded", unexpected_copy)

    with pytest.raises(migration.MigrationError) as error:
        migration._copy_stable_private_file(
            source,
            tmp_path / "snapshot.bin",
            max_bytes=5,
        )

    assert error.value.code == "entry_too_large"
    assert copied is False
