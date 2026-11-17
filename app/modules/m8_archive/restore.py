"""恢复：保留来源身份与原版本，另分配本地顺序。

三条硬规矩：
1. 恢复出来的批次按 (来源, 来源版本号) 认，不改写 version_seq，也不覆盖本地当前批次。
2. 同源同号但内容指纹不同 → 隔离成冲突，不入 runs、不覆盖。
3. 两阶段导入：先写 prepared 记录，再在同一事务里落批次并标 applied；
   中途断掉只留 prepared，批次不可见，重放同包即可续上。
"""

from __future__ import annotations

from app import config, errors, store
from app.modules.m6_batches import history
from app.modules.m8_archive import canonical, package, result as result_mod

STATUS_APPLIED = "applied"
STATUS_CONFLICT = "conflict"
STATUS_DUPLICATE = "duplicate"
STATUS_STARTED = "started"
STATUS_PREPARED = "prepared"
STATUS_REJECTED = "rejected"
STATUS_PENDING = (STATUS_STARTED, STATUS_PREPARED)


def _now() -> str:
    return config.now_utc().isoformat()


# 中断注入点：只在测试里替换，生产恒为 no-op
def _failpoint(_name: str) -> None:
    return None


def _identity(package_doc: dict) -> tuple[str, int]:
    source = package_doc["source"]
    return source["source_id"], int(source["source_version_seq"])


def _local_run_id(package_doc: dict, local_seq: int) -> str:
    """本地运行号：与来源号脱钩，避免同号顶掉；同一本地顺序必然同一号。"""
    seed = {
        "source_id": package_doc["source"]["source_id"],
        "source_version_seq": int(package_doc["source"]["source_version_seq"]),
        "payload_sha256": package_doc["integrity"]["payload_sha256"],
        "local_seq": int(local_seq),
    }
    return "rs-" + canonical.digest(seed)[:12]


def _run_from_package(package_doc: dict, local_seq: int, import_id: str,
                      archive_sha256: str) -> dict:
    """归档包 → 运行载荷；判定结果原样用，不重跑也不读样例目录。"""
    batch = package_doc["batch"]
    snapshots = package_doc["snapshots"]
    refs = package_doc["refs"]
    result = package_doc["result"]
    ledger = package_doc.get("ledger") or {}
    imported_at = _now()
    origin_source = package_doc["source"]

    diff = result.get("diff") or []
    counts = result.get("counts") or {}
    # 本地批次键按来源身份派生：不占用本地跑批的批次键，来源键另存原值
    run = {
        "run_id": _local_run_id(package_doc, local_seq),
        "batch_key": "imp-" + canonical.digest({
            "source_id": origin_source["source_id"],
            "source_version_seq": int(origin_source["source_version_seq"]),
        })[:24],
        "old_schema_id": batch["old_schema_id"],
        "new_schema_id": batch["new_schema_id"],
        "version_label_old": batch.get("version_label_old"),
        "version_label_new": batch.get("version_label_new"),
        # version_seq 保留来源版本号：这是原证据，不许改
        "version_seq": int(batch["source_version_seq"]),
        "input_sha256": batch["input_sha256"],
        "digests": dict(batch.get("input_digests") or {}),
        "input_names": dict(batch.get("input_names") or {}),
        "params": dict(batch.get("params") or {}),
        "ruleset": batch["ruleset"],
        "created_at": batch["created_at"],
        "effective_from": batch["effective_from"],
        "effective_to": batch.get("effective_to"),
        "window": {
            "from": batch["effective_from"],
            "to": batch.get("effective_to"),
            "semantics": batch.get("window_semantics") or config.WINDOW_SEMANTICS,
        },
        "fields_old": (snapshots.get("old") or {}).get("fields") or [],
        "fields_new": (snapshots.get("new") or {}).get("fields") or [],
        "type_stats_old": (result.get("type_stats") or {}).get("old") or {},
        "type_stats_new": (result.get("type_stats") or {}).get("new") or {},
        "diff": diff,
        "buckets": result.get("buckets") or {},
        "changes": result.get("changes") or {},
        "verdicts": result.get("verdicts") or {},
        "counts": counts,
        "name_clashes": result.get("name_clashes") or [],
        "mapping": package_doc.get("mapping") or {},
        "refs": refs,
        "resolution": result.get("resolution") or {},
        "impact": result_mod.rebuild_impact(result),
        "ledger": ledger,
        "quarantine": (
            [dict(row, side="old") for row in
             (package_doc.get("quarantine") or {}).get("old") or []]
            + [dict(row, side="new") for row in
               (package_doc.get("quarantine") or {}).get("new") or []]
        ),
        "quarantine_old": (package_doc.get("quarantine") or {}).get("old") or [],
        "quarantine_new": (package_doc.get("quarantine") or {}).get("new") or [],
        "warnings": [],
        "reused": False,
        "origin": "imported",
        "local_seq": local_seq,
        "source_id": origin_source["source_id"],
        "source_version_seq": int(origin_source["source_version_seq"]),
        "source_run_id": origin_source["source_run_id"],
        "source_batch_key": batch["batch_key"],
        "source_origin": package_doc["source"].get("origin") or batch.get("origin") or "local",
        "import_id": import_id,
        "archive_sha256": archive_sha256,
        "imported_at": imported_at,
        "source_identity": package_doc["identity"],
    }
    # 来源摘要与导入信息参与本地顺序之后的展示，不参与判定
    run["import"] = {
        "import_id": import_id,
        "archive_sha256": archive_sha256,
        "imported_at": imported_at,
        "payload_sha256": package_doc["integrity"]["payload_sha256"],
        "source_id": origin_source["source_id"],
        "source_version_seq": int(origin_source["source_version_seq"]),
        "source_run_id": origin_source["source_run_id"],
    }
    run["summary"] = history.summary_of(run)
    return run


