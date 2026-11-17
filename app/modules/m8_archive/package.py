"""归档包格式与整包校验。

包是自洽的证据件：结构快照、引用、影响结果、账目全部在内，摘要算在规范化字节上。
校验先整体过一遍，任何一条不过就整包拒绝，绝不半包入库。
"""

from __future__ import annotations

from app import config, errors
from app.modules.m1_intake.service import schema_id_of
from app.modules.m8_archive import canonical

FORMAT = "lineageguard-batch-archive"
FORMAT_VERSION = 1
PACKAGE_KINDS = ("reference", "restore")

REQUIRED_TOP = (
    "format", "format_version", "kind", "source", "batch", "identity",
    "snapshots", "refs", "result", "integrity",
)
REQUIRED_INTEGRITY = ("algorithm", "payload_sha256", "sections")

MANDATED_SECTIONS = {
    "batch": "批次身份与生效区间",
    "snapshots": "结构快照",
    "refs": "引用登记",
    "result": "影响结果",
}

ALLOWED_SECTIONS = set(MANDATED_SECTIONS) | {"ledger", "mapping", "quarantine", "fields"}


def _bad(message: str, reason: str, **detail) -> errors.ArchiveRejected:
    payload = {"reason": reason}
    payload.update(detail)
    return errors.ArchiveRejected(message, detail=payload)


def fingerprint(batch: dict, result: dict, snapshots: dict,
                refs: dict | None = None) -> dict:
    """身份指纹：同源同号是不是同一批，只看内容指纹，不看导出时间。"""
    return {
        "batch_key": batch["batch_key"],
        "input_sha256": batch["input_sha256"],
        "ruleset": batch.get("ruleset"),
        "params": batch.get("params") or {},
        "effective_from": batch.get("effective_from"),
        "effective_to": batch.get("effective_to"),
        "version_label_old": batch.get("version_label_old"),
        "version_label_new": batch.get("version_label_new"),
        "old_schema_id": batch["old_schema_id"],
        "new_schema_id": batch["new_schema_id"],
        "source_sha256": {
            "old": (snapshots.get("old") or {}).get("source_sha256"),
            "new": (snapshots.get("new") or {}).get("source_sha256"),
            "base": (snapshots.get("base") or {}).get("source_sha256"),
        },
        "snapshot_digest": canonical.digest(snapshots),
        "refs_digest": canonical.digest(refs) if refs is not None else None,
        "result_digest": canonical.digest(result),
    }


def build_package(run: dict, snapshots: dict, source: dict | None = None,
                  kind: str = "reference") -> dict:
    """把一次批次整理成归档包；不含导出时间，保证同批次同摘要。"""
    from app.modules.m8_archive import result as result_mod

    source_run_id = run.get("source_run_id") or run["run_id"]
    batch = {
        "run_id": source_run_id,
        "batch_key": run.get("source_batch_key") or run["batch_key"],
        "version_seq": int(run["version_seq"]),
        "created_at": run["created_at"],
        "ruleset": run["ruleset"],
        "input_sha256": run["input_sha256"],
        "input_digests": dict(run.get("digests") or {}),
        "input_names": dict(run.get("input_names") or {}),
        "params": dict(run.get("params") or {}),
        "old_schema_id": run["old_schema_id"],
        "new_schema_id": run["new_schema_id"],
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "effective_from": run["effective_from"],
        "effective_to": run.get("effective_to"),
        "window_semantics": config.WINDOW_SEMANTICS,
        # 重新导出恢复批次时沿用原来源的写法，保证导出 → 再导出字节一致
        "origin": run.get("source_origin") or run.get("origin") or "local",
        "source_run_id": run.get("source_run_id") or run["run_id"],
        "source_version_seq": run.get("source_version_seq") or int(run["version_seq"]),
    }
    snapshot_section = snapshot_section_of(snapshots)

    refs_section = dict(run.get("refs") or {})
    result_section = result_mod.build(run)
    ledger_section = run.get("ledger") or {}
    mapping_section = run.get("mapping") or {}
    quarantine_section = {
        "old": run.get("quarantine_old") or [],
        "new": run.get("quarantine_new") or [],
    }

    resolved_source = {
        "source_id": ((source or {}).get("source_id")
                      or (run.get("source_id") if run.get("origin") == "imported" else None)
                      or config.SOURCE_ID),
        "source_run_id": batch["source_run_id"],
        "source_version_seq": batch["source_version_seq"],
        "origin": batch["origin"],
    }
    identity = fingerprint(batch, result_section, snapshot_section, refs_section)

    package = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "generator": {"app": config.APP_NAME, "ruleset": run["ruleset"],
                      "source_id": resolved_source["source_id"]},
        "source": resolved_source,
        "batch": batch,
        "identity": identity,
        "snapshots": snapshot_section,
        "refs": refs_section,
        "result": result_section,
        "ledger": ledger_section,
        "mapping": mapping_section,
        "quarantine": quarantine_section,
    }
    package["integrity"] = {
        "algorithm": "sha256",
        "canonical": "json-sort-keys-utf8",
        "payload_sha256": canonical.digest(package),
        "sections": {
            name: canonical.digest(package[name])
            for name in sorted(ALLOWED_SECTIONS)
            if name in package
        },
        "cross_checks": _cross_checks(package),
    }
    return package


