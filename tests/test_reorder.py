"""层位发掘顺序整体调整：预览、凭预览提交、冲突禁止、旧预览失效、原子性、回放与审计。"""

from app import replay, services


def _grid_with_relations(conn):
    gid = services.create_grid(conn, "T01", "测试遗址", "", "t").id
    l1 = services.create_layer(conn, gid, "L1", 1, "表土", "", "", "t").id
    l2 = services.create_layer(conn, gid, "L2", 2, "文化层", "", "", "t").id
    l3 = services.create_layer(conn, gid, "L3", 3, "生土", "", "", "t").id
    services.add_relation(conn, gid, l1, l2, "压覆", "t")
    services.add_relation(conn, gid, l2, l3, "压覆", "t")
    return gid, l1, l2, l3


def _seqs(conn, gid):
    return {r["code"]: r["seq"] for r in services.list_layers(conn, gid)}


# ------------------------------------------------------------- 输入校验

def test_preview_requires_all_existing_layers(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 漏掉 L3、多给不存在的 L9
    result = services.preview_reorder_layers(conn, gid, {"L1": "2", "L2": "1", "L9": "9"})
    assert not result.ok
    assert "L3" in result.error and "L9" in result.error
    # 未写库，顺序不变
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}
    assert result.data.get("preview") is None


def test_preview_rejects_non_positive_and_duplicate_seqs(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "0", "L2": "-2", "L3": "2"}
    )
    assert not result.ok
    assert "正整数" in result.error
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}

    dup = services.preview_reorder_layers(conn, gid, {"L1": "5", "L2": "5", "L3": "6"})
    assert not dup.ok and "互不重复" in dup.error

    bad_int = services.preview_reorder_layers(
        conn, gid, {"L1": "x", "L2": "2", "L3": "3"}
    )
    assert not bad_int.ok


# ------------------------------------------------------------- 冲突预览

def test_preview_lists_covering_conflicts_and_forbids_apply(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 新顺序 L1=3, L2=2, L3=1：关系 L1→L2（3>=2）、L2→L3（2>=1）全部冲突
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "3", "L2": "2", "L3": "1"}
    )
    assert not result.ok
    assert result.data.get("token") is None
    preview = result.data["preview"]
    conflicts = preview["conflicts"]
    assert len(conflicts) == 2
    labels = {f"{c['upper_code']}→{c['lower_code']}" for c in conflicts}
    assert labels == {"L1→L2", "L2→L3"}
    assert "L1→L2" in result.conflict_path
    # 预览不写库
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}


def test_preview_valid_non_contiguous_numbering_allowed(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 非连续编号 10/20/30，方向仍递增 → 合法
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}
    )
    assert result.ok
    assert result.data["token"]
    changes = {c["code"]: (c["old_seq"], c["new_seq"])
               for c in result.data["preview"]["changes"]}
    assert changes == {"L1": (1, 10), "L2": (2, 20), "L3": (3, 30)}
    # 仅预览，尚未生效
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}


# ------------------------------------------------------------- 原子提交

def test_commit_applies_atomically_with_unique_constraint_swap(conn):
    """互换两个层位的正整数序位（2↔3）也能一次提交成功：
    两阶段更新先让出序位，不会撞上 UNIQUE(grid_id, seq)，且无负序残留。"""
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 去掉 L2→L3 边，使 L2=3、L3=2 不再有覆盖冲突（L1→L2 仍为 1<3）
    conn.execute("DELETE FROM layer_relations WHERE upper_id=? AND lower_id=?", (l2, l3))
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "1", "L2": "3", "L3": "2"}
    )
    assert result.ok
    preview_changes = {c["code"]: c["changed"] for c in result.data["preview"]["changes"]}
    assert preview_changes == {"L1": False, "L2": True, "L3": True}

    commit = services.commit_reorder_layers(conn, result.data["token"], "t")
    assert commit.ok
    assert commit.data["changed_count"] == 2
    assert _seqs(conn, gid) == {"L1": 1, "L2": 3, "L3": 2}
    bad = conn.execute("SELECT COUNT(*) c FROM layers WHERE seq <= 0").fetchone()["c"]
    assert bad == 0


