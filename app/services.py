"""核心业务逻辑：探方、层位、覆盖关系（无环 + 发掘顺序）、遗物批次、冲突草稿。

所有写操作都写入 audit_logs。关系录入出现矛盾时不报错退出，而是保留草稿并返回
冲突路径；遗物登记到已关闭层位直接拒绝（不补录）。
"""

import hashlib
import json
import secrets
import sqlite3
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


# ---------------------------------------------------------------- 层位发掘顺序整体调整


def _relations_as_pairs(conn, grid_id: int) -> list[tuple[int, int]]:
    return [
        (r["upper_id"], r["lower_id"])
        for r in conn.execute(
            "SELECT upper_id, lower_id FROM layer_relations WHERE grid_id = ? ORDER BY id",
            (grid_id,),
        )
    ]


def grid_fingerprint(conn, grid_id: int) -> str:
    """探方“层位 + 覆盖关系”状态指纹。

    层位（id/code/seq/status）或覆盖关系集合发生任何变化，指纹都会不同，
    用于判定顺序调整预览是否已过期。
    """
    layer_part = [
        (r["id"], r["code"], r["seq"], r["status"])
        for r in conn.execute(
            "SELECT id, code, seq, status FROM layers WHERE grid_id = ? ORDER BY id",
            (grid_id,),
        )
    ]
    rel_part = _relations_as_pairs(conn, grid_id)
    raw = json.dumps({"layers": layer_part, "relations": rel_part}, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_new_order(conn, grid_id: int, new_order: dict) -> Result:
    """校验用户输入的 {layer_id: 新顺序号}：

    键必须恰好覆盖现存层位（不多不少）；值必须为互不重复的正整数。
    成功时返回规整后的 {layer_id: new_seq:int}。
    """
    try:
        raw = {int(k): v for k, v in dict(new_order).items()}
    except (TypeError, ValueError):
        return Result.failure("层位编号无法识别，请重新预览")

    existing = list_layers(conn, grid_id)
    ids = {row["id"] for row in existing}
    submitted = set(raw)
    if submitted != ids:
        missing = sorted(
            row["code"] for row in existing if row["id"] in ids - submitted
        )
        if missing:
            return Result.failure("编号未覆盖全部现存层位，缺少：" + "、".join(missing))
        return Result.failure("提交包含本探方之外或已不存在的层位编号")

    parsed: dict[int, int] = {}
    for lid, value in raw.items():
        try:
            new_seq = int(value)
        except (TypeError, ValueError):
            code = next(row["code"] for row in existing if row["id"] == lid)
            return Result.failure(f"层位 {code} 的新顺序号必须是整数")
        if new_seq <= 0:
            code = next(row["code"] for row in existing if row["id"] == lid)
            return Result.failure(f"层位 {code} 的新顺序号必须为正整数")
        parsed[lid] = new_seq

    if len(set(parsed.values())) != len(parsed):
        return Result.failure("新顺序号必须互不重复")
    return Result.success(**{"order": parsed})


def _relation_impacts(layers: list, new_seq: dict[int, int], rel_pairs) -> tuple[list[dict], list[dict]]:
    """按新顺序评估每条现存覆盖关系，返回 (全部受影响关系, 冲突关系)。

    冲突：覆盖方新顺序号不小于被覆盖方（new_upper_seq >= new_lower_seq）。
    """
    code_map = {row["id"]: row["code"] for row in layers}
    old_seq = {row["id"]: row["seq"] for row in layers}
    affected, conflicts = [], []
    for upper_id, lower_id in rel_pairs:
        item = {
            "upper_id": upper_id,
            "lower_id": lower_id,
            "upper_code": code_map[upper_id],
            "lower_code": code_map[lower_id],
            "upper_old": old_seq[upper_id],
            "lower_old": old_seq[lower_id],
            "upper_new": new_seq[upper_id],
            "lower_new": new_seq[lower_id],
            "moved": (
                old_seq[upper_id] != new_seq[upper_id]
                or old_seq[lower_id] != new_seq[lower_id]
            ),
        }
        item["conflict"] = item["upper_new"] >= item["lower_new"]
        affected.append(item)
        if item["conflict"]:
            conflicts.append(item)
    return affected, conflicts


def preview_reorder(conn, grid_id: int, new_order: dict, actor: str = "") -> Result:
    """生成顺序调整预览：校验输入并按新顺序评估全部覆盖关系（不改动任何层位）。

    预览保存为一次性凭据（token + 状态指纹）；同一探方旧预览自动失效。
    存在冲突关系时仍生成预览（供查看冲突），但提交会被禁止。
    """
    grid = get_grid(conn, grid_id)
    if grid is None:
        return Result.failure("探方不存在")

    parsed = _parse_new_order(conn, grid_id, new_order)
    if not parsed.ok:
        return parsed
    new_seq = parsed.data["order"]

    layers = list_layers(conn, grid_id)
    rel_pairs = _relations_as_pairs(conn, grid_id)
    affected, conflicts = _relation_impacts(layers, new_seq, rel_pairs)
    changes = [
        {
            "layer_id": row["id"],
            "code": row["code"],
            "old_seq": row["seq"],
            "new_seq": new_seq[row["id"]],
            "status": row["status"],
        }
        for row in layers
    ]

    token = secrets.token_hex(16)
    payload = json.dumps(
        {"new_order": new_seq, "changes": changes, "relations": affected},
        ensure_ascii=False,
    )
    ts = audit.now_ts()
    # 同一探方只保留最新一份预览
    conn.execute("DELETE FROM reorder_previews WHERE grid_id = ?", (grid_id,))
    cur = conn.execute(
        """INSERT INTO reorder_previews
               (grid_id, token, fingerprint, payload, actor, created_at)
           VALUES (?,?,?,?,?,?)""",
        (grid_id, token, grid_fingerprint(conn, grid_id), payload, actor.strip(), ts),
    )
    preview_id = cur.lastrowid
    conn.commit()
    return Result.success(
        preview_id,
        token=token,
        changes=changes,
        relations=affected,
        conflicts=conflicts,
    )


def get_reorder_preview(conn, preview_id: int, token: str):
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM reorder_previews WHERE id = ? AND token = ?",
        (preview_id, token),
    ).fetchone()