def _write_snapshots(package_doc: dict) -> None:
    """结构快照按原 schema_id 落库；已存在同号快照一律不覆盖。"""
    for side in ("base", "old", "new"):
        snapshot = (package_doc["snapshots"] or {}).get(side)
        if not isinstance(snapshot, dict):
            continue
        if store.load_snapshot(snapshot["schema_id"]) is not None:
            continue
        stored = dict(snapshot)
        stored.setdefault("created_at", package_doc["batch"]["created_at"])
        stored.setdefault("source_sha256", "")
        stored.setdefault("source_path", "")
        stored.setdefault("encoding", "")
        stored.setdefault("bom", False)
        stored.setdefault("field_count", len(stored.get("fields") or []))
        stored.setdefault("blocked_count", 0)
        store.save_snapshot(stored)
        if stored.get("diagnostics"):
            store.save_quarantine(stored["schema_id"], stored["diagnostics"])


def _record_import(package_doc: dict, archive_sha256: str, status: str,
                   run_id: str | None = None, detail: str | None = None,
                   created_at: str | None = None,
                   local_seq: int | None = None) -> dict:
    source_id, source_version_seq = _identity(package_doc)
    now = _now()
    record = {
        "import_id": import_id_for(archive_sha256),
        "archive_sha256": archive_sha256,
        "source_id": source_id,
        "source_version_seq": source_version_seq,
        "source_run_id": package_doc["source"]["source_run_id"],
        "status": status,
        "run_id": run_id,
        "detail": detail,
        "local_seq": local_seq,
        "archive": None,
        "created_at": created_at or now,
        "updated_at": now,
    }
    store.save_import(record)
    return record


def import_id_for(archive_sha256: str) -> str:
    """导入号只看整包摘要：同一份包重放必然落到同一条记录。"""
    return "im-" + archive_sha256[:12]


def _existing_for(package_doc: dict) -> dict | None:
    source_id, source_version_seq = _identity(package_doc)
    return store.imported_run(source_id, source_version_seq)


