"""核心业务逻辑：探方、层位、覆盖关系（无环 + 发掘顺序）、遗物批次、冲突草稿。

所有写操作都写入 audit_logs。关系录入出现矛盾时不报错退出，而是保留草稿并返回
冲突路径；遗物登记到已关闭层位直接拒绝（不补录）。
"""

import json
from collections import deque
from dataclasses import dataclass, field

from . import audit


@dataclass
class Result:
    ok: bool
    id: int | None = None
    error: str = ""
    conflict_path: str = ""
    draft_id: int | None = None
    data: dict = field(default_factory=dict)

    @classmethod
    def success(cls, id_: int | None = None, **data) -> "Result":
        return cls(True, id=id_, data=data)

    @classmethod
    def failure(cls, error: str, conflict_path: str = "", draft_id: int | None = None) -> "Result":
        return cls(False, error=error, conflict_path=conflict_path, draft_id=draft_id)


# ---------------------------------------------------------------- 基础查询

def get_grid(conn, grid_id: int):
    return conn.execute("SELECT * FROM grids WHERE id = ?", (grid_id,)).fetchone()


def get_layer(conn, layer_id: int):
    return conn.execute("SELECT * FROM layers WHERE id = ?", (layer_id,)).fetchone()


def list_grids(conn):
    return conn.execute(
        """SELECT g.*,
                  COUNT(DISTINCT l.id) AS layer_count,
                  COUNT(DISTINCT b.id) AS batch_count
             FROM grids g
             LEFT JOIN layers l ON l.grid_id = g.id
             LEFT JOIN artifact_batches b ON b.layer_id = l.id
            GROUP BY g.id
            ORDER BY g.code"""
    ).fetchall()


def list_layers(conn, grid_id: int, status: str | None = None):
    sql = "SELECT * FROM layers WHERE grid_id = ?"
    params: list = [grid_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY seq, code"
    return conn.execute(sql, params).fetchall()


def list_relations(conn, grid_id: int):
    return conn.execute(
        """SELECT r.*, u.code AS upper_code, l.code AS lower_code,
                  u.seq  AS upper_seq,  l.seq AS lower_seq
             FROM layer_relations r
             JOIN layers u ON u.id = r.upper_id
             JOIN layers l ON l.id = r.lower_id
            WHERE r.grid_id = ?
            ORDER BY r.created_at, r.id""",
        (grid_id,),
    ).fetchall()


# ---------------------------------------------------------------- 探方 / 层位

def create_grid(conn, code: str, site: str, note: str, actor: str = "") -> Result:
    code = (code or "").strip()
    if not code:
        return Result.failure("探方编号不能为空")
    if conn.execute("SELECT 1 FROM grids WHERE code = ?", (code,)).fetchone():
        return Result.failure(f"探方编号 {code} 已存在")

    ts = audit.now_ts()
    cur = conn.execute(
        "INSERT INTO grids (code, site, note, created_at, updated_at) VALUES (?,?,?,?,?)",
        (code, site.strip(), note.strip(), ts, ts),
    )
    grid_id = cur.lastrowid
    audit.log(
        conn, "create", "grid", grid_id, grid_id, actor, True,
        f"新建探方 {code}", {"code": code, "site": site}, commit=False,
    )
    conn.commit()
    return Result.success(grid_id)


def create_layer(
    conn, grid_id: int, code: str, seq: int, name: str,
    period: str, description: str, actor: str = "",
) -> Result:
    grid = get_grid(conn, grid_id)
    if grid is None:
        return Result.failure("探方不存在")
    code = (code or "").strip()
    if not code:
        return Result.failure("层位编号不能为空")
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        return Result.failure("发掘顺序必须是整数")
    if seq <= 0:
        return Result.failure("发掘顺序必须为正整数（先发掘/上层数值小）")
    if conn.execute(
        "SELECT 1 FROM layers WHERE grid_id = ? AND code = ?", (grid_id, code)
    ).fetchone():
        return Result.failure(f"该探方中层位 {code} 已存在")
    if conn.execute(
        "SELECT 1 FROM layers WHERE grid_id = ? AND seq = ?", (grid_id, seq)
    ).fetchone():
        return Result.failure(f"发掘顺序 {seq} 已被本探方其他层位占用")

    ts = audit.now_ts()
    cur = conn.execute(
        """INSERT INTO layers
               (grid_id, code, seq, name, period, description, status, opened_at, created_by)
           VALUES (?,?,?,?,?,?,'open',?,?)""",
        (grid_id, code, seq, name.strip(), period.strip(), description.strip(), ts, actor.strip()),
    )
    layer_id = cur.lastrowid
    conn.execute("UPDATE grids SET updated_at = ? WHERE id = ?", (ts, grid_id))
    audit.log(
        conn, "open_layer", "layer", layer_id, grid_id, actor, True,
        f"探方 {grid['code']} 开放层位 {code}（发掘顺序 {seq}）",
        {"layer_id": layer_id, "code": code, "seq": seq}, commit=False,
    )
    conn.commit()
    return Result.success(layer_id)


def close_layer(conn, layer_id: int, actor: str = "") -> Result:
    layer = get_layer(conn, layer_id)
    if layer is None:
        return Result.failure("层位不存在")
    if layer["status"] == "closed":
        return Result.failure(f"层位 {layer['code']} 已关闭，无需重复操作")

    ts = audit.now_ts()
    conn.execute(
        "UPDATE layers SET status = 'closed', closed_at = ? WHERE id = ?",
        (ts, layer_id),
    )
    conn.execute("UPDATE grids SET updated_at = ? WHERE id = ?", (ts, layer["grid_id"]))
    audit.log(
        conn, "close_layer", "layer", layer_id, layer["grid_id"], actor, True,
        f"关闭层位 {layer['code']}（关闭后禁止补录遗物）",
        {"layer_id": layer_id, "code": layer["code"]}, commit=False,
    )
    conn.commit()
    return Result.success(layer_id)


# ---------------------------------------------------------------- 覆盖关系

def _find_path(conn, grid_id: int, start_id: int, target_id: int) -> list[int]:
    """在 upper->lower 有向图中，BFS 求 start_id 到 target_id 的层位 id 路径；不可达返回 []。"""
    adjacency: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT upper_id, lower_id FROM layer_relations WHERE grid_id = ?", (grid_id,)
    ):
        adjacency.setdefault(row["upper_id"], []).append(row["lower_id"])

    queue = deque([[start_id]])
    seen = {start_id}
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == target_id:
            return path
        for nxt in adjacency.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return []


