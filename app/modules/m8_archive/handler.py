"""M8 入口：按批次导出归档、整包恢复、导入与冲突回读。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import config, errors, store, web
from app.modules.m8_archive import restore, service

router = APIRouter()


@router.get("/api/export/archive.json")
def export_archive(run_id: str | None = None, kind: str = "reference"):
    """按批次导出：带结构快照、引用、影响结果、摘要与关联校验。"""
    body = service.export_bytes(run_id, kind)
    run = store.run_by_id(run_id) if run_id else store.latest_run()
    name = f"lineageguard-{(run or {}).get('run_id', 'batch')}.archive.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Archive-Sha256": service.canonical.sha256_bytes(body),
        },
    )


@router.get("/api/export/archive-manifest.json")
def export_archive_manifest(run_id: str | None = None):
    """归档清单：只有摘要与来源身份，不含包体。"""
    return service.manifest_of(service.export(run_id))


@router.post("/api/archives/restore")
async def restore_archive(request: Request):
    """整包恢复：先整包校验，坏包一行不写；同源同号异内容隔离为冲突。"""
    raw = await request.body()
    if not raw:
        raise errors.BadInput("需要给归档包", detail={"field": "body"})
    result = restore.restore_bytes(raw)
    return {
        "status": result["status"],
        "reused": result["reused"],
        "run_id": result["run_id"],
        "local_seq": result["local_seq"],
        "version_seq": result["version_seq"],
        "source": result["source"],
        "import_id": result["import_id"],
        "archive_sha256": result["archive_sha256"],
        "warnings": (result.get("run") or {}).get("warnings") or [],
        "counts": ((result.get("run") or {}).get("ledger") or {}).get("counts") or {},
        "balanced": ((result.get("run") or {}).get("ledger") or {}).get("balanced") or {},
    }


@router.get("/api/archives")
def archive_history():
    """历史回读：当前批次与导入批次分栏，来源身份逐条标明。"""
    view = restore.history_view()
    return {
        "source_id": config.SOURCE_ID,
        "current": view["current"],
        "imported": view["imported"],
        "pending": [
            {key: item[key] for key in
             ("import_id", "status", "source_id", "source_version_seq",
              "source_run_id", "local_seq", "detail", "created_at")}
            for item in view["pending"]
        ],
        "conflicts": view["conflicts"],
        "imports": store.count_imports(),
        "conflict_count": store.count_conflicts(),
    }


@router.post("/api/archives/replay")
def replay_imports():
    """把停在 prepared 的导入接着做完；半包未完成前不会出现在历史里。"""
    return {"results": restore.replay_pending()}


@router.get("/api/archives/{run_id}")
def imported_run(run_id: str):
    run = store.run_by_id(run_id)
    if run is None:
        raise errors.NotFound("没有这个批次", detail={"run_id": run_id})
    if run.get("origin") != "imported":
        raise errors.BadInput("这个批次是本地跑的，按批次导出即可",
                             detail={"run_id": run_id, "origin": run.get("origin")})
    import_record = store.import_by_id(run.get("import_id") or "")
    return {
        "run": run,
        # 包体太大且不用于回读，这里只给记录本身
        "import": ({key: value for key, value in import_record.items()
                    if key != "archive"} if import_record else None),
        "origin_view": restore.origin_view(run),
    }


@router.get("/archives", response_class=HTMLResponse)
def archives_page(request: Request):
    view = restore.history_view()
    return web.render(
        request,
        "archives.html",
        "archives",
        current=view["current"],
        imported=view["imported"],
        pending=view["pending"],
        conflicts=view["conflicts"],
        source_id=config.SOURCE_ID,
        ledger_current=(store.latest_run() or {}).get("ledger") or {},
    )
