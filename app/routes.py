"""全部页面与导出路由。"""

import csv
import io
import json

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, url_for,
)

from . import audit, db, replay as replay_mod, services

bp = Blueprint("routes", __name__)


def _actor() -> str:
    return (request.form.get("actor") or "").strip()


# ---------------------------------------------------------------- 首页 / 探方

@bp.route("/")
def index():
    conn = db.get_db()
    grids = services.list_grids(conn)
    open_count = conn.execute(
        "SELECT COUNT(*) c FROM layers WHERE status='open'"
    ).fetchone()["c"]
    draft_count = conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"]
    recent = conn.execute(
        "SELECT * FROM audit_logs ORDER BY ts DESC, id DESC LIMIT 8"
    ).fetchall()
    return render_template(
        "index.html", grids=grids, open_count=open_count,
        draft_count=draft_count, recent=recent,
    )


@bp.post("/grids/create")
def create_grid():
    conn = db.get_db()
    result = services.create_grid(
        conn,
        request.form.get("code", ""),
        request.form.get("site", ""),
        request.form.get("note", ""),
        _actor(),
    )
    if result.ok:
        flash(f"探方已创建（id={result.id}）。", "success")
        return redirect(url_for("routes.grid_detail", grid_id=result.id))
    flash(result.error, "error")
    return redirect(url_for("routes.index"))


@bp.route("/grids/<int:grid_id>")
def grid_detail(grid_id: int):
    conn = db.get_db()
    grid = services.get_grid(conn, grid_id)
    if grid is None:
        flash("探方不存在。", "error")
        return redirect(url_for("routes.index"))

    layers = services.list_layers(conn, grid_id)
    relations = services.list_relations(conn, grid_id)
    batches = services.list_batches_for_grid(conn, grid_id)

    from .graph import build_graph

    graph = build_graph([dict(l) for l in layers], [dict(r) for r in relations])
    open_layers = [l for l in layers if l["status"] == "open"]

    return render_template(
        "grid_detail.html",
        grid=grid,
        layers=layers,
        relations=relations,
        batches=batches,
        graph=graph,
        open_layers=open_layers,
        artifact_categories=["陶片", "石器", "骨器", "金属器", "植物遗存", "其他"],
    )


# ---------------------------------------------------------------- 层位

@bp.post("/grids/<int:grid_id>/layers")
def create_layer(grid_id: int):
    conn = db.get_db()
    result = services.create_layer(
        conn, grid_id,
        request.form.get("code", ""),
        request.form.get("seq", ""),
        request.form.get("name", ""),
        request.form.get("period", ""),
        request.form.get("description", ""),
        _actor(),
    )
    if result.ok:
        flash(f"层位已开放（id={result.id}）。", "success")
    else:
        flash(result.error, "error")
    return redirect(url_for("routes.grid_detail", grid_id=grid_id))


@bp.post("/layers/<int:layer_id>/close")
def close_layer(layer_id: int):
    conn = db.get_db()
    result = services.close_layer(conn, layer_id, _actor())
    layer = services.get_layer(conn, layer_id)
    if result.ok:
        flash("层位已关闭，此后禁止补录遗物。", "success")
    else:
        flash(result.error, "error")
    return redirect(
        url_for("routes.grid_detail", grid_id=layer["grid_id"]) if layer
        else url_for("routes.index")
    )


# ---------------------------------------------------------------- 层位顺序整体调整

def _parse_reorder_form() -> dict:
    """从表单收集 {layer_id: 新顺序号}（仅取 seq_<id> 字段，值留空则忽略）。"""
    new_order: dict[int, str] = {}
    for key, value in request.form.items():
        if key.startswith("seq_"):
            try:
                new_order[int(key[4:])] = value
            except ValueError:
                continue
    return new_order


@bp.post("/grids/<int:grid_id>/reorder/preview")
def reorder_preview(grid_id):
    conn = db.get_db()
    result = services.preview_reorder(conn, grid_id, _parse_reorder_form(), _actor())
    if not result.ok:
        flash(result.error, "error")
        return redirect(url_for("routes.grid_detail", grid_id=grid_id))
    conflicts = result.data.get("conflicts", [])
    if conflicts:
        flash(
            f"预览已生成：存在 {len(conflicts)} 处覆盖冲突，禁止提交，请调整后重新预览。",
            "error",
        )
    else:
        flash("预览已生成：确认无误后凭此预览一次性提交。", "success")
    return redirect(
        url_for(
            "routes.reorder_preview_page",
            grid_id=grid_id,
            preview_id=result.id,
            token=result.data["token"],
        )
    )