def _conflict_hit(package_doc: dict, existing: dict) -> bool:
    """同源同号：内容指纹不同才算冲突；相同就是同一批，按重复导入处理。"""
    stored = existing.get("source_identity")
    if not isinstance(stored, dict):
        # 老库没存指纹：只能按批次键与输入摘要比，宁可判重复也不误报冲突
        stored = {
            "batch_key": existing.get("source_batch_key") or existing.get("batch_key"),
            "input_sha256": existing.get("input_sha256"),
        }
        incoming = package_doc["identity"]
        return (stored["batch_key"] != incoming["batch_key"]
                or stored["input_sha256"] != incoming["input_sha256"])
    return canonical.digest(stored) != canonical.digest(package_doc["identity"])


def _isolate_conflict(package_doc: dict, archive_sha256: str,
                      existing: dict | None) -> dict:
    source_id, source_version_seq = _identity(package_doc)
    detail = "同源同号但内容指纹不同，已隔离，不覆盖原批次"
    store.save_conflict({
        "conflict_id": "cf-" + canonical.digest(
            {"source_id": source_id, "source_version_seq": source_version_seq}
        )[:12],
        "source_id": source_id,
        "source_version_seq": source_version_seq,
        "existing_run_id": (existing or {}).get("run_id"),
        "existing_sha256": (existing or {}).get("archive_sha256"),
        "incoming_run_id": package_doc["batch"]["run_id"],
        "incoming_sha256": archive_sha256,
        "detail": detail,
        "created_at": _now(),
    })
    _record_import(package_doc, archive_sha256, STATUS_CONFLICT, detail=detail)
    raise errors.ArchiveConflict(
        "同源同号异内容，已隔离为冲突",
        detail={
            "reason": "ARCHIVE_CONFLICT",
            "source_id": source_id,
            "source_version_seq": source_version_seq,
            "existing_run_id": (existing or {}).get("run_id"),
            "incoming_run_id": package_doc["batch"]["run_id"],
        },
    )


def _duplicate(package_doc: dict, archive_sha256: str, existing: dict) -> dict:
    """同一批重复导入：不新增行，直接回读已有批次。"""
    record = _record_import(package_doc, archive_sha256, STATUS_DUPLICATE,
                            run_id=existing["run_id"],
                            detail="同一归档重放，未新增批次")
    return {
        "status": STATUS_DUPLICATE,
        "reused": True,
        "run_id": existing["run_id"],
        "local_seq": int(existing.get("local_seq") or 0),
        "version_seq": int(existing.get("version_seq") or 0),
        "source": package_doc["source"],
        "import_id": record["import_id"],
        "archive_sha256": archive_sha256,
        "run": existing,
    }


def _finish(record: dict, package_doc: dict, archive_sha256: str,
            run: dict) -> dict:
    return _record_import(package_doc, archive_sha256, STATUS_APPLIED,
                          run_id=run["run_id"],
                          detail="恢复完成",
                          created_at=record["created_at"])