def snapshot_section_of(snapshots: dict) -> dict:
    """结构快照段：只留能自证的部分，字段逐条带身份与行号。"""
    section: dict = {}
    for side in ("base", "old", "new"):
        snapshot = snapshots.get(side)
        if snapshot is None:
            continue
        section[side] = {
            "schema_id": snapshot["schema_id"],
            "version_label": snapshot["version_label"],
            "source_path": snapshot["source_path"],
            "source_sha256": snapshot["source_sha256"],
            "encoding": snapshot["encoding"],
            "bom": bool(snapshot.get("bom")),
            "field_count": int(snapshot["field_count"]),
            "blocked_count": int(snapshot["blocked_count"]),
            "fields": snapshot.get("fields") or [],
            "diagnostics": snapshot.get("diagnostics") or [],
            "tables": snapshot.get("tables") or [],
            "stats": snapshot.get("stats") or {},
        }
    return section


def _cross_checks(package: dict) -> dict:
    """包内关联完整性：摘要、账目、引用、影响结果互相能对上。"""
    batch = package.get("batch") or {}
    snapshots = package.get("snapshots") or {}
    refs = package.get("refs") or {}
    result = package.get("result") or {}
    ledger = package.get("ledger") or {}
    digest_map = batch.get("input_digests") or {}
    checks: dict[str, dict] = {}
    declared_sides = set(digest_map)
    for side in ("old", "new", "base"):
        snapshot = snapshots.get(side)
        if snapshot is None:
            # 输入摘要声明了这一侧就必须有对应快照；base 没声明才可以缺
            checks[f"snapshot_{side}"] = {
                "ok": side not in declared_sides,
                "detail": "缺快照" if side in declared_sides else "无此输入",
            }
            continue
        declared = digest_map.get(side)
        checks[f"snapshot_{side}"] = {
            "ok": declared == snapshot.get("source_sha256"),
            "detail": "源摘要一致" if declared == snapshot.get("source_sha256")
            else "源摘要与输入摘要不一致",
        }
    refs_total = sum(len(item.get("refs") or [])
                     for item in (refs.get("consumers") or []))
    checks["refs_total"] = {
        "ok": refs_total == int(refs.get("ref_total") or 0),
        "detail": f"登记 {refs_total} 处，声明 {refs.get('ref_total')} 处",
    }
    ledger_refs = (ledger.get("refs") or {})
    checks["ledger_refs"] = {
        "ok": int(ledger_refs.get("refs_total") or 0) == refs_total,
        "detail": "账目引用数与登记引用数一致" if int(
            ledger_refs.get("refs_total") or 0) == refs_total else "账目引用数与登记引用数不一致",
    }
    counts = (ledger.get("counts") or {})
    fields = (ledger.get("fields") or {})
    checks["ledger_balanced"] = {
        "ok": bool((ledger.get("balanced") or {}).get("all")),
        "detail": "四账平" if (ledger.get("balanced") or {}).get("all") else "账目不平",
    }
    checks["ledger_totals"] = {
        "ok": int(fields.get("old_total") or 0) == int(counts.get("old_total") or 0)
        and int(fields.get("new_total") or 0) == int(counts.get("new_total") or 0),
        "detail": "字段总数一致",
    }
    impact_summary = ((result.get("impact") or {}).get("summary") or {})
    checks["impact_refs"] = {
        "ok": int(impact_summary.get("refs_total") or 0) == refs_total,
        "detail": "影响结果与登记引用数不一致" if int(
            impact_summary.get("refs_total") or 0) != refs_total
        else "影响结果与登记引用数一致",
    }
    diff_rows = result.get("diff") or []
    counted = (int(result.get("counts", {}).get("matched") or 0)
               + int((result.get("buckets") or {}).get("deleted") or 0)
               + int((result.get("buckets") or {}).get("added") or 0))
    checks["result_rows"] = {
        "ok": len(diff_rows) == counted,
        "detail": (f"结果 {len(diff_rows)} 行，对账应为 {counted} 行"),
    }
    checks["result_matches_snapshots"] = _snapshot_result_check(snapshots, result)
    counts = (result.get("counts") or {})
    checks["result_totals"] = {
        "ok": len([row for row in diff_rows if row.get("bucket") != "added"])
        == int(counts.get("old_total") or 0)
        and len([row for row in diff_rows if row.get("bucket") != "deleted"])
        == int(counts.get("new_total") or 0),
        "detail": "结果行数推不出两侧字段总数",
    }
    breaking = len([row for row in diff_rows if row.get("verdict") == "BREAKING"])
    checks["result_verdicts"] = {
        "ok": breaking == int(counts.get("breaking") or 0)
        and breaking == int(((result.get("verdicts") or {}).get("BREAKING")) or 0),
        "detail": f"结果 {breaking} 条破坏，声明 {counts.get('breaking')} 条",
    }
    checks["result_buckets"] = {
        "ok": (sum(int((result.get("buckets") or {}).get(name, 0)) for name in
                   ("unchanged", "renamed", "retyped", "required", "conflict", "deleted"))
               == int(counts.get("old_total") or 0))
        and (sum(int((result.get("buckets") or {}).get(name, 0)) for name in
                 ("unchanged", "renamed", "retyped", "required", "conflict", "added"))
             == int(counts.get("new_total") or 0)),
        "detail": "结果字段账与两侧总数平" if (
            sum(int((result.get("buckets") or {}).get(name, 0)) for name in
                ("unchanged", "renamed", "retyped", "required", "conflict", "deleted"))
            == int(counts.get("old_total") or 0)
            and sum(int((result.get("buckets") or {}).get(name, 0)) for name in
                    ("unchanged", "renamed", "retyped", "required", "conflict", "added"))
            == int(counts.get("new_total") or 0)) else "结果字段账与两侧总数不平",
    }
    return checks