def _layer_code_map(conn, ids) -> dict[int, str]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        row["id"]: row["code"]
        for row in conn.execute(
            f"SELECT id, code FROM layers WHERE id IN ({placeholders})", tuple(ids)
        )
    }


def _save_relation_draft(conn, grid_id, upper_id, lower_id, note, actor, reason, conflict_path) -> int:
    ts = audit.now_ts()
    cur = conn.execute(
        """INSERT INTO drafts (grid_id, kind, payload, reason, conflict_path, created_at)
           VALUES (?, 'relation', ?, ?, ?, ?)""",
        (
            grid_id,
            json.dumps(
                {"upper_id": upper_id, "lower_id": lower_id, "note": note, "actor": actor},
                ensure_ascii=False,
            ),
            reason,
            conflict_path,
            ts,
        ),
    )
    return cur.lastrowid


def add_relation(
    conn, grid_id: int, upper_id: int, lower_id: int,
    note: str = "", actor: str = "", save_draft: bool = True,
) -> Result:
    """登记 “upper 覆盖 lower” 关系。

    约束：两层必须属于同一探方；upper.seq 必须严格小于 lower.seq；不得成环。
    违反后两条时保留草稿、返回冲突路径。
    """
    grid = get_grid(conn, grid_id)
    if grid is None:
        return Result.failure("探方不存在")
    upper = get_layer(conn, upper_id) if upper_id else None
    lower = get_layer(conn, lower_id) if lower_id else None
    if upper is None or lower is None:
        return Result.failure("请选择两个有效的层位")
    if upper["grid_id"] != grid_id or lower["grid_id"] != grid_id:
        return Result.failure("两个层位必须属于同一探方")
    if upper_id == lower_id:
        return Result.failure("不能建立层位到自身的覆盖关系")

    if conn.execute(
        "SELECT id FROM layer_relations WHERE upper_id = ? AND lower_id = ?",
        (upper_id, lower_id),
    ).fetchone():
        existing = conn.execute(
            "SELECT id FROM layer_relations WHERE upper_id = ? AND lower_id = ?",
            (upper_id, lower_id),
        ).fetchone()
        return Result.success(existing["id"], duplicated=True)

    # 1) 发掘顺序：先发掘的（seq 小）覆盖后发掘的（seq 大）
    if upper["seq"] >= lower["seq"]:
        reason = (
            f"违反发掘顺序：{upper['code']} 的发掘顺序为 {upper['seq']}，"
            f"{lower['code']} 为 {lower['seq']}；覆盖方必须先发掘（顺序号更小）。"
        )
        conflict_path = (
            f"{upper['code']}(seq={upper['seq']}) ✗ {lower['code']}(seq={lower['seq']})"
        )
        audit.log(
            conn, "add_relation", "relation", None, grid_id, actor, False,
            f"关系 {upper['code']}→{lower['code']} 被拒绝：{reason}",
            {"upper_id": upper_id, "lower_id": lower_id, "reason": reason}, commit=False,
        )
        draft_id = None
        if save_draft:
            draft_id = _save_relation_draft(
                conn, grid_id, upper_id, lower_id, note, actor, reason, conflict_path
            )
            audit.log(
                conn, "save_draft", "draft", draft_id, grid_id, actor, True,
                f"关系草稿已保留（冲突路径：{conflict_path}）",
                {"draft_id": draft_id, "conflict_path": conflict_path}, commit=False,
            )
        conn.commit()
        return Result.failure(reason, conflict_path, draft_id)

    # 2) 无环检查：若已存在 lower → … → upper 路径，再加入 upper → lower 即成环
    cycle = _find_path(conn, grid_id, lower_id, upper_id)
    if cycle:
        cycle.append(upper_id)  # 闭合：lower → … → upper → lower(隐含)
        codes = _layer_code_map(conn, cycle)
        path_labels = [codes[i] for i in cycle]
        conflict_path = " → ".join(path_labels) + f" → {lower['code']}（成环）"
        reason = f"新增关系 {upper['code']}→{lower['code']} 将使层位图成环：{conflict_path}"
        audit.log(
            conn, "add_relation", "relation", None, grid_id, actor, False,
            f"关系 {upper['code']}→{lower['code']} 被拒绝：检测到环",
            {"upper_id": upper_id, "lower_id": lower_id,
             "conflict_path": conflict_path, "cycle_ids": cycle},
            commit=False,
        )
        draft_id = None
        if save_draft:
            draft_id = _save_relation_draft(
                conn, grid_id, upper_id, lower_id, note, actor, reason, conflict_path
            )
            audit.log(
                conn, "save_draft", "draft", draft_id, grid_id, actor, True,
                f"关系草稿已保留（冲突路径：{conflict_path}）",
                {"draft_id": draft_id, "conflict_path": conflict_path}, commit=False,
            )
        conn.commit()
        return Result.failure(reason, conflict_path, draft_id)

    ts = audit.now_ts()
    cur = conn.execute(
        "INSERT INTO layer_relations (grid_id, upper_id, lower_id, note, created_at) VALUES (?,?,?,?,?)",
        (grid_id, upper_id, lower_id, note.strip(), ts),
    )
    rel_id = cur.lastrowid
    audit.log(
        conn, "add_relation", "relation", rel_id, grid_id, actor, True,
        f"建立覆盖关系 {upper['code']} → {lower['code']}",
        {"relation_id": rel_id, "upper_id": upper_id, "lower_id": lower_id}, commit=False,
    )
    conn.commit()
    return Result.success(rel_id)


