"""SQLite 读写：批次快照、运行清单、隔离行、归档导入日志与键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
本地批次（origin='local'）与恢复批次（origin='imported'）共用表，
但版本号、批次键、运行号与生效区间各自独立：导入不推进本地版本，也不抢占当前批次。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    origin              TEXT NOT NULL DEFAULT 'local',
    local_seq           INTEGER NOT NULL DEFAULT 0,
    source_id           TEXT,
    source_version_seq  INTEGER,
    source_run_id       TEXT,
    source_batch_key    TEXT,
    source_origin       TEXT,
    import_id           TEXT,
    archive_sha256      TEXT,
    imported_at         TEXT
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seq (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_imports (
    import_id          TEXT PRIMARY KEY,
    archive_sha256     TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    source_version_seq INTEGER NOT NULL,
    source_run_id      TEXT NOT NULL,
    status             TEXT NOT NULL,
    run_id             TEXT,
    detail             TEXT,
    archive_json       TEXT,
    local_seq          INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_conflicts (
    conflict_id        TEXT PRIMARY KEY,
    source_id          TEXT NOT NULL,
    source_version_seq INTEGER NOT NULL,
    existing_run_id    TEXT,
    existing_sha256    TEXT,
    incoming_run_id    TEXT,
    incoming_sha256    TEXT NOT NULL,
    detail             TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
"""

# 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里逐列补，保住已有批次
RUN_COLUMNS = {
    "origin": "TEXT NOT NULL DEFAULT 'local'",
    "local_seq": "INTEGER NOT NULL DEFAULT 0",
    "source_id": "TEXT",
    "source_version_seq": "INTEGER",
    "source_run_id": "TEXT",
    "source_batch_key": "TEXT",
    "source_origin": "TEXT",
    "import_id": "TEXT",
    "archive_sha256": "TEXT",
    "imported_at": "TEXT",
}

TABLES = ("snapshots", "runs", "quarantine", "kv", "seq",
          "archive_imports", "archive_conflicts")


