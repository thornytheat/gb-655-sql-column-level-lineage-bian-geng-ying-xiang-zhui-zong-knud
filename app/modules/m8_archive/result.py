"""归档里的「影响结果」段：判定结论、影响面与引用解析。

恢复时直接用这一段重建批次载荷，不重跑判定，也不再读样例目录，
所以恢复出来的结论与源端逐字节一致。
"""

from __future__ import annotations


def build(run: dict) -> dict:
    impact = run.get("impact") or {}
    graph = impact.get("impact") or {}
    return {
        "counts": run.get("counts") or {},
        "buckets": run.get("buckets") or {},
        "verdicts": run.get("verdicts") or {},
        "changes": run.get("changes") or {},
        "name_clashes": run.get("name_clashes") or [],
        "diff": run.get("diff") or [],
        "type_stats": {
            "old": run.get("type_stats_old") or {},
            "new": run.get("type_stats_new") or {},
        },
        "resolution": run.get("resolution") or {},
        "impact": {
            "affected": graph.get("rows") or [],
            "total": int(graph.get("total") or 0),
            "depth_counts": graph.get("depth_counts") or {},
            "max_depth": int(graph.get("max_depth") or 0),
            "seed_fields": graph.get("seed_fields") or [],
            "changed_fields": int(graph.get("changed_fields") or 0),
            "node_count": int(graph.get("node_count") or 0),
            "edge_count": int(graph.get("edge_count") or 0),
            "cycles": (impact.get("cycles") or {}).get("cycles") or [],
            "self_loops": (impact.get("cycles") or {}).get("self_loops") or [],
            "one_sided": impact.get("one_sided") or [],
            "broken": impact.get("broken") or [],
            "summary": impact.get("summary") or {},
        },
    }


def rebuild_impact(result: dict) -> dict:
    """把归档里的影响结果段还原成运行载荷用法。"""
    block = result.get("impact") or {}
    rows = block.get("affected") or []
    return {
        "impact": {
            "rows": rows,
            "total": int(block.get("total") or len(rows)),
            "depth_counts": block.get("depth_counts") or {},
            "max_depth": int(block.get("max_depth") or 0),
            "seed_fields": block.get("seed_fields") or [],
            "changed_fields": int(block.get("changed_fields") or 0),
            "node_count": int(block.get("node_count") or 0),
            "edge_count": int(block.get("edge_count") or 0),
        },
        "cycles": {
            "cycles": block.get("cycles") or [],
            "self_loops": block.get("self_loops") or [],
            "cycle_count": len(block.get("cycles") or []),
            "self_loop_count": len(block.get("self_loops") or []),
        },
        "one_sided": block.get("one_sided") or [],
        "broken": block.get("broken") or [],
        "summary": block.get("summary") or {},
    }
