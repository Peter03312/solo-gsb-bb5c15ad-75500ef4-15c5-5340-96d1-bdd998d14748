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
    def failure(
        cls, error: str, conflict_path: str = "", draft_id: int | None = None, **data
    ) -> "Result":
        return cls(False, error=error, conflict_path=conflict_path,
                   draft_id=draft_id, data=data)


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


# ---------------------------------------------------------------- 层位顺序整体调整

# 预览令牌的有效期：预览后应尽快确认提交
REORDER_TOKEN_MAX_AGE = 30 * 60


def _reorder_serializer():
    """预览令牌签名器（复用 Flask SECRET_KEY，不新增表、不依赖会话存储）。"""
    from itsdangerous import URLSafeSerializer

    from flask import current_app

    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="layer-reorder-preview")


def _layers_state_fingerprint(layers, relations) -> str:
    """对影响覆盖关系校验的状态取指纹：层位集合（id/seq/status）与全部覆盖边。

    预览与提交之间任一项发生变化（新增/关闭层位、顺序变化、关系增删等），
    指纹即不同，旧预览令牌将被拒绝。
    """
    import hashlib

    parts = [
        ("layer", row["id"], row["seq"], row["status"])
        for row in layers
    ]
    parts += [
        ("rel", row["upper_id"], row["lower_id"])
        for row in sorted(relations, key=lambda r: (r["upper_id"], r["lower_id"]))
    ]
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_reorder_input(conn, grid_id: int, new_seq_by_code: dict):
    """解析并校验输入：编号恰好覆盖现存层位、顺序号为互不重复的正整数。

    返回 (layers, code_to_new_seq, error)；error 非空表示校验失败。
    """
    layers = list_layers(conn, grid_id)
    if not layers:
        return layers, {}, "该探方暂无层位，无法调整顺序"

    existing_codes = {row["code"] for row in layers}
    submitted_codes = set(new_seq_by_code)

    errors: list[str] = []
    missing = existing_codes - submitted_codes
    extra = submitted_codes - existing_codes
    if missing:
        errors.append("以下现存层位未提供新顺序号：" + "、".join(sorted(missing)))
    if extra:
        errors.append("以下编号不属于该探方现存层位：" + "、".join(sorted(extra)))

    code_to_new_seq: dict[str, int] = {}
    invalid: list[str] = []
    for code in existing_codes & submitted_codes:
        raw = new_seq_by_code[code]
        try:
            seq = int(raw)
        except (TypeError, ValueError):
            invalid.append(f"{code}（{raw!r} 不是整数）")
            continue
        if seq <= 0:
            invalid.append(f"{code}（顺序号须为正整数）")
            continue
        code_to_new_seq[code] = seq
    if invalid:
        errors.append("新顺序号不合法：" + "、".join(invalid))

    seen: dict[int, str] = {}
    duplicates: set[str] = set()
    for code, seq in code_to_new_seq.items():
        if seq in seen:
            duplicates.add(code)
            duplicates.add(seen[seq])
        else:
            seen[seq] = code
    if duplicates:
        errors.append("新顺序号必须互不重复，重复占用：" + "、".join(sorted(duplicates)))

    return layers, code_to_new_seq, "；".join(errors)


def _build_reorder_preview(conn, grid_id: int, layers, code_to_new_seq: dict) -> dict:
    """根据新顺序计算覆盖关系冲突与各层位变更前后顺序。"""
    seq_by_id = {row["id"]: code_to_new_seq[row["code"]] for row in layers}
    relations = list_relations(conn, grid_id)

    relation_rows = []
    conflicts = []
    for rel in relations:
        upper_new = seq_by_id[rel["upper_id"]]
        lower_new = seq_by_id[rel["lower_id"]]
        item = {
            "upper_code": rel["upper_code"],
            "lower_code": rel["lower_code"],
            "upper_old": rel["upper_seq"],
            "lower_old": rel["lower_seq"],
            "upper_new": upper_new,
            "lower_new": lower_new,
            "conflict": upper_new >= lower_new,
        }
        relation_rows.append(item)
        if item["conflict"]:
            conflicts.append(item)

    changes = [
        {
            "layer_id": row["id"],
            "code": row["code"],
            "old_seq": row["seq"],
            "new_seq": code_to_new_seq[row["code"]],
            "changed": row["seq"] != code_to_new_seq[row["code"]],
        }
        for row in layers
    ]
    changes.sort(key=lambda c: c["new_seq"])
    return {
        "relations": relation_rows,
        "conflicts": conflicts,
        "changes": changes,
        "fingerprint": _layers_state_fingerprint(layers, relations),
    }