def db_path() -> Path:
    raw = os.environ.get("LINEAGEGUARD_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    # isolation_level=None：事务由调用方显式 BEGIN/COMMIT，序号分配才能原子
    conn = sqlite3.connect(db_path(), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：已有批次原样保留，origin 一律回填为 local。"""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, decl in RUN_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    if "local_seq" not in have:
        conn.execute("UPDATE runs SET local_seq=version_seq WHERE origin='local'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_import_identity"
        " ON runs(source_id, source_version_seq) WHERE origin = 'imported'"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_origin_seq ON runs(origin, version_seq)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_local_seq ON runs(local_seq)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_conflicts_identity"
        " ON archive_conflicts(source_id, source_version_seq)"
    )


def init_db() -> None:
    with _LOCK, _connect() as conn:
        # executescript 自带提交，建表后单独开事务补列与建索引
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        _migrate(conn)
        conn.commit()


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        _migrate(conn)
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def find_snapshot_by_digest(source_sha256: str) -> dict | None:
    """按源文件摘要找快照：归档里定位 base 快照用，不靠调用方猜 schema_id。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT payload_json FROM snapshots WHERE source_sha256=?
               ORDER BY created_at DESC, schema_id LIMIT 1""",
            (source_sha256 or "",),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

RUN_FIELDS = (
    "run_id", "batch_key", "old_schema_id", "new_schema_id", "version_seq",
    "input_sha256", "ruleset", "created_at", "effective_from", "effective_to",
)
RUN_PROVENANCE = (
    "origin", "local_seq", "source_id", "source_version_seq", "source_run_id",
    "source_batch_key", "source_origin", "import_id", "archive_sha256", "imported_at",
)


def _run_params(run: dict) -> tuple:
    values = [run[field] for field in RUN_FIELDS]
    values += [
        run.get("origin") or "local",
        int(run.get("local_seq") or 0),
        run.get("source_id"),
        run.get("source_version_seq"),
        run.get("source_run_id"),
        run.get("source_batch_key"),
        run.get("source_origin"),
        run.get("import_id"),
        run.get("archive_sha256"),
        run.get("imported_at"),
        _dumps(run.get("summary") or {}),
        _dumps(run),
    ]
    return tuple(values)


_RUN_COLUMNS = ",".join(RUN_FIELDS + RUN_PROVENANCE + ("summary_json", "payload_json"))
_RUN_PLACEHOLDERS = ",".join("?" * (len(RUN_FIELDS) + len(RUN_PROVENANCE) + 2))


def save_import(record: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO archive_imports (import_id, archive_sha256, source_id,
                   source_version_seq, source_run_id, status, run_id, detail,
                   archive_json, local_seq, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(import_id) DO UPDATE SET
                   archive_sha256=excluded.archive_sha256,
                   source_id=excluded.source_id,
                   source_version_seq=excluded.source_version_seq,
                   source_run_id=excluded.source_run_id,
                   status=CASE
                       WHEN archive_imports.status='applied'
                            AND excluded.status='duplicate'
                       THEN archive_imports.status
                       ELSE excluded.status END,
                   run_id=COALESCE(archive_imports.run_id, excluded.run_id),
                   detail=excluded.detail,
                   archive_json=COALESCE(archive_imports.archive_json,
                                         excluded.archive_json),
                   local_seq=COALESCE(archive_imports.local_seq,
                                      excluded.local_seq),
                   updated_at=excluded.updated_at""",
            (
                record["import_id"], record["archive_sha256"], record["source_id"],
                int(record["source_version_seq"]), record["source_run_id"],
                record["status"], record.get("run_id"), record.get("detail"),
                _dumps(record["archive"]) if record.get("archive") is not None else None,
                record.get("local_seq"),
                record["created_at"], record["updated_at"],
            ),
        )


def conflict_for_identity(source_id: str, source_version_seq: int) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT * FROM archive_conflicts
               WHERE source_id=? AND source_version_seq=?""",
            (source_id, int(source_version_seq)),
        ).fetchone()
    return dict(row) if row else None


def claim_import(record: dict) -> dict:
    """原子地占住一条导入记录，返回当前生效的记录。

    已经 applied/duplicate/conflict 的记录不会被后来的 prepared 覆盖；
    已预分配的顺序号保留，重放沿用同一个号。
    """
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM archive_imports WHERE import_id=?", (record["import_id"],)
            ).fetchone()
            if row is not None:
                current = dict(row)
                if current["status"] in ("applied", "duplicate", "conflict"):
                    conn.commit()
                    current["archive"] = _loads(current.pop("archive_json"))
                    return current
                conn.execute(
                    """UPDATE archive_imports SET archive_sha256=?, source_id=?,
                           source_version_seq=?, source_run_id=?, status=?,
                           local_seq=COALESCE(local_seq, ?), archive_json=?, updated_at=?
                       WHERE import_id=?""",
                    (
                        record["archive_sha256"], record["source_id"],
                        int(record["source_version_seq"]), record["source_run_id"],
                        "prepared", record.get("local_seq"),
                        _dumps(record.get("archive")) if record.get("archive") is not None
                        else None,
                        record["updated_at"], record["import_id"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO archive_imports (import_id, archive_sha256, source_id,
                           source_version_seq, source_run_id, status, run_id, detail,
                           archive_json, local_seq, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record["import_id"], record["archive_sha256"], record["source_id"],
                        int(record["source_version_seq"]), record["source_run_id"],
                        "prepared", record.get("run_id"), record.get("detail"),
                        _dumps(record.get("archive")) if record.get("archive") is not None
                        else None,
                        record.get("local_seq"), record["created_at"], record["updated_at"],
                    ),
                )
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM archive_imports WHERE import_id=?", (record["import_id"],)
        ).fetchone()
    result = dict(saved)
    result["archive"] = _loads(result.pop("archive_json"))
    return result


def import_by_identity(source_id: str, source_version_seq: int) -> dict | None:
    """按来源身份找导入记录：停在半路的同源同号也算，用来判冲突。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT * FROM archive_imports
               WHERE source_id=? AND source_version_seq=?
               ORDER BY created_at DESC, import_id LIMIT 1""",
            (source_id, int(source_version_seq)),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    record["archive"] = _loads(record.pop("archive_json"))
    return record


def import_by_id(import_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM archive_imports WHERE import_id=?", (import_id,)
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    record["archive"] = _loads(record.pop("archive_json"))
    return record


def list_pending_imports() -> list[dict]:
    """两阶段导入里停在 started/prepared 的记录：中断后能续上，也不会被读到。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT import_id, archive_sha256, source_id, source_version_seq,
                      source_run_id, status, run_id, detail, local_seq,
                      created_at, updated_at
               FROM archive_imports WHERE status IN ('started', 'prepared')
               ORDER BY created_at, import_id"""
        ).fetchall()
    return [dict(row) for row in rows]


def count_imports() -> dict:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM archive_imports GROUP BY status"
        ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def save_conflict(record: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO archive_conflicts (conflict_id, source_id, source_version_seq,
                   existing_run_id, existing_sha256, incoming_run_id, incoming_sha256,
                   detail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id, source_version_seq) DO UPDATE SET
                   existing_run_id=COALESCE(archive_conflicts.existing_run_id,
                                            excluded.existing_run_id),
                   existing_sha256=COALESCE(archive_conflicts.existing_sha256,
                                            excluded.existing_sha256),
                   incoming_run_id=excluded.incoming_run_id,
                   incoming_sha256=excluded.incoming_sha256,
                   detail=excluded.detail,
                   created_at=excluded.created_at""",
            (
                record["conflict_id"], record["source_id"],
                int(record["source_version_seq"]), record.get("existing_run_id"),
                record.get("existing_sha256"), record.get("incoming_run_id"),
                record["incoming_sha256"], record.get("detail"), record["created_at"],
            ),
        )


def list_conflicts() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM archive_conflicts ORDER BY created_at, conflict_id"
        ).fetchall()
    return [dict(row) for row in rows]