def commit_reorder(conn, preview_id: int, token: str, actor: str = "") -> Result:
    """凭预览一次性提交顺序调整（原子操作）。

    预览必须存在、状态指纹仍与当前一致（层位/关系未变化）、提交的新顺序仍满足
    覆盖关系约束。任一层位更新失败则整体回滚，不留部分更新。
    """
    preview = get_reorder_preview(conn, preview_id, token)
    if preview is None:
        return Result.failure("预览不存在或凭据无效，请重新预览")
    grid_id = preview["grid_id"]
    grid = get_grid(conn, grid_id)
    if grid is None:
        return Result.failure("探方不存在")

    stale = preview["fingerprint"] != grid_fingerprint(conn, grid_id)

    try:
        payload = json.loads(preview["payload"])
    except (ValueError, TypeError):
        payload = {}
    new_seq = {int(k): int(v) for k, v in payload.get("new_order", {}).items()}

    if stale:
        reason = "预览后层位或覆盖关系已发生变化，本次提交已拒绝，请重新预览"
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：预览已过期",
            {"reason": reason, "preview_id": preview_id}, commit=False,
        )
        conn.commit()
        return Result.failure(reason)

    # 提交时重新校验，不盲信预览快照
    parsed = _parse_new_order(conn, grid_id, new_seq)
    if not parsed.ok:
        return parsed
    new_seq = parsed.data["order"]
    layers = list_layers(conn, grid_id)
    _, conflicts = _relation_impacts(layers, new_seq, _relations_as_pairs(conn, grid_id))
    if conflicts:
        desc = "；".join(f"{c['upper_code']}→{c['lower_code']}" for c in conflicts)
        reason = (
            f"新顺序存在 {len(conflicts)} 处覆盖冲突（覆盖方顺序号不小于被覆盖方）：{desc}；"
            "已禁止应用"
        )
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：覆盖冲突 {desc}",
            {"reason": reason, "conflicts": conflicts, "preview_id": preview_id},
            commit=False,
        )
        conn.commit()
        return Result.failure(reason)

    changes = [
        {
            "layer_id": row["id"],
            "code": row["code"],
            "old_seq": row["seq"],
            "new_seq": new_seq[row["id"]],
        }
        for row in layers
    ]
    changed_count = sum(1 for c in changes if c["old_seq"] != c["new_seq"])
    summary_changes = [
        f"{c['code']}:{c['old_seq']}→{c['new_seq']}"
        for c in changes if c["old_seq"] != c["new_seq"]
    ]
    summary = (
        f"探方 {grid['code']} 整体调整层位发掘顺序（{changed_count} 个层位变更："
        + ("，".join(summary_changes) if summary_changes else "顺序无变化") + "）"
    )

    ts = audit.now_ts()
    try:
        # 两阶段更新，规避 UNIQUE(grid_id, seq) 中途冲突：
        # 1) 全部先置为互不相同的负临时值（同事务内不提交，外部不可见中间态）
        for lid, target in new_seq.items():
            conn.execute(
                "UPDATE layers SET seq = ? WHERE id = ? AND grid_id = ?",
                (-target, lid, grid_id),
            )
        # 2) 再统一置为最终顺序号
        for lid, target in new_seq.items():
            conn.execute("UPDATE layers SET seq = ? WHERE id = ?", (target, lid))
        conn.execute("UPDATE grids SET updated_at = ? WHERE id = ?", (ts, grid_id))
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, True,
            summary, {"changes": changes, "preview_id": preview_id}, commit=False,
        )
        # 一次性预览：提交成功后删除，防止重复使用
        conn.execute("DELETE FROM reorder_previews WHERE id = ?", (preview_id,))
        conn.commit()
    except sqlite3.DatabaseError as exc:
        # 任何中途失败都整体回滚，绝不留下部分更新
        conn.rollback()
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整失败并已整体回滚：{exc}",
            {"preview_id": preview_id},
        )
        return Result.failure(f"顺序调整失败，已整体回滚，请重试：{exc}")

    return Result.success(grid_id, changes=changes, changed_count=changed_count)


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
