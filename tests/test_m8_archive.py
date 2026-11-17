"""M8 灾备归档：整包校验、来源身份、冲突隔离、幂等重放与顺序唯一。"""

from __future__ import annotations

import copy
import json
import threading

import pytest

from app import errors, manifest, store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_archive import canonical, package, restore, service

SOURCE_B = "dr-site-b"


def _reseal(doc: dict) -> dict:
    """改完内容重新签一遍：用来构造「格式合法但内容不同」的包。"""
    doc["identity"] = package.fingerprint(doc["batch"], doc["result"], doc["snapshots"],
                                          doc.get("refs"))
    doc["integrity"] = {
        "algorithm": "sha256",
        "canonical": "json-sort-keys-utf8",
        "payload_sha256": canonical.digest({k: v for k, v in doc.items() if k != "integrity"}),
        "sections": {name: canonical.digest(doc[name])
                     for name in sorted(package.ALLOWED_SECTIONS) if name in doc},
        "cross_checks": package._cross_checks(doc),
    }
    return doc


def _package_bytes() -> bytes:
    return service.export_bytes()


def test_archive_carries_structure_refs_and_impact(client):
    doc = service.export()
    assert doc["format"] == package.FORMAT
    assert doc["kind"] == "reference"
    assert set(doc["snapshots"]) == {"base", "old", "new"}
    assert doc["snapshots"]["old"]["fields"]
    assert doc["refs"]["consumers"]
    assert doc["result"]["impact"]["summary"]["consumers"] == 7
    assert doc["result"]["diff"]
    assert doc["integrity"]["algorithm"] == "sha256"
    assert all(item["ok"] for item in doc["integrity"]["cross_checks"].values())


def test_archive_digest_is_recomputable(client):
    body = _package_bytes()
    doc = json.loads(body)
    assert canonical.digest({k: v for k, v in doc.items() if k != "integrity"}) \
        == doc["integrity"]["payload_sha256"]
    # 重排键、改空白不改变结论：摘要算在规范化字节上
    shuffled = json.loads(json.dumps(doc, sort_keys=False))
    assert package.validate_package(shuffled)["integrity"]["payload_sha256"] \
        == doc["integrity"]["payload_sha256"]


def test_export_is_byte_stable(client):
    assert _package_bytes() == _package_bytes()
    run = store.latest_run()
    assert service.export_bytes(run["run_id"]) == _package_bytes()


@pytest.mark.parametrize("field", ["refs", "snapshots", "result", "batch"])
def test_missing_section_rejected_whole_package(client, field):
    doc = service.export()
    del doc[field]
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.validate_package(doc)
    assert caught.value.code == "ARCHIVE_REJECTED"
    assert store.count_runs() == 1


def test_tampered_section_rejected(client):
    doc = service.export()
    doc["result"]["counts"]["breaking"] = 999
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.validate_package(doc)
    assert caught.value.detail["reason"] == "DIGEST_MISMATCH"


def test_tampered_snapshot_rejected_even_if_resealed(client):
    doc = service.export()
    doc["snapshots"]["old"]["fields"][0]["name"] = "forged"
    _reseal(doc)
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.validate_package(doc)
    assert caught.value.detail["reason"] in ("CROSS_CHECK_FAILED", "IDENTITY_MISMATCH")


def test_forged_snapshot_identity_rejected(client):
    doc = service.export()
    doc["snapshots"]["old"]["schema_id"] = "sch-000000000000"
    doc["batch"]["old_schema_id"] = "sch-000000000000"
    _reseal(doc)
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.validate_package(doc)
    assert caught.value.detail["reason"] == "SNAPSHOT_ID_MISMATCH"


def test_missing_declared_base_snapshot_rejected(client):
    doc = service.export()
    del doc["snapshots"]["base"]
    _reseal(doc)
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.validate_package(doc)
    assert caught.value.detail["reason"] == "SNAPSHOT_MISSING"


def test_broken_package_writes_nothing(client):
    before = store.count_runs()
    with pytest.raises(errors.ArchiveRejected):
        restore.restore_bytes(b'{"format":"lineageguard-batch-archive"}')
    assert store.count_runs() == before
    assert store.list_pending_imports() == []
    assert store.count_imports() == {}