def count_conflicts() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM archive_conflicts").fetchone()
    return int(row["n"])


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def imported_run(source_id: str, source_version_seq: int) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT payload_json FROM runs
               WHERE origin='imported' AND source_id=? AND source_version_seq=?""",
            (source_id, int(source_version_seq)),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    """当前批次只认本地跑的：导入批次按来源单独读，绝不顶掉当前批次。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT payload_json FROM runs
               WHERE origin='local' ORDER BY version_seq DESC, run_id LIMIT 1"""
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_imported_runs(limit: int = 100) -> list[dict]:
    """导入批次按本地顺序倒排；历史回读只读这一批，不跟本地批次混在一起。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM runs
               WHERE origin='imported' ORDER BY local_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def list_runs(limit: int = 30) -> list[dict]:
    """历史按本地顺序倒排，来源身份一并带上，回读时看得清是哪来的。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json, origin, local_seq, source_id, source_version_seq,
                      source_run_id, imported_at
               FROM runs ORDER BY local_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        summary = _loads(row["summary_json"]) or {}
        summary.update({
            "origin": row["origin"],
            "local_seq": int(row["local_seq"] or 0),
            "source_id": row["source_id"],
            "source_version_seq": row["source_version_seq"],
            "source_run_id": row["source_run_id"],
            "imported_at": row["imported_at"],
        })
        out.append(summary)
    return out


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def count_local_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE origin='local'"
        ).fetchone()
    return int(row["n"])


def count_imported_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE origin='imported'"
        ).fetchone()
    return int(row["n"])


def _ensure_seq(conn: sqlite3.Connection, name: str) -> None:
    row = conn.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()
    if row is not None:
        return
    seed = 0
    if name == "local_seq":
        # 老库升级：已有序号接着用，绝不回退
        seed = int(conn.execute(
            "SELECT COALESCE(MAX(local_seq), 0) AS n FROM runs"
        ).fetchone()["n"])
    conn.execute("INSERT OR IGNORE INTO seq (name, value) VALUES (?, ?)", (name, seed))


def _next_seq(conn: sqlite3.Connection, name: str) -> int:
    """序号表自增：同一事务里取号，跑批与恢复共用一条线，顺序唯一。"""
    _ensure_seq(conn, name)
    conn.execute("UPDATE seq SET value = value + 1 WHERE name=?", (name,))
    row = conn.execute("SELECT value FROM seq WHERE name=?", (name,)).fetchone()
    return int(row["value"])


def allocate_local_seq() -> int:
    """本地顺序：跑批与导入共用一条单调线，中断也不会退回去。"""
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        seq = _next_seq(conn, "local_seq")
        conn.commit()
    return seq


def save_local_run(run: dict, summary_fn=None) -> bool:
    """本地跑批落库：顺序号与版本号在同一事务里取，并发也只有一个赢家。

    返回 False 表示同一批次键已被别的线程写入，调用方应回读那一份。
    """
    run["origin"] = "local"
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            run["local_seq"] = _next_seq(conn, "local_seq")
            row = conn.execute(
                "SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs WHERE origin='local'"
            ).fetchone()
            run["version_seq"] = int(row["n"]) + 1
            if summary_fn is not None:
                run["summary"] = summary_fn(run)
            conn.execute(
                f"INSERT INTO runs ({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})",
                _run_params(run),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        conn.commit()
    return True


def apply_import(run: dict, record: dict) -> bool:
    """恢复提交：分配本地顺序、写入批次、把导入记录标成 applied，同一事务。

    任何一步失败整笔回滚，读侧要么看到完整批次，要么什么都看不到；
    停在 prepared 的导入仍可重放，重放按来源身份幂等。
    """
    run["origin"] = "imported"
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 身份已被隔离成冲突时不再落库：先到的冲突结论不可被后到的覆盖
            if conn.execute(
                """SELECT 1 FROM archive_conflicts
                   WHERE source_id=? AND source_version_seq=?""",
                (run.get("source_id"), int(run.get("source_version_seq") or 0)),
            ).fetchone() is not None:
                conn.rollback()
                return False
            # 预分配过的顺序号直接沿用：重放不跳号、不重号；没分配才现取
            if not int(run.get("local_seq") or 0):
                run["local_seq"] = _next_seq(conn, "local_seq")
            conn.execute(
                f"INSERT INTO runs ({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})",
                _run_params(run),
            )
            conn.execute(
                """UPDATE archive_imports SET status=?, run_id=?, detail=?, local_seq=?,
                       updated_at=? WHERE import_id=?""",
                (record["status"], record.get("run_id"), record.get("detail"),
                 run["local_seq"], record["updated_at"], record["import_id"]),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        conn.commit()
    return True


def current_version_seq() -> int:
    """当前本地版本号：导入批次不推进它，基线校验仍按本地跑批算。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs WHERE origin='local'"
        ).fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT run_id, effective_from, effective_to FROM runs
               WHERE origin='local' ORDER BY version_seq"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_import_windows() -> list[dict]:
    """历史批次也参与重叠提示：只做提示，不改本地当前窗口。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT run_id, effective_from, effective_to FROM runs
               WHERE origin='imported' ORDER BY local_seq"""
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default