def test_commit_no_partial_update_on_integrity_failure(conn):
    """第二阶段写入时若数据库层面失败，事务必须整体回滚，不留部分更新。

    用一个临时触发器在 seq 被更新为 20 时中止语句（模拟约束/磁盘故障）。
    """
    gid, l1, l2, l3 = _grid_with_relations(conn)
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}
    )
    assert result.ok
    before = _seqs(conn, gid)

    conn.execute(
        """CREATE TEMP TRIGGER fail_on_seq20
           BEFORE UPDATE OF seq ON layers
           WHEN NEW.seq = 20
           BEGIN
               SELECT RAISE(ABORT, 'simulated integrity fault');
           END;"""
    )
    try:
        commit = services.commit_reorder_layers(conn, result.data["token"], "t")
        assert not commit.ok
        assert "回滚" in commit.error
    finally:
        conn.execute("DROP TRIGGER fail_on_seq20")

    # 事务整体回滚：顺序与之前完全一致，无部分更新、无负序残留
    assert _seqs(conn, gid) == before
    bad = conn.execute("SELECT COUNT(*) c FROM layers WHERE seq <= 0").fetchone()["c"]
    assert bad == 0
    # 失败已留痕
    denied = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action='reorder_layers' AND success=0"
    ).fetchone()["c"]
    assert denied >= 1


def test_commit_unknown_grid_in_token_rejected(conn, grid_with_layers):
    result = services.commit_reorder_layers(conn, "not-a-real-token", "t")
    assert not result.ok and "令牌" in result.error


# ------------------------------------------------------------- 旧预览失效

def test_stale_preview_rejected_after_layer_change(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}
    )
    token = result.data["token"]
    # 预览后关闭层位（状态变化进入指纹）
    services.close_layer(conn, l2, "t")
    commit = services.commit_reorder_layers(conn, token, "t")
    assert not commit.ok and "重新预览" in commit.error
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}
    # 审计中留下被拒绝的提交记录
    denied = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action='reorder_layers' AND success=0"
    ).fetchone()["c"]
    assert denied >= 1


def test_stale_preview_rejected_when_relation_added_after_preview(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 预览时不存在 L2→L3：新顺序 L2=3、L3=2 不违反任何现存边
    conn.execute("DELETE FROM layer_relations WHERE upper_id=? AND lower_id=?", (l2, l3))
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "1", "L2": "3", "L3": "2"}, actor="t"
    )
    assert result.ok
    token = result.data["token"]
    # 预览后补登 L2→L3（当前顺序 2<3 合法），但新顺序下 3>=2 冲突
    added = services.add_relation(conn, gid, l2, l3, "后补", "t")
    assert added.ok
    commit = services.commit_reorder_layers(conn, token, "t")
    assert not commit.ok and "重新预览" in commit.error
    assert _seqs(conn, gid) == {"L1": 1, "L2": 2, "L3": 3}


# ------------------------------------------------------------- 已关闭层位

def test_closed_layer_participates_but_keeps_status_and_artifacts(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    services.register_artifact(conn, l2, "陶片", 5, "关闭前批次", "t")
    services.close_layer(conn, l2, "t")
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}, actor="t"
    )
    assert result.ok
    assert services.commit_reorder_layers(conn, result.data["token"], "t").ok

    l2_row = services.get_layer(conn, l2)
    assert l2_row["seq"] == 20 and l2_row["status"] == "closed"
    batches = services.list_batches_for_grid(conn, gid)
    l2_batches = [b for b in batches if b["layer_id"] == l2]
    assert len(l2_batches) == 1  # 遗物归属不变
    # 关闭层位补录依旧被拒绝
    rejected = services.register_artifact(conn, l2, "骨器", 1, "x", "t")
    assert not rejected.ok


# ------------------------------------------------------------- 后续录入按新顺序校验

def test_future_relation_validated_against_new_order(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    # 新顺序 L1=10, L2=20, L3=30：再建 L1→L3 合法；反向 L3→L1 仍被拒
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}, actor="t"
    )
    assert services.commit_reorder_layers(conn, result.data["token"], "t").ok

    ok = services.add_relation(conn, gid, l1, l3, "间接压覆", "t")
    assert ok.ok
    bad = services.add_relation(conn, gid, l3, l1, "误录", "t")
    assert not bad.ok and "发掘顺序" in bad.error


