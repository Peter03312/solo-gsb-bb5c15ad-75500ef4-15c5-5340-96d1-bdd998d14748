"""覆盖关系：发掘顺序校验、无环（DAG）校验与冲突草稿。"""

from app import services


def test_valid_relations_build_dag(conn, grid_with_layers):
    g = grid_with_layers
    r1 = services.add_relation(conn, g["grid"], g["l1"], g["l2"], "压覆", "tester")
    r2 = services.add_relation(conn, g["grid"], g["l2"], g["l3"], "压覆", "tester")
    assert r1.ok and r2.ok
    assert len(services.list_relations(conn, g["grid"])) == 2


def test_duplicate_relation_is_idempotent(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    again = services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    assert again.ok and again.data.get("duplicated")
    assert len(services.list_relations(conn, g["grid"])) == 1


def test_reversed_order_rejected_and_drafted(conn, grid_with_layers):
    # 覆盖方 seq 更大（后发掘）→ 违反发掘顺序
    g = grid_with_layers
    result = services.add_relation(conn, g["grid"], g["l3"], g["l1"])
    assert not result.ok
    assert "发掘顺序" in result.error
    assert "L3" in result.conflict_path and "L1" in result.conflict_path
    assert result.draft_id is not None

    # 关系未写入
    assert services.list_relations(conn, g["grid"]) == []
    # 草稿保留了原始录入
    draft = services.get_draft(conn, result.draft_id)
    assert draft is not None and draft["kind"] == "relation"


def test_cycle_detected_with_conflict_path(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    services.add_relation(conn, g["grid"], g["l2"], g["l3"])

    # 再加 L3→L1 形成 L1→L2→L3→L1 环；顺序本身合法（3 不小于 1？不合法！）
    # L3.seq=3 >= L1.seq=1，会先被顺序校验拦截。为专门测环，构造 seq 合规但成环的场景：
    # L1→L3 已可经 L1→L2→L3 到达；此处用“反向成环且顺序非法”仍走顺序分支，
    # 故下面另建专用层位验证纯成环路径。
    result = services.add_relation(conn, g["grid"], g["l3"], g["l1"])
    assert not result.ok  # 无论走哪个分支都必须拒绝


def test_bfs_second_line_of_defense_catches_cycle(conn, grid_with_layers):
    """顺序校验是第一道防线（在 seq 严格递增的不变量下即可阻止成环）；
    BFS 环检测是第二道防线：即便存在绕过服务层的脏数据边，成环录入仍会被捕获。"""
    from app import audit

    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    services.add_relation(conn, g["grid"], g["l2"], g["l3"])

    # 正常图：L1 经链可达 L3，反向不可达
    from app.services import _find_path

    assert _find_path(conn, g["grid"], g["l1"], g["l3"]) == [g["l1"], g["l2"], g["l3"]]
    assert _find_path(conn, g["grid"], g["l3"], g["l1"]) == []

    # 脏数据边 L3→L1（绕过顺序校验直接入库）
    conn.execute(
        "INSERT INTO layer_relations (grid_id, upper_id, lower_id, note, created_at)"
        " VALUES (?,?,?,?,?)",
        (g["grid"], g["l3"], g["l1"], "脏数据", audit.now_ts()),
    )

    # 此时录入 L1→L3：顺序合法（1<3），但会闭合出环 L1→…→L3→L1
    result = services.add_relation(conn, g["grid"], g["l1"], g["l3"])
    assert not result.ok
    assert "成环" in result.error
    assert "L1" in result.conflict_path and "L3" in result.conflict_path
    assert result.draft_id is not None
    # 关系仍然没有写入
    assert conn.execute(
        "SELECT COUNT(*) c FROM layer_relations WHERE upper_id=? AND lower_id=?",
        (g["l1"], g["l3"]),
    ).fetchone()["c"] == 0


def test_relation_endpoints_must_be_same_grid(conn, grid_with_layers):
    g = grid_with_layers
    other = services.create_grid(conn, "T02", "另一遗址", "", "t")
    lo = services.create_layer(conn, other.id, "X1", 1, "", "", "", "t").id
    result = services.add_relation(conn, g["grid"], g["l1"], lo)
    assert not result.ok and "同一探方" in result.error


def test_self_relation_rejected(conn, grid_with_layers):
    g = grid_with_layers
    result = services.add_relation(conn, g["grid"], g["l1"], g["l1"])
    assert not result.ok


def test_failed_attempts_are_audited_but_not_applied(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l2"], g["l1"])  # 顺序非法
    bad = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action='add_relation' AND success=0"
    ).fetchone()["c"]
    assert bad >= 1


def test_draft_retry_and_discard(conn, grid_with_layers):
    g = grid_with_layers
    result = services.add_relation(conn, g["grid"], g["l2"], g["l1"])
    draft_id = result.draft_id
    # 矛盾仍在（seq 不可变），重试依旧失败且不会清除草稿
    again = services.retry_draft(conn, draft_id)
    assert not again.ok
    assert services.get_draft(conn, draft_id) is not None
    # 丢弃后草稿消失，但审计保留
    assert services.discard_draft(conn, draft_id).ok
    assert services.get_draft(conn, draft_id) is None