def preview_reorder_layers(
    conn, grid_id: int, new_seq_by_code: dict, actor: str = "",
) -> Result:
    """预览层位顺序整体调整：展示新旧顺序与受影响覆盖关系。

    不写库。通过全部校验且无覆盖冲突时，data 中返回签名令牌 token，
    供 commit_reorder_layers 凭该次预览提交。
    """
    if get_grid(conn, grid_id) is None:
        return Result.failure("探方不存在")

    layers, code_to_new_seq, error = _validate_reorder_input(
        conn, grid_id, new_seq_by_code
    )
    if error:
        return Result.failure(error)

    preview = _build_reorder_preview(conn, grid_id, layers, code_to_new_seq)
    if preview["conflicts"]:
        lines = [
            f"{c['upper_code']}(新顺序 {c['upper_new']}) ✗ "
            f"{c['lower_code']}(新顺序 {c['lower_new']})"
            for c in preview["conflicts"]
        ]
        reason = (
            "新顺序违反覆盖关系：覆盖方顺序号必须小于被覆盖方。冲突关系："
            + "；".join(lines)
        )
        return Result.failure(
            reason,
            conflict_path="；".join(
                f"{c['upper_code']}→{c['lower_code']}" for c in preview["conflicts"]
            ),
            preview=preview,
            actor=actor.strip(),
        )

    token = _reorder_serializer().dumps(
        {
            "grid_id": grid_id,
            "new_seq": {str(row["id"]): code_to_new_seq[row["code"]] for row in layers},
            "fingerprint": preview["fingerprint"],
        }
    )
    return Result.success(token=token, preview=preview, actor=actor.strip())