def restore_bytes(data: bytes | str, *, max_bytes: int | None = None) -> dict:
    """整包校验 → 身份判定 → 两阶段导入。坏包一行都不写。"""
    package_doc = package.load_package(data, max_bytes=max_bytes)
    if isinstance(data, str):
        data = data.encode("utf-8")
    archive_sha256 = package.canonical.sha256_bytes(data)

    existing = _existing_for(package_doc)
    if existing is not None:
        if _conflict_hit(package_doc, existing):
            return _isolate_conflict(package_doc, archive_sha256, existing)
        return _duplicate(package_doc, archive_sha256, existing)

    # 同源同号但还停在半路的另一份包：内容指纹不同就是冲突，不能各自往下写
    pending_same_identity = store.import_by_identity(*_identity(package_doc))
    if (pending_same_identity is not None
            and pending_same_identity["status"] in STATUS_PENDING
            and import_id_for(archive_sha256) != pending_same_identity["import_id"]):
        return _isolate_conflict(package_doc, archive_sha256, None)

    # 第一段：原子占位（含包体），已经完成的导入不会被后来的请求覆盖
    import_id = import_id_for(archive_sha256)
    created_at = None
    local_seq = None
    draft = {
        "import_id": import_id,
        "archive_sha256": archive_sha256,
        "source_id": package_doc["source"]["source_id"],
        "source_version_seq": int(package_doc["source"]["source_version_seq"]),
        "source_run_id": package_doc["source"]["source_run_id"],
        "status": STATUS_PREPARED,
        "detail": "已校验，待落库",
        "local_seq": None,
        "created_at": _now(),
        "updated_at": _now(),
        "archive": package_doc,
    }
    claimed = store.claim_import(draft)
    if claimed is not None:
        import_id = claimed["import_id"]
        created_at = claimed["created_at"]
        local_seq = int(claimed.get("local_seq") or 0) or None
        if claimed["status"] in (STATUS_APPLIED, STATUS_DUPLICATE):
            existing = store.run_by_id(claimed.get("run_id") or "") if claimed.get("run_id") else None
            if existing is not None:
                return _duplicate(package_doc, archive_sha256, existing)
            return {
                "status": claimed["status"], "reused": True,
                "run_id": claimed.get("run_id"),
                "local_seq": int(claimed.get("local_seq") or 0),
                "version_seq": int((existing or {}).get("version_seq") or 0),
                "source": package_doc["source"], "import_id": import_id,
                "archive_sha256": archive_sha256, "run": existing,
            }
        if claimed["status"] == STATUS_CONFLICT:
            raise errors.ArchiveConflict(
                "同源同号异内容，已隔离为冲突",
                detail={"reason": "ARCHIVE_CONFLICT",
                        "source_id": claimed["source_id"],
                        "source_version_seq": claimed["source_version_seq"]},
            )

    record = {"import_id": import_id, "archive_sha256": archive_sha256,
              "source_id": package_doc["source"]["source_id"],
              "source_version_seq": int(package_doc["source"]["source_version_seq"]),
              "source_run_id": package_doc["source"]["source_run_id"],
              "status": STATUS_PREPARED, "detail": "已校验，待落库",
              "local_seq": local_seq, "created_at": created_at or _now(),
              "updated_at": _now(), "archive": package_doc}

    if not local_seq:
        local_seq = store.allocate_local_seq()
        record["local_seq"] = local_seq
        # 先记下预分配顺序：中途断掉，重放沿用同一个本地顺序，不跳号、不重号
        _persist_archive(import_id, package_doc, record)
    run = _run_from_package(package_doc, local_seq, import_id, archive_sha256)
    _write_snapshots(package_doc)
    _failpoint("after_prepare")

    # 第二段：同一事务里落批次 + 标记 applied
    finished = dict(record)
    finished["status"] = STATUS_APPLIED
    finished["run_id"] = run["run_id"]
    finished["detail"] = "恢复完成"
    finished["updated_at"] = _now()
    if not store.apply_import(run, finished):
        conflict = store.conflict_for_identity(*_identity(package_doc))
        if conflict is not None:
            raise errors.ArchiveConflict(
                "同源同号异内容，已隔离为冲突",
                detail={
                    "reason": "ARCHIVE_CONFLICT",
                    "source_id": conflict["source_id"],
                    "source_version_seq": conflict["source_version_seq"],
                    "existing_run_id": conflict.get("existing_run_id"),
                    "incoming_run_id": conflict.get("incoming_run_id"),
                },
            )
        existing = _existing_for(package_doc)
        if existing is None:
            raise errors.ImportInterrupted(
                "导入未完成，批次不可见，可用同一归档重放",
                detail={"reason": "IMPORT_INTERRUPTED", "import_id": import_id,
                        "local_seq": local_seq},
            )
        return _duplicate(package_doc, archive_sha256, existing)
    _failpoint("after_apply")

    return {
        "status": STATUS_APPLIED,
        "reused": False,
        "run_id": run["run_id"],
        "local_seq": local_seq,
        "version_seq": run["version_seq"],
        "source": package_doc["source"],
        "import_id": import_id,
        "archive_sha256": archive_sha256,
        "run": run,
    }


