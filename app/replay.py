"""按探方回放层位演变：以审计日志为唯一事实源，逐条重放重建图状态。

每个事件携带重放后的累计快照（开放层位、已关闭层位、覆盖边）。
冲突/被拒绝的事件保留在时间线上但不改变状态。
"""

import json

from .graph import build_graph

ACTION_LABELS = {
    "create": "新建探方",
    "open_layer": "开放层位",
    "close_layer": "关闭层位",
    "reorder_layers": "整体调整发掘顺序",
    "add_relation": "登记覆盖关系",
    "register_artifact": "登记遗物批次",
    "save_draft": "保留冲突草稿",
    "resolve_draft": "草稿重试成功",
    "discard_draft": "丢弃草稿",
}


def _layer_label(code_by_id, layer_id):
    return code_by_id.get(layer_id, f"#{layer_id}")


def replay_grid(conn, grid_id: int) -> dict:
    grid = conn.execute("SELECT * FROM grids WHERE id = ?", (grid_id,)).fetchone()
    logs = conn.execute(
        "SELECT * FROM audit_logs WHERE grid_id = ? ORDER BY ts, id", (grid_id,)
    ).fetchall()

    open_layers: dict[int, dict] = {}
    closed_layers: dict[int, dict] = {}
    edges: dict[int, tuple[int, int]] = {}     # relation_id -> (upper_id, lower_id)
    code_by_id: dict[int, str] = {}
    seq_by_id: dict[int, int] = {}
    name_by_id: dict[int, str] = {}

    events = []
    for row in logs:
        try:
            details = json.loads(row["details"])
        except (ValueError, TypeError):
            details = {}

        title = ACTION_LABELS.get(row["action"], row["action"])
        conflict = False
        kind = "info"

        if row["action"] == "open_layer":
            lid = details.get("layer_id")
            if lid is not None:
                open_layers[lid] = {"id": lid, "seq": details.get("seq")}
                code_by_id[lid] = details.get("code", f"#{lid}")
                seq_by_id[lid] = details.get("seq")
                name_by_id[lid] = ""
            title = f"开放层位 {details.get('code', '')}（顺序 {details.get('seq', '?')}）"
            kind = "open"
        elif row["action"] == "close_layer":
            lid = details.get("layer_id")
            open_layers.pop(lid, None)
            if lid is not None:
                closed_layers[lid] = {"id": lid, "seq": seq_by_id.get(lid)}
            title = f"关闭层位 {details.get('code', '')}（此后禁止补录遗物）"
            kind = "close"
        elif row["action"] == "reorder_layers":
            # 一次性整体改序：该事件之后的帧一律显示新顺序
            layer_changes = details.get("changes") or []
            for ch in layer_changes:
                lid = ch.get("layer_id")
                new_seq = ch.get("new_seq")
                if lid is not None and new_seq is not None:
                    seq_by_id[lid] = new_seq
                    if lid in open_layers:
                        open_layers[lid]["seq"] = new_seq
                    if lid in closed_layers:
                        closed_layers[lid]["seq"] = new_seq
            if row["success"]:
                desc = "，".join(
                    f"{c.get('code')}:{c.get('old_seq')}→{c.get('new_seq')}"
                    for c in layer_changes
                )
                title = f"整体调整发掘顺序：{desc}" if desc else "整体调整发掘顺序（无实际变化）"
                kind = "reorder"
            else:
                conflict = True
                title = "拒绝顺序调整：" + row["summary"]
                kind = "conflict"
        elif row["action"] == "add_relation":
            uid, lid = details.get("upper_id"), details.get("lower_id")
            rel_id = details.get("relation_id")
            if row["success"] and rel_id is not None and uid is not None and lid is not None:
                edges[rel_id] = (uid, lid)
                title = f"登记覆盖关系 {_layer_label(code_by_id, uid)} → {_layer_label(code_by_id, lid)}"
                kind = "edge"
            else:
                conflict = True
                title = f"拒绝关系 {_layer_label(code_by_id, uid)} → {_layer_label(code_by_id, lid)}"
                kind = "conflict"
        elif row["action"] == "register_artifact":
            if row["success"]:
                title = row["summary"]
                kind = "artifact"
            else:
                conflict = True
                title = "拒绝补录：" + row["summary"]
                kind = "conflict"
        elif row["action"] == "save_draft":
            title = "保留冲突草稿：" + details.get("conflict_path", "")
            kind = "draft"
        elif row["action"] == "create":
            title = row["summary"]
        else:
            title = row["summary"] or title

        # 构造该时刻快照
        all_ids = set(open_layers) | set(closed_layers)
        layer_rows = [
            {
                "id": lid,
                "code": code_by_id.get(lid, f"#{lid}"),
                "seq": seq_by_id.get(lid, 0) or 0,
                "name": name_by_id.get(lid, ""),
                "status": "closed" if lid in closed_layers else "open",
            }
            for lid in all_ids
        ]
        rel_rows = [
            {"id": rid, "upper_id": pair[0], "lower_id": pair[1], "note": ""}
            for rid, pair in edges.items()
        ]
        graph = build_graph(layer_rows, rel_rows)

        events.append(
            {
                "log_id": row["id"],
                "ts": row["ts"],
                "action": row["action"],
                "label": ACTION_LABELS.get(row["action"], row["action"]),
                "title": title,
                "actor": row["actor"],
                "success": bool(row["success"]),
                "conflict": conflict,
                "kind": kind,
                "conflict_path": details.get("conflict_path", ""),
                "reason": details.get("reason", ""),
                "graph": graph,
                "open_count": len(open_layers),
                "closed_count": len(closed_layers),
                "edge_count": len(edges),
            }
        )

    return {"grid": grid, "events": events, "total": len(events)}