@bp.route("/grids/<int:grid_id>/reorder/<int:preview_id>")
def reorder_preview_page(grid_id, preview_id):
    conn = db.get_db()
    token = request.args.get("token", "")
    preview = services.get_reorder_preview(conn, preview_id, token)
    if preview is None or preview["grid_id"] != grid_id:
        flash("预览不存在、链接不完整或已被提交/取代，请重新发起调整。", "error")
        return redirect(url_for("routes.grid_detail", grid_id=grid_id))

    grid = services.get_grid(conn, grid_id)
    try:
        payload = json.loads(preview["payload"])
    except (ValueError, TypeError):
        payload = {}
    changes = payload.get("changes", [])
    relations = payload.get("relations", [])
    conflicts = [r for r in relations if r.get("conflict")]
    stale = preview["fingerprint"] != services.grid_fingerprint(conn, grid_id)

    return render_template(
        "reorder_preview.html",
        grid=grid,
        preview=preview,
        changes=changes,
        relations=relations,
        conflicts=conflicts,
        stale=stale,
    )


@bp.post("/grids/<int:grid_id>/reorder/<int:preview_id>/commit")
def reorder_commit(grid_id, preview_id):
    conn = db.get_db()
    token = request.form.get("token", "")
    result = services.commit_reorder(conn, preview_id, token, _actor())
    if result.ok:
        changed = result.data.get("changed_count", 0)
        flash(f"发掘顺序已整体生效（{changed} 个层位顺序变更，一次性原子提交）。", "success")
        return redirect(url_for("routes.grid_detail", grid_id=grid_id))
    flash(result.error, "error")
    if services.get_reorder_preview(conn, preview_id, token) is not None:
        return redirect(
            url_for(
                "routes.reorder_preview_page",
                grid_id=grid_id, preview_id=preview_id, token=token,
            )
        )
    return redirect(url_for("routes.grid_detail", grid_id=grid_id))


# ---------------------------------------------------------------- 覆盖关系

@bp.post("/grids/<int:grid_id>/relations")
def add_relation(grid_id: int):
    conn = db.get_db()
    result = services.add_relation(
        conn, grid_id,
        request.form.get("upper_id", type=int),
        request.form.get("lower_id", type=int),
        request.form.get("note", ""),
        _actor(),
    )
    if result.ok:
        if result.data.get("duplicated"):
            flash("该覆盖关系已存在，无需重复登记。", "info")
        else:
            flash("覆盖关系已登记，层位图已更新。", "success")
    else:
        msg = f"发现矛盾，录入已作为草稿保留。冲突路径：{result.conflict_path}"
        flash(msg, "error")
        return redirect(url_for("routes.conflicts", highlight=result.draft_id))
    return redirect(url_for("routes.grid_detail", grid_id=grid_id))


# ---------------------------------------------------------------- 遗物批次

@bp.post("/artifacts")
def register_artifact():
    conn = db.get_db()
    layer_id = request.form.get("layer_id", type=int)
    result = services.register_artifact(
        conn, layer_id,
        request.form.get("category", ""),
        request.form.get("count", ""),
        request.form.get("description", ""),
        _actor(),
    )
    layer = services.get_layer(conn, layer_id) if layer_id else None
    if result.ok:
        flash(f"遗物批次 {result.data.get('batch_no')} 已登记到开放层位。", "success")
    else:
        flash(result.error, "error")
    if layer:
        return redirect(url_for("routes.grid_detail", grid_id=layer["grid_id"]))
    return redirect(url_for("routes.index"))


# ---------------------------------------------------------------- 冲突草稿

@bp.route("/conflicts")
def conflicts():
    conn = db.get_db()
    drafts = services.list_drafts(conn)
    layers_by_grid: dict = {}
    for draft in drafts:
        gid = draft["grid_id"]
        if gid and gid not in layers_by_grid:
            layers_by_grid[gid] = services.list_layers(conn, gid)
    highlight = request.args.get("highlight", type=int)
    return render_template(
        "conflicts.html", drafts=drafts,
        layers_by_grid=layers_by_grid, highlight=highlight,
    )