# ------------------------------------------------------------- 审计与回放

def test_audit_records_before_after_for_every_layer(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}, actor="t"
    )
    assert services.commit_reorder_layers(conn, result.data["token"], "张田野").ok

    import json

    row = conn.execute(
        "SELECT * FROM audit_logs WHERE action='reorder_layers' AND success=1"
    ).fetchone()
    assert row is not None
    details = json.loads(row["details"])
    by_code = {c["code"]: (c["old_seq"], c["new_seq"]) for c in details["changes"]}
    assert by_code == {"L1": (1, 10), "L2": (2, 20), "L3": (3, 30)}
    assert "1→10" in row["summary"]


def test_replay_old_frames_show_old_seq_and_new_frames_new_seq(conn):
    gid, l1, l2, l3 = _grid_with_relations(conn)
    result = services.preview_reorder_layers(
        conn, gid, {"L1": "10", "L2": "20", "L3": "30"}, actor="t"
    )
    assert services.commit_reorder_layers(conn, result.data["token"], "t").ok

    state = replay.replay_grid(conn, gid)
    events = state["events"]
    reorder_index = next(
        i for i, e in enumerate(events) if e["action"] == "reorder_layers" and e["success"]
    )

    def seq_map(event):
        return {n["code"]: n["seq"] for n in event["graph"]["nodes"]}

    # 旧帧保持旧顺序
    assert seq_map(events[reorder_index - 1]) == {"L1": 1, "L2": 2, "L3": 3}
    # 新帧显示新顺序
    assert seq_map(events[reorder_index]) == {"L1": 10, "L2": 20, "L3": 30}
    assert seq_map(events[-1]) == {"L1": 10, "L2": 20, "L3": 30}
    # 状态（开放/关闭）与边数不因改序改变
    last = events[-1]
    assert last["edge_count"] == 2
    assert last["open_count"] == 3 and last["closed_count"] == 0


# ------------------------------------------------------------- HTTP 流程

def test_reorder_http_full_flow(client, app):
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        gid = services.create_grid(conn, "T08", "", "", "t").id
        l1 = services.create_layer(conn, gid, "L1", 1, "", "", "", "t").id
        l2 = services.create_layer(conn, gid, "L2", 2, "", "", "", "t").id
        services.add_relation(conn, gid, l1, l2, "", "t")

    # 表单页可打开
    assert client.get(f"/grids/{gid}/layers/reorder").status_code == 200

    # 冲突预览（200 渲染预览页，列出冲突，不跳转）
    resp = client.post(
        f"/grids/{gid}/layers/reorder/preview",
        data={"seq_L1": "2", "seq_L2": "1", "actor": "t"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "发现冲突，禁止应用" in body and "L1→L2" in body

    # 合法预览：页面带令牌与提交按钮
    resp = client.post(
        f"/grids/{gid}/layers/reorder/preview",
        data={"seq_L1": "10", "seq_L2": "20", "actor": "t"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "凭此预览提交应用" in body
    import re
    token = re.search(r'name="token" value="([^"]+)"', body).group(1)

    # 提交生效
    resp = client.post(
        f"/grids/{gid}/layers/reorder/commit",
        data={"token": token, "actor": "t"},
    )
    assert resp.status_code == 302
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        assert {r["code"]: r["seq"] for r in services.list_layers(conn, gid)} == {
            "L1": 10, "L2": 20
        }

    # 审计导出可追溯本次调整
    export = client.get("/audit/export").get_data(as_text=True)
    assert "reorder_layers" in export and "整体调整发掘顺序" in export


def test_reorder_http_stale_token_redirects_with_message(client, app):
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        gid = services.create_grid(conn, "T07", "", "", "t").id
        services.create_layer(conn, gid, "L1", 1, "", "", "", "t")
        services.create_layer(conn, gid, "L2", 2, "", "", "", "t")
        result = services.preview_reorder_layers(
            conn, gid, {"L1": "5", "L2": "6"}, actor="t"
        )
        token = result.data["token"]

    # 篡改令牌
    resp = client.post(
        f"/grids/{gid}/layers/reorder/commit",
        data={"token": token + "x", "actor": "t"},
    )
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "重新预览" in page