def test_oversized_package_rejected(client):
    with pytest.raises(errors.ArchiveRejected) as caught:
        package.load_package(b"x" * 64, max_bytes=16)
    assert caught.value.detail["reason"] == "ARCHIVE_TOO_LARGE"


def test_restore_keeps_source_identity_and_assigns_local_order(client):
    data = _package_bytes()
    local = store.latest_run()
    result = restore.restore_bytes(data)
    assert result["status"] == "applied"
    restored = store.run_by_id(result["run_id"])

    # 来源身份与原版本号原样保留
    assert restored["source_id"] == json.loads(data)["source"]["source_id"]
    assert restored["source_version_seq"] == local["version_seq"] == 1
    assert restored["version_seq"] == local["version_seq"]
    # 另分配本地顺序，且不与本地批次同号
    assert restored["local_seq"] > local["local_seq"]
    assert restored["run_id"] != local["run_id"]
    # 判定结果逐字节一致
    assert canonical.digest(restored["diff"]) == canonical.digest(local["diff"])
    assert restored["ledger"] == local["ledger"]


def test_restore_does_not_displace_current_batch(client):
    local = store.latest_run()
    restore.restore_bytes(_package_bytes())
    assert store.latest_run()["run_id"] == local["run_id"]
    assert store.current_version_seq() == local["version_seq"]
    assert store.count_local_runs() == 1
    assert store.count_imported_runs() == 1


def test_history_readback_marks_origin(client):
    result = restore.restore_bytes(_package_bytes())
    history = restore.history_view()
    assert history["current"]["origin"] == "local"
    imported = history["imported"]
    assert len(imported) == 1
    assert imported[0]["run_id"] == result["run_id"]
    assert imported[0]["source_id"] == json.loads(_package_bytes())["source"]["source_id"]
    assert imported[0]["origin"] == "imported"
    listing = store.list_runs()
    by_id = {item["run_id"]: item for item in listing}
    assert by_id[result["run_id"]]["origin"] == "imported"
    assert by_id[history["current"]["run_id"]]["origin"] == "local"


def test_reexport_of_restored_batch_is_identical(client):
    data = _package_bytes()
    result = restore.restore_bytes(data)
    assert service.export_bytes(result["run_id"]) == data


def test_same_source_same_version_identical_content_is_duplicate(client):
    data = _package_bytes()
    first = restore.restore_bytes(data)
    second = restore.restore_bytes(data)
    assert second["status"] == "duplicate"
    assert second["reused"] is True
    assert second["run_id"] == first["run_id"]
    assert store.count_runs() == 2
    assert store.count_conflicts() == 0


def test_same_source_same_version_different_content_isolated(client):
    data = _package_bytes()
    first = restore.restore_bytes(data)
    doc = json.loads(data)
    doc["batch"]["ruleset"] = "fm-forged"
    _reseal(doc)

    with pytest.raises(errors.ArchiveConflict) as caught:
        restore.restore_bytes(canonical.encode(doc))
    assert caught.value.detail["reason"] == "ARCHIVE_CONFLICT"
    conflicts = store.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["existing_run_id"] == first["run_id"]
    # 原批次不动，也没有多出第二个批次
    assert store.count_runs() == 2
    assert store.imported_run(doc["source"]["source_id"],
                              doc["source"]["source_version_seq"])["run_id"] == first["run_id"]


def test_conflict_does_not_overwrite_existing(client):
    data = _package_bytes()
    first = restore.restore_bytes(data)
    before = store.run_by_id(first["run_id"])
    doc = json.loads(data)
    doc["refs"]["registered_at"] = "2027-01-01T00:00:00Z"
    _reseal(doc)
    with pytest.raises(errors.ArchiveConflict):
        restore.restore_bytes(canonical.encode(doc))
    after = store.run_by_id(first["run_id"])
    assert canonical.digest(before) == canonical.digest(after)