def commit_reorder_layers(conn, token: str, actor: str = "") -> Result:
    """凭一次有效预览一次性应用层位顺序调整。

    - 令牌过期/被篡改/不属于该探方 → 拒绝；
    - 预览后层位或覆盖关系发生变化（指纹不符）→ 拒绝并要求重新预览；
    - 覆盖关系在当前数据下不再满足顺序约束 → 拒绝并要求重新预览。
    通过时在单个事务内完成全部 seq 更新与审计写入，不会因唯一约束冲突留下部分更新。
    """
    grid_id = None
    try:
        payload = _reorder_serializer().loads(token, max_age=REORDER_TOKEN_MAX_AGE)
        grid_id = payload.get("grid_id")
        new_seq_raw = payload.get("new_seq")
        fingerprint = payload.get("fingerprint")
        if not isinstance(grid_id, int) or not isinstance(new_seq_raw, dict):
            raise ValueError("令牌内容不完整")
        new_seq_by_id = {int(k): int(v) for k, v in new_seq_raw.items()}
    except Exception as exc:  # BadSignature / SignatureExpired / 内容异常
        reason = f"预览令牌无效或已过期，请重新预览后再提交（{exc.__class__.__name__}）"
        audit.log(
            conn, "reorder_layers", "grid", None, None, actor, False,
            f"顺序调整提交被拒绝：{reason}", {"reason": reason},
        )
        return Result.failure(reason)

    grid = get_grid(conn, grid_id)
    if grid is None:
        return Result.failure("探方不存在")

    layers = list_layers(conn, grid_id)
    current_ids = {row["id"] for row in layers}
    if set(new_seq_by_id) != current_ids:
        reason = "预览后层位集合发生变化（有层位新增或删除），请重新预览后再提交"
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：{reason}", {"reason": reason},
        )
        return Result.failure(reason, stale=True)
    if len(set(new_seq_by_id.values())) != len(new_seq_by_id) or any(
        v <= 0 for v in new_seq_by_id.values()
    ):
        reason = "新顺序号必须为互不重复的正整数，请重新预览"
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：{reason}", {"reason": reason},
        )
        return Result.failure(reason, stale=True)

    relations = list_relations(conn, grid_id)
    if _layers_state_fingerprint(layers, relations) != fingerprint:
        reason = "预览后层位或覆盖关系已发生变化，旧预览已失效，请重新预览后再提交"
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：{reason}", {"reason": reason},
        )
        return Result.failure(reason, stale=True)

    # 二次确认：覆盖方顺序号不得不小于被覆盖方
    bad = [
        rel for rel in relations
        if new_seq_by_id[rel["upper_id"]] >= new_seq_by_id[rel["lower_id"]]
    ]
    if bad:
        conflict_path = "；".join(
            f"{rel['upper_code']}→{rel['lower_code']}" for rel in bad
        )
        reason = (
            "当前覆盖关系在新顺序下存在冲突（覆盖方顺序号不小于被覆盖方），"
            f"已禁止应用，请重新预览。冲突关系：{conflict_path}"
        )
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整提交被拒绝：{reason}",
            {"reason": reason, "conflict_path": conflict_path},
        )
        return Result.failure(reason, conflict_path=conflict_path, stale=True)

    changes = [
        {
            "layer_id": row["id"],
            "code": row["code"],
            "old_seq": row["seq"],
            "new_seq": new_seq_by_id[row["id"]],
            "changed": row["seq"] != new_seq_by_id[row["id"]],
        }
        for row in layers
    ]
    changed = [c for c in changes if c["changed"]]
    changed.sort(key=lambda c: c["new_seq"])

    ts = audit.now_ts()
    try:
        # 显式事务保证原子性：先结清连接上可能残留的隐式事务（正常流程中为空操作），
        # 再显式开启；两阶段更新全部在同一事务内，最后一次 commit 原子提交。
        # 先全部让出正整数序位，再写入新顺序，
        # 避免互换（如 2↔3）时撞上 layers(grid_id, seq) 唯一约束。
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for lid in new_seq_by_id:
            conn.execute("UPDATE layers SET seq = ? WHERE id = ?", (-lid, lid))
        for lid, new_seq in new_seq_by_id.items():
            conn.execute("UPDATE layers SET seq = ? WHERE id = ?", (new_seq, lid))
        conn.execute(
            "UPDATE grids SET updated_at = ? WHERE id = ?", (ts, grid_id)
        )
        summary_changes = [
            {"layer_id": c["layer_id"], "code": c["code"],
             "old_seq": c["old_seq"], "new_seq": c["new_seq"]}
            for c in changed
        ]
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, True,
            f"探方 {grid['code']} 整体调整发掘顺序：{len(changed)} 个层位顺序变更"
            + ("（" + "，".join(f"{c['code']}:{c['old_seq']}→{c['new_seq']}" for c in changed) + "）"
               if changed else ""),
            {"changes": summary_changes,
             "all_layers": [
                 {"layer_id": c["layer_id"], "code": c["code"],
                  "old_seq": c["old_seq"], "new_seq": c["new_seq"]}
                 for c in changes]},
            commit=False,
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        audit.log(
            conn, "reorder_layers", "grid", grid_id, grid_id, actor, False,
            f"探方 {grid['code']} 顺序调整失败，已整体回滚：{exc}",
            {"error": str(exc)},
        )
        return Result.failure(f"顺序调整失败，已整体回滚，未留下部分更新：{exc}")

    return Result.success(grid_id, changes=changes, changed_count=len(changed))


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