def _snapshot_result_check(snapshots: dict, result: dict) -> dict:
    """结果逐行必须落在结构快照里：身份、表、旧名、新名、类型都得对得上。"""
    old_fields = {field.get("field_id"): field
                  for field in (snapshots.get("old") or {}).get("fields") or []}
    new_fields = {field.get("field_id"): field
                  for field in (snapshots.get("new") or {}).get("fields") or []}
    for row in result.get("diff") or []:
        field_id = row.get("field_id")
        bucket = row.get("bucket")
        if bucket != "added":
            old = old_fields.get(field_id)
            if old is None or old.get("table") != row.get("table") \
                    or old.get("name") != row.get("old_name"):
                return {"ok": False, "detail": f"结果行 {field_id} 与旧快照对不上"}
        if bucket != "deleted":
            new = new_fields.get(field_id)
            if new is None or new.get("table") != row.get("table") \
                    or new.get("name") != row.get("new_name"):
                return {"ok": False, "detail": f"结果行 {field_id} 与新快照对不上"}
    return {"ok": True, "detail": "结果逐行与结构快照一致"}


def validate_package(raw) -> dict:
    """整包校验；任何一条不过就抛 ARCHIVE_REJECTED，不返回半份结果。"""
    if not isinstance(raw, dict):
        raise _bad("归档包顶层必须是对象", "NOT_OBJECT",
                   type=type(raw).__name__)
    missing = [key for key in REQUIRED_TOP if key not in raw]
    if missing:
        raise _bad("归档包缺少必需段", "MISSING_SECTION", keys=missing)
    if raw.get("format") != FORMAT:
        raise _bad("不是 lineageguard 归档包", "BAD_FORMAT", format=raw.get("format"))
    if raw.get("format_version") != FORMAT_VERSION:
        raise _bad("归档包版本不认识", "BAD_FORMAT_VERSION",
                   version=raw.get("format_version"))
    if raw.get("kind") not in PACKAGE_KINDS:
        raise _bad("归档包类型不认识", "BAD_KIND", kind=raw.get("kind"))

    integrity = raw.get("integrity")
    if not isinstance(integrity, dict):
        raise _bad("归档包缺少校验段", "MISSING_INTEGRITY")
    absent = [key for key in REQUIRED_INTEGRITY if key not in integrity]
    if absent:
        raise _bad("校验段不完整", "MISSING_INTEGRITY", keys=absent)
    if integrity.get("algorithm") != "sha256":
        raise _bad("只接受 sha256 摘要", "BAD_ALGORITHM",
                   algorithm=integrity.get("algorithm"))

    # 摘要必须算在规范化字节上：重排键、改空白都不该改变结论
    document = {k: v for k, v in raw.items() if k != "integrity"}
    try:
        recomputed = canonical.digest(document)
    except (ValueError, TypeError) as exc:
        raise _bad("归档内容无法规范化", "NOT_CANONICAL", detail=str(exc))
    if recomputed != integrity.get("payload_sha256"):
        raise _bad(
            "归档摘要不匹配，整包拒绝",
            "DIGEST_MISMATCH",
            expected=integrity.get("payload_sha256"),
            actual=recomputed,
        )

    sections = integrity.get("sections")
    if not isinstance(sections, dict):
        raise _bad("缺少分段摘要", "MISSING_SECTION_DIGEST")
    for name in MANDATED_SECTIONS:
        if name not in raw:
            raise _bad("归档包缺少必需段", "MISSING_SECTION", keys=[name])
        if name not in sections:
            raise _bad("缺少分段摘要", "MISSING_SECTION_DIGEST", keys=[name])
    extra = sorted(set(sections) - ALLOWED_SECTIONS)
    if extra:
        raise _bad("校验段引用了未知分段", "UNKNOWN_SECTION", keys=extra)
    for name, digest in sections.items():
        if name not in raw:
            raise _bad("分段摘要指向不存在的段", "SECTION_ABSENT", key=name)
        try:
            actual = canonical.digest(raw[name])
        except (ValueError, TypeError) as exc:
            raise _bad("分段无法规范化", "NOT_CANONICAL", key=name, detail=str(exc))
        if actual != digest:
            raise _bad("分段摘要不匹配，整包拒绝", "SECTION_DIGEST_MISMATCH",
                       key=name, expected=digest, actual=actual)

    batch = raw["batch"]
    for key in ("run_id", "batch_key", "version_seq", "created_at", "ruleset",
                "input_sha256", "input_digests", "old_schema_id", "new_schema_id",
                "effective_from", "source_run_id", "source_version_seq"):
        if key not in batch:
            raise _bad("批次段缺少字段", "MISSING_BATCH_FIELD", key=key)
    if not isinstance(batch.get("version_seq"), int) or batch["version_seq"] < 1:
        raise _bad("来源版本号必须是正整数", "BAD_SOURCE_VERSION",
                   value=repr(batch.get("version_seq")))
    if not isinstance(batch.get("source_version_seq"), int) or batch["source_version_seq"] < 1:
        raise _bad("来源版本号必须是正整数", "BAD_SOURCE_VERSION",
                   value=repr(batch.get("source_version_seq")))

    source = raw["source"]
    source_id = source.get("source_id") if isinstance(source, dict) else None
    if not isinstance(source_id, str) or not config.SOURCE_ID_RE.match(source_id):
        raise _bad("来源身份不合法", "BAD_SOURCE_ID", value=repr(source_id))
    if source.get("source_run_id") != batch.get("source_run_id"):
        raise _bad("来源身份与批次段不一致", "SOURCE_MISMATCH")
    if source.get("source_version_seq") != batch.get("source_version_seq"):
        raise _bad("来源版本号与批次段不一致", "SOURCE_MISMATCH")

    identity = raw["identity"]
    if not isinstance(identity, dict):
        raise _bad("身份指纹段不合法", "BAD_IDENTITY")
    recomputed_identity = fingerprint(batch, raw["result"], raw["snapshots"],
                                      raw["refs"])
    if canonical.digest(identity) != canonical.digest(recomputed_identity):
        raise _bad("身份指纹与内容不一致，整包拒绝", "IDENTITY_MISMATCH")

    snapshots = raw["snapshots"]
    if not isinstance(snapshots, dict):
        raise _bad("快照段不合法", "BAD_SNAPSHOTS")
    for side in ("old", "new"):
        snapshot = snapshots.get(side)
        if not isinstance(snapshot, dict):
            raise _bad("缺少结构快照", "SNAPSHOT_MISSING", side=side)
        for key in ("schema_id", "version_label", "source_sha256", "fields"):
            if key not in snapshot:
                raise _bad("结构快照缺少字段", "SNAPSHOT_INCOMPLETE",
                           side=side, key=key)
    base = snapshots.get("base")
    if base is not None:
        for key in ("schema_id", "source_sha256", "fields"):
            if key not in base:
                raise _bad("基线快照缺少字段", "SNAPSHOT_INCOMPLETE",
                           side="base", key=key)
    elif "base" in (batch.get("input_digests") or {}):
        raise _bad("声明了基线输入却缺基线快照", "SNAPSHOT_MISSING", side="base")

    refs = raw["refs"]
    if not isinstance(refs, dict) or not isinstance(refs.get("consumers"), list):
        raise _bad("引用段不合法", "BAD_REFS")
    for item in refs["consumers"]:
        if not isinstance(item, dict) or not isinstance(item.get("refs"), list):
            raise _bad("引用登记项不合法", "BAD_REFS")
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise _bad("引用登记项缺名字", "BAD_REFS")

    checks = integrity.get("cross_checks")
    if not isinstance(checks, dict) or not checks:
        raise _bad("缺少关联完整性校验", "MISSING_CROSS_CHECKS")
    failed = sorted(key for key, item in checks.items()
                    if not isinstance(item, dict) or not item.get("ok"))
    if failed:
        raise _bad("包内关联不完整，整包拒绝", "CROSS_CHECK_FAILED", keys=failed)
    recomputed_checks = _cross_checks(raw)
    broken = sorted(key for key in recomputed_checks if not recomputed_checks[key]["ok"])
    if broken:
        raise _bad("包内关联复算不通过，整包拒绝", "CROSS_CHECK_FAILED", keys=broken)

    # 身份指纹里的结果摘要必须就是 result 段的实际摘要
    if identity.get("result_digest") != canonical.digest(raw["result"]):
        raise _bad("结果摘要与身份指纹不一致", "IDENTITY_MISMATCH")
    if identity.get("snapshot_digest") != canonical.digest(raw["snapshots"]):
        raise _bad("结构快照摘要与身份指纹不一致", "IDENTITY_MISMATCH")
    if identity.get("refs_digest") != canonical.digest(raw["refs"]):
        raise _bad("引用摘要与身份指纹不一致", "IDENTITY_MISMATCH")
    # 结构快照必须自证身份：schema_id 由版本标签与字段清单重新推导
    for side in ("base", "old", "new"):
        snapshot = snapshots.get(side)
        if not isinstance(snapshot, dict):
            continue
        try:
            derived = schema_id_of(snapshot["version_label"], snapshot["fields"])
        except (KeyError, TypeError):
            raise _bad("结构快照字段不完整", "SNAPSHOT_INCOMPLETE", side=side)
        if derived != snapshot.get("schema_id"):
            raise _bad("结构快照身份与内容不符，整包拒绝", "SNAPSHOT_ID_MISMATCH",
                       side=side, expected=snapshot.get("schema_id"), actual=derived)
    old_digest = (batch.get("input_digests") or {}).get("old")
    new_digest = (batch.get("input_digests") or {}).get("new")
    if snapshots["old"].get("source_sha256") != old_digest:
        raise _bad("旧版快照与输入摘要不符", "CROSS_CHECK_FAILED", side="old")
    if snapshots["new"].get("source_sha256") != new_digest:
        raise _bad("新版快照与输入摘要不符", "CROSS_CHECK_FAILED", side="new")

    return raw


def load_package(data: bytes | str, *, max_bytes: int | None = None) -> dict:
    """从字节或文本读包：先限大小，再解 JSON，再整包校验。"""
    import json

    limit = max_bytes or config.MAX_ARCHIVE_BYTES
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        raise _bad("归档包为空", "EMPTY_ARCHIVE")
    if len(data) > limit:
        raise _bad("归档包过大", "ARCHIVE_TOO_LARGE",
                   size=len(data), max=limit)
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise _bad("归档包不是 UTF-8", "BAD_ENCODING", pos=exc.start)
    except json.JSONDecodeError as exc:
        raise _bad("归档包不是合法 JSON", "BAD_JSON", pos=exc.pos, msg=exc.msg)
    return validate_package(raw)