def test_interrupted_import_invisible_and_replayable(client, monkeypatch):
    data = _package_bytes()
    original = restore._failpoint

    def boom(name: str) -> None:
        if name == "after_prepare":
            raise RuntimeError("power cut")

    monkeypatch.setattr(restore, "_failpoint", boom)
    with pytest.raises(RuntimeError):
        restore.restore_bytes(data)
    monkeypatch.setattr(restore, "_failpoint", original)

    # 半包不可见：批次没进历史、没顶掉当前批次，只有一条 prepared 记录
    assert store.count_imported_runs() == 0
    assert store.count_runs() == 1
    pending = store.list_pending_imports()
    assert len(pending) == 1 and pending[0]["status"] == "prepared"
    assert all(item["run_id"] != pending[0]["import_id"] for item in store.list_runs())

    results = restore.replay_pending()
    assert results[0]["status"] == "applied"
    assert store.count_imported_runs() == 1
    assert store.list_pending_imports() == []


def test_replay_reuses_preallocated_local_seq(client, monkeypatch):
    data = _package_bytes()
    original = restore._failpoint

    def boom(name: str) -> None:
        if name == "after_prepare":
            raise RuntimeError("power cut")

    monkeypatch.setattr(restore, "_failpoint", boom)
    with pytest.raises(RuntimeError):
        restore.restore_bytes(data)
    monkeypatch.setattr(restore, "_failpoint", original)
    prepared_seq = store.list_pending_imports()[0]["local_seq"]

    result = restore.replay_pending()[0]
    assert result["status"] == "applied"
    assert result["local_seq"] == prepared_seq
    sequences = [item["local_seq"] for item in store.list_runs()]
    assert len(sequences) == len(set(sequences))


def test_retry_after_interrupt_does_not_duplicate(client, monkeypatch):
    data = _package_bytes()
    original = restore._failpoint

    def boom(name: str) -> None:
        if name == "after_prepare":
            raise RuntimeError("power cut")

    monkeypatch.setattr(restore, "_failpoint", boom)
    with pytest.raises(RuntimeError):
        restore.restore_bytes(data)
    monkeypatch.setattr(restore, "_failpoint", original)

    first = restore.restore_bytes(data)
    second = restore.restore_bytes(data)
    assert first["status"] == "applied"
    assert second["status"] == "duplicate"
    assert store.count_imported_runs() == 1
    assert store.count_runs() == 2


def test_concurrent_restores_keep_single_order(client):
    data = _package_bytes()
    results: list[dict] = []
    errors_seen: list[str] = []

    def worker() -> None:
        try:
            results.append(restore.restore_bytes(data))
        except Exception as exc:  # noqa: BLE001 - 测试里要看到任何异常
            errors_seen.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors_seen == []
    assert sum(1 for item in results if item["status"] == "applied") == 1
    assert store.count_imported_runs() == 1
    sequences = [item["local_seq"] for item in store.list_runs()]
    assert len(sequences) == len(set(sequences))