# ---------------------------------------------------------------- 遗物批次

def register_artifact(
    conn, layer_id: int, category: str, count, description: str, actor: str = "",
) -> Result:
    layer = get_layer(conn, layer_id)
    if layer is None:
        return Result.failure("层位不存在")
    category = (category or "").strip()
    if not category:
        return Result.failure("遗物类别不能为空")
    try:
        count = int(count)
    except (TypeError, ValueError):
        return Result.failure("数量必须是非负整数")
    if count < 0:
        return Result.failure("数量不能为负")

    if layer["status"] != "open":
        grid = get_grid(conn, layer["grid_id"])
        reason = (
            f"层位 {layer['code']} 已于 {layer['closed_at']} 关闭，"
            f"遗物只能归属于当前开放层位，禁止补录。"
        )
        audit.log(
            conn, "register_artifact", "artifact_batch", None, layer["grid_id"],
            actor, False, f"拒绝向已关闭层位 {layer['code']} 补录遗物（{category}×{count}）",
            {"layer_id": layer_id, "layer_code": layer["code"],
             "grid_code": grid["code"] if grid else None,
             "category": category, "count": count},
        )
        return Result.failure(reason)

    ts = audit.now_ts()
    compact = ts.replace("-", "").replace(":", "").replace("+00:00", "").replace("T", "")[:14]
    grid = get_grid(conn, layer["grid_id"])
    batch_no = f"B{grid['code']}-{layer['code']}-{compact}"
    cur = conn.execute(
        """INSERT INTO artifact_batches
               (layer_id, batch_no, category, count, description, recorded_by, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (layer_id, batch_no, category, count, description.strip(), actor.strip(), ts),
    )
    batch_id = cur.lastrowid
    audit.log(
        conn, "register_artifact", "artifact_batch", batch_id, layer["grid_id"], actor, True,
        f"层位 {layer['code']} 登记遗物批次 {batch_no}：{category}×{count}",
        {"batch_id": batch_id, "batch_no": batch_no, "layer_id": layer_id,
         "layer_code": layer["code"], "category": category, "count": count},
    )
    return Result.success(batch_id, batch_no=batch_no)


def list_batches_for_grid(conn, grid_id: int):
    return conn.execute(
        """SELECT b.*, l.code AS layer_code, l.status AS layer_status, l.seq AS layer_seq
             FROM artifact_batches b
             JOIN layers l ON l.id = b.layer_id
            WHERE l.grid_id = ?
            ORDER BY b.created_at, b.id""",
        (grid_id,),
    ).fetchall()


# ---------------------------------------------------------------- 草稿

def list_drafts(conn):
    rows = conn.execute(
        """SELECT d.*, g.code AS grid_code
             FROM drafts d LEFT JOIN grids g ON g.id = d.grid_id
            ORDER BY d.created_at DESC, d.id DESC"""
    ).fetchall()
    drafts = []
    for row in rows:
        item = dict(row)
        try:
            item["payload_obj"] = json.loads(row["payload"])
        except (ValueError, TypeError):
            item["payload_obj"] = {}
        drafts.append(item)
    return drafts


def get_draft(conn, draft_id: int):
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def retry_draft(conn, draft_id: int, actor: str = "") -> Result:
    """重新提交草稿：按草稿内容再次走校验；成功则删除草稿。"""
    draft = get_draft(conn, draft_id)
    if draft is None:
        return Result.failure("草稿不存在")
    if draft["kind"] != "relation":
        return Result.failure("暂只支持关系类草稿重试")
    payload = json.loads(draft["payload"])
    result = add_relation(
        conn,
        draft["grid_id"],
        payload.get("upper_id"),
        payload.get("lower_id"),
        payload.get("note", ""),
        actor or payload.get("actor", ""),
        save_draft=False,
    )
    if result.ok:
        conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        audit.log(
            conn, "resolve_draft", "draft", draft_id, draft["grid_id"], actor, True,
            f"草稿 #{draft_id} 重试成功并已清除，建立关系 id={result.id}",
            {"draft_id": draft_id, "relation_id": result.id},
        )
    return result


def discard_draft(conn, draft_id: int, actor: str = "") -> Result:
    draft = get_draft(conn, draft_id)
    if draft is None:
        return Result.failure("草稿不存在")
    conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
    audit.log(
        conn, "discard_draft", "draft", draft_id, draft["grid_id"], actor, True,
        f"丢弃草稿 #{draft_id}", {"draft_id": draft_id},
    )
    return Result.success(draft_id)