@bp.post("/drafts/<int:draft_id>/retry")
def retry_draft(draft_id: int):
    conn = db.get_db()
    result = services.retry_draft(conn, draft_id, _actor())
    if result.ok:
        flash(f"草稿 #{draft_id} 重试成功，冲突已解除。", "success")
        draft = None
        return redirect(url_for("routes.conflicts"))
    flash(
        f"草稿 #{draft_id} 仍存在矛盾：{result.error} 冲突路径：{result.conflict_path}",
        "error",
    )
    return redirect(url_for("routes.conflicts"))


@bp.post("/drafts/<int:draft_id>/discard")
def discard_draft(draft_id: int):
    conn = db.get_db()
    services.discard_draft(conn, draft_id, _actor())
    flash(f"草稿 #{draft_id} 已丢弃。", "info")
    return redirect(url_for("routes.conflicts"))


# ---------------------------------------------------------------- 回放

@bp.route("/grids/<int:grid_id>/replay")
def replay(grid_id: int):
    conn = db.get_db()
    state = replay_mod.replay_grid(conn, grid_id)
    if state["grid"] is None:
        flash("探方不存在。", "error")
        return redirect(url_for("routes.index"))

    total = state["total"]
    step = request.args.get("step", default=total, type=int)
    if total == 0:
        step = 0
    else:
        step = max(1, min(step, total))

    events = state["events"]
    current = events[step - 1] if step > 0 else None
    return render_template(
        "replay.html",
        grid=state["grid"],
        events=events,
        total=total,
        step=step,
        current=current,
    )


# ---------------------------------------------------------------- 审计

def _audit_query(conn, grid_id, action, only_errors, q):
    sql = "SELECT a.*, g.code AS grid_code FROM audit_logs a LEFT JOIN grids g ON g.id=a.grid_id WHERE 1=1"
    params: list = []
    if grid_id:
        sql += " AND a.grid_id = ?"
        params.append(grid_id)
    if action:
        sql += " AND a.action = ?"
        params.append(action)
    if only_errors:
        sql += " AND a.success = 0"
    if q:
        sql += " AND (a.summary LIKE ? OR a.details LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY a.ts DESC, a.id DESC"
    rows = conn.execute(sql, params).fetchall()
    parsed = []
    for row in rows:
        item = dict(row)
        try:
            item["details_obj"] = json.loads(row["details"])
        except (ValueError, TypeError):
            item["details_obj"] = {}
        parsed.append(item)
    return parsed


@bp.route("/audit")
def audit_view():
    conn = db.get_db()
    grid_id = request.args.get("grid_id", type=int)
    action = request.args.get("action", "")
    only_errors = request.args.get("only_errors") == "1"
    q = (request.args.get("q") or "").strip()
    logs = _audit_query(conn, grid_id, action, only_errors, q)
    grids = services.list_grids(conn)
    actions = [
        ("create", "新建探方"),
        ("open_layer", "开放层位"),
        ("close_layer", "关闭层位"),
        ("add_relation", "登记/拒绝关系"),
        ("register_artifact", "登记/拒绝遗物"),
        ("save_draft", "保留草稿"),
        ("resolve_draft", "草稿重试"),
        ("discard_draft", "丢弃草稿"),
        ("reorder_layers", "整体调整顺序"),
    ]
    return render_template(
        "audit.html", logs=logs, grids=grids, actions=actions,
        f_grid_id=grid_id, f_action=action, f_only_errors=only_errors, f_q=q,
    )


@bp.route("/audit/export")
def audit_export():
    conn = db.get_db()
    grid_id = request.args.get("grid_id", type=int)
    action = request.args.get("action", "")
    only_errors = request.args.get("only_errors") == "1"
    q = (request.args.get("q") or "").strip()
    logs = _audit_query(conn, grid_id, action, only_errors, q)

    buf = io.StringIO()
    buf.write("﻿")  # BOM，便于 Excel 正确识别 UTF-8
    writer = csv.writer(buf)
    writer.writerow(
        ["ID", "时间(UTC)", "操作", "对象", "对象ID", "探方编号", "操作人",
         "是否成功", "摘要", "详情JSON"]
    )
    for row in logs:
        writer.writerow([
            row["id"], row["ts"], row["action"], row["entity"], row["entity_id"],
            row["grid_code"] or "", row["actor"],
            "成功" if row["success"] else "拒绝/冲突",
            row["summary"], row["details"],
        ])

    filename = "audit_records.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
