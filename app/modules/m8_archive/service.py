"""归档导出：按批次出一个自洽的包。

导出不重跑判定，只用已存结果，所以同一批次导出多少次都是同一份字节。
"""

from __future__ import annotations

from app import config, errors, store
from app.modules.m1_intake import service as intake
from app.modules.m8_archive import canonical, package


def _snapshots_of(run: dict) -> dict:
    """从批次载荷还原三份结构快照；缺的在库里按 schema_id 补，不读原始文件。"""
    snapshots: dict = {}
    for side, key in (("old", "fields_old"), ("new", "fields_new")):
        snapshot = store.load_snapshot(run.get(f"{side}_schema_id") or "")
        if snapshot is None:
            raise errors.NotFound(
                "批次依赖的结构快照已不在库里，无法导出",
                detail={"side": side, "schema_id": run.get(f"{side}_schema_id")},
            )
        snapshots[side] = snapshot
    base_sha = (run.get("digests") or {}).get("base")
    if base_sha:
        base = store.find_snapshot_by_digest(base_sha)
        if base is None:
            raise errors.NotFound(
                "批次声明了基线输入，但基线结构快照已不在库里",
                detail={"side": "base", "source_sha256": base_sha},
            )
        snapshots["base"] = base
    return snapshots


def export(run_id: str | None = None, kind: str = "reference") -> dict:
    if kind not in package.PACKAGE_KINDS:
        raise errors.BadInput("归档类型不认识", detail={"kind": kind})
    run = store.run_by_id(run_id) if run_id else store.latest_run()
    if run is None:
        raise errors.NotFound("还没有批次可以归档",
                             detail={"run_id": run_id} if run_id else {})
    snapshots = _snapshots_of(run)
    return package.build_package(run, snapshots, kind=kind)


def export_bytes(run_id: str | None = None, kind: str = "reference") -> bytes:
    return canonical.encode(export(run_id, kind))


def manifest_of(package_doc: dict) -> dict:
    """归档摘要清单：不落包体，只给校验值与来源身份，便于对账。"""
    return {
        "format": package_doc["format"],
        "format_version": package_doc["format_version"],
        "kind": package_doc["kind"],
        "source": package_doc["source"],
        "batch": {
            "run_id": package_doc["batch"]["run_id"],
            "batch_key": package_doc["batch"]["batch_key"],
            "version_seq": package_doc["batch"]["version_seq"],
            "created_at": package_doc["batch"]["created_at"],
            "ruleset": package_doc["batch"]["ruleset"],
        },
        "identity": package_doc["identity"],
        "integrity": {
            "algorithm": package_doc["integrity"]["algorithm"],
            "payload_sha256": package_doc["integrity"]["payload_sha256"],
            "sections": package_doc["integrity"]["sections"],
        },
        "size_hint": len(canonical.encode(package_doc)),
        "max_bytes": config.MAX_ARCHIVE_BYTES,
    }