def test_concurrent_restore_and_run_share_one_sequence(client):
    data = _package_bytes()
    batch_service.execute(params={"wave": "second"})
    errors_seen: list[str] = []

    def run_batch() -> None:
        try:
            batch_service.execute(params={"wave": "concurrent"})
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(repr(exc))

    def run_restore() -> None:
        try:
            restore.restore_bytes(data)
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(repr(exc))

    threads = [threading.Thread(target=run_batch), threading.Thread(target=run_restore)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors_seen == []
    sequences = [item["local_seq"] for item in store.list_runs()]
    assert len(sequences) == len(set(sequences))
    versions = [item["version_seq"] for item in store.list_runs() if item["origin"] == "local"]
    assert len(versions) == len(set(versions))
    assert max(versions) == store.current_version_seq()


def test_import_survives_restart(client, monkeypatch):
    from app.modules.m8_archive import restore as restore_mod

    data = _package_bytes()
    original = restore_mod._failpoint

    def boom(name: str) -> None:
        if name == "after_prepare":
            raise RuntimeError("power cut")

    monkeypatch.setattr(restore_mod, "_failpoint", boom)
    with pytest.raises(RuntimeError):
        restore_mod.restore_bytes(data)
    monkeypatch.setattr(restore_mod, "_failpoint", original)

    # 模拟重启：启动钩子里重放未完成导入
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as restarted:
        history = restarted.get("/api/archives").json()
        assert history["pending"] == []
        assert len(history["imported"]) == 1


def test_http_export_and_restore(client):
    response = client.get("/api/export/archive.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert len(response.headers["X-Archive-Sha256"]) == 64
    restored = client.post("/api/archives/restore", content=response.content,
                           headers={"Content-Type": "application/json"})
    assert restored.status_code == 200
    body = restored.json()
    assert body["status"] == "applied"
    assert body["source"]["source_version_seq"] == 1

    history = client.get("/api/archives").json()
    assert history["current"]["origin"] == "local"
    assert len(history["imported"]) == 1
    assert history["conflicts"] == []


def test_http_broken_package_is_422(client):
    response = client.post("/api/archives/restore", content=b'{"format":"nope"}')
    assert response.status_code == 422
    assert response.json()["error"] == "ARCHIVE_REJECTED"


def test_http_conflict_is_409(client):
    data = client.get("/api/export/archive.json").content
    assert client.post("/api/archives/restore", content=data).status_code == 200
    doc = json.loads(data)
    doc["batch"]["ruleset"] = "fm-forged"
    _reseal(doc)
    response = client.post("/api/archives/restore", content=canonical.encode(doc))
    assert response.status_code == 409
    assert response.json()["error"] == "ARCHIVE_CONFLICT"


def test_old_database_upgrades_in_place(tmp_path, monkeypatch):
    """老库升级不能丢批次：补列、回填 origin/local_seq、序号接着走。"""
    import sqlite3

    path = tmp_path / "old.sqlite"
    monkeypatch.setenv("LINEAGEGUARD_DB", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE snapshots (schema_id TEXT PRIMARY KEY, version_label TEXT NOT NULL,
               source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, encoding TEXT NOT NULL,
               bom INTEGER NOT NULL, field_count INTEGER NOT NULL, blocked_count INTEGER NOT NULL,
               created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
           CREATE TABLE runs (run_id TEXT PRIMARY KEY, batch_key TEXT NOT NULL UNIQUE,
               old_schema_id TEXT NOT NULL, new_schema_id TEXT NOT NULL, version_seq INTEGER NOT NULL,
               input_sha256 TEXT NOT NULL, ruleset TEXT NOT NULL, created_at TEXT NOT NULL,
               effective_from TEXT NOT NULL, effective_to TEXT, summary_json TEXT NOT NULL,
               payload_json TEXT NOT NULL);
           CREATE TABLE quarantine (schema_id TEXT NOT NULL, line INTEGER, table_name TEXT,
               column_name TEXT, reason TEXT NOT NULL, blocking INTEGER NOT NULL, detail TEXT, raw TEXT);
           CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);"""
    )
    conn.execute(
        "INSERT INTO runs VALUES ('old-run','old-key','a','b',7,'ish','rs',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,'{}',"
        "'{\"run_id\":\"old-run\",\"version_seq\":7}')"
    )
    conn.commit()
    conn.close()

    from app import store as store_mod

    store_mod.init_db()
    migrated = store_mod.latest_run()
    assert migrated["run_id"] == "old-run"
    assert store_mod.current_version_seq() == 7
    assert store_mod.allocate_local_seq() == 8


def test_archive_manifest_has_no_body(client):
    manifest_doc = service.manifest_of(service.export())
    assert "snapshots" not in manifest_doc
    assert len(manifest_doc["integrity"]["payload_sha256"]) == 64
    assert manifest_doc["identity"]["batch_key"] == store.latest_run()["batch_key"]


def test_restore_rejects_unknown_kind(client):
    with pytest.raises(errors.BadInput):
        service.export(kind="merge")


def test_batch_key_not_reused_by_restore(client):
    local = store.latest_run()
    result = restore.restore_bytes(_package_bytes())
    restored = store.run_by_id(result["run_id"])
    assert restored["batch_key"] != local["batch_key"]
    # 本地再跑同一输入仍然命中本地批次，不会被恢复批次搅乱
    again = batch_service.execute()
    assert again["reused"] is True
    assert again["run_id"] == local["run_id"]
    assert manifest.run_id_for(local["batch_key"]) == local["run_id"]


def test_imported_runs_do_not_join_local_overlap_windows(client):
    store.latest_run()
    restore.restore_bytes(_package_bytes())
    local_windows = {item["run_id"] for item in store.list_windows()}
    imported_windows = {item["run_id"] for item in store.list_import_windows()}
    assert local_windows.isdisjoint(imported_windows)
    assert len(store.list_windows()) == 1


def test_export_unknown_run_is_404(client):
    with pytest.raises(errors.NotFound):
        service.export("fm-does-not-exist")