def _persist_archive(import_id: str, package_doc: dict, record: dict) -> None:
    """把包体写回 prepared 记录：续跑时不必再要原始文件。"""
    stored = store.import_by_id(import_id) or {}
    stored.update({
        "import_id": import_id,
        "archive_sha256": record["archive_sha256"],
        "source_id": record["source_id"],
        "source_version_seq": record["source_version_seq"],
        "source_run_id": record["source_run_id"],
        "status": record["status"],
        "run_id": record.get("run_id"),
        "detail": record.get("detail"),
        "local_seq": record.get("local_seq"),
        "archive": package_doc,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    })
    store.save_import(stored)


def replay_pending() -> list[dict]:
    """启动时或手动触发：把停在 prepared 的导入接着做完。

    半包不可见：没走到 applied 的批次不会出现在历史、当前批次或任何页面。
    """
    results: list[dict] = []
    for pending in store.list_pending_imports():
        record = store.import_by_id(pending["import_id"])
        package_doc = (record or {}).get("archive")
        if not isinstance(package_doc, dict):
            results.append({"import_id": pending["import_id"],
                            "status": "lost",
                            "detail": "包体缺失，无法续跑"})
            continue
        try:
            package.validate_package(package_doc)
            result = _replay_one(package_doc, record)
        except errors.FieldmapError as exc:
            results.append({"import_id": pending["import_id"],
                            "status": "failed", "detail": exc.message})
            continue
        except Exception as exc:  # noqa: BLE001 - 单条续跑失败不能拖垮启动
            results.append({"import_id": pending["import_id"],
                            "status": "failed", "detail": repr(exc)})
            continue
        results.append(result)
    return results


def _replay_one(package_doc: dict, record: dict) -> dict:
    source_id, source_version_seq = _identity(package_doc)
    existing = store.imported_run(source_id, source_version_seq)
    if existing is not None:
        _finish(record, package_doc, record["archive_sha256"], existing)
        return {"import_id": record["import_id"], "status": STATUS_APPLIED,
                "reused": True, "run_id": existing["run_id"]}
    local_seq = int(record.get("local_seq") or 0) or store.allocate_local_seq()
    run = _run_from_package(package_doc, local_seq, record["import_id"],
                           record["archive_sha256"])
    _write_snapshots(package_doc)
    finished = dict(record)
    finished["status"] = STATUS_APPLIED
    finished["run_id"] = run["run_id"]
    finished["detail"] = "中断后重放完成"
    finished["updated_at"] = _now()
    if not store.apply_import(run, finished):
        return {"import_id": record["import_id"], "status": "skipped",
                "detail": "已有同源同号批次"}
    return {"import_id": record["import_id"], "status": STATUS_APPLIED,
            "reused": False, "run_id": run["run_id"], "local_seq": local_seq}


def history_view() -> dict:
    """历史回读：当前批次与导入批次分栏，来源身份逐条标明。"""
    current = store.latest_run()
    imported = store.list_imported_runs()
    return {
        "current": origin_view(current),
        "imported": [origin_view(run) for run in imported],
        "pending": store.list_pending_imports(),
        "conflicts": store.list_conflicts(),
    }


def origin_view(run: dict | None) -> dict | None:
    if not run:
        return None
    return {
        "run_id": run.get("run_id"),
        "origin": run.get("origin") or "local",
        "local_seq": int(run.get("local_seq") or 0),
        "version_seq": int(run.get("version_seq") or 0),
        "source_id": run.get("source_id"),
        "source_version_seq": run.get("source_version_seq"),
        "source_run_id": run.get("source_run_id"),
        "imported_at": run.get("imported_at"),
        "archive_sha256": run.get("archive_sha256"),
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
    }
