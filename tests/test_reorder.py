"""层位发掘顺序整体调整：预览/提交两阶段、覆盖冲突、原子性、陈旧拒绝、回放。"""

import json

from app import replay, services


# ---------------------------------------------------------------- 输入校验

def test_preview_requires_exact_layer_coverage(conn, grid_with_layers):
    g = grid_with_layers
    # 缺少 L3
    result = services.preview_reorder(conn, g["grid"], {g["l1"]: 1, g["l2"]: 2})
    assert not result.ok and "覆盖" in result.error and "L3" in result.error

    # 多出不存在的层位 id
    result = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 1, g["l2"]: 2, g["l3"]: 3, 9999: 4}
    )
    assert not result.ok and "之外" in result.error


def test_preview_requires_positive_distinct_integers(conn, grid_with_layers):
    g = grid_with_layers
    not_int = services.preview_reorder(
        conn, g["grid"], {g["l1"]: "x", g["l2"]: 2, g["l3"]: 3}
    )
    assert not not_int.ok and "整数" in not_int.error

    zero = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 0, g["l2"]: 2, g["l3"]: 3}
    )
    assert not zero.ok and "正整数" in zero.error

    dup = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 2, g["l3"]: 3}
    )
    assert not dup.ok and "互不重复" in dup.error


def test_preview_valid_input_does_not_change_layers(conn, grid_with_layers):
    g = grid_with_layers
    result = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 3, g["l2"]: 2, g["l3"]: 1}
    )
    assert result.ok and result.data["token"]
    # 预览不改任何顺序
    assert services.get_layer(conn, g["l1"])["seq"] == 1
    assert services.get_layer(conn, g["l3"])["seq"] == 3


# ---------------------------------------------------------------- 覆盖冲突

def test_preview_lists_conflicting_relations(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"], "压覆", "t")
    services.add_relation(conn, g["grid"], g["l2"], g["l3"], "压覆", "t")

    # 整体倒序：L1(1→3) 覆盖 L2(2→2) 变成 3 >= 2，冲突
    result = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 3, g["l2"]: 2, g["l3"]: 1}
    )
    assert result.ok  # 预览仍生成，供查看冲突
    conflicts = result.data["conflicts"]
    assert len(conflicts) == 2
    labels = {f"{c['upper_code']}→{c['lower_code']}" for c in conflicts}
    assert labels == {"L1→L2", "L2→L3"}

    # 凭冲突预览提交必须被禁止
    commit = services.commit_reorder(conn, result.id, result.data["token"])
    assert not commit.ok and "禁止应用" in commit.error
    assert services.get_layer(conn, g["l1"])["seq"] == 1  # 未改动


def test_commit_after_valid_reorder_applies_atomically(conn, grid_with_layers):
    g = grid_with_layers
    # 仅 L1→L3 一条关系：新顺序 L2=1, L1=2, L3=3 仍满足 2 < 3
    services.add_relation(conn, g["grid"], g["l1"], g["l3"], "压覆", "t")

    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    assert not preview.data["conflicts"]
    done = services.commit_reorder(conn, preview.id, preview.data["token"])
    assert done.ok
    assert done.data["changed_count"] == 2
    assert services.get_layer(conn, g["l1"])["seq"] == 2
    assert services.get_layer(conn, g["l2"])["seq"] == 1
    assert services.get_layer(conn, g["l3"])["seq"] == 3


def test_swap_two_seqs_does_not_hit_midway_unique_violation(conn, grid_with_layers):
    """两个相邻顺序号直接对调：朴素逐条 UPDATE 会在第一条就撞上 UNIQUE(grid_id, seq)；
    两阶段（先负临时值再统一落位）必须一次性成功。"""
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    assert services.commit_reorder(conn, preview.id, preview.data["token"]).ok
    assert services.get_layer(conn, g["l1"])["seq"] == 2
    assert services.get_layer(conn, g["l2"])["seq"] == 1


def test_failed_midway_update_rolls_back_entire_transaction(conn, grid_with_layers):
    """模拟第二阶段落库失败（触发器 ABORT）：不得留下任何部分更新。"""
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    # 第一阶段写负值（-2）不触发；第二阶段写最终值 2 时 ABORT
    conn.execute(
        "CREATE TRIGGER fail_on_final BEFORE UPDATE ON layers "
        "WHEN new.seq = 2 AND old.seq < 0 "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    result = services.commit_reorder(conn, preview.id, preview.data["token"])
    assert not result.ok and "回滚" in result.error

    # 全部顺序保持原值，预览仍在（DELETE 也随事务回滚）
    assert services.get_layer(conn, g["l1"])["seq"] == 1
    assert services.get_layer(conn, g["l2"])["seq"] == 2
    assert services.get_layer(conn, g["l3"])["seq"] == 3
    assert services.get_reorder_preview(conn, preview.id, preview.data["token"]) is not None
    conn.execute("DROP TRIGGER fail_on_final")


# ---------------------------------------------------------------- 陈旧预览

def test_stale_preview_rejected_after_layer_change(conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 1, g["l2"]: 2, g["l3"]: 3}
    )
    # 预览后开放新层位 → 指纹变化
    services.create_layer(conn, g["grid"], "L4", 4, "新层", "", "", "t")
    stale = services.commit_reorder(conn, preview.id, preview.data["token"])
    assert not stale.ok and "重新预览" in stale.error
    assert services.get_layer(conn, g["l1"])["seq"] == 1


def test_stale_preview_rejected_after_relation_change(conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    # 预览后新增覆盖关系 L2→L3（新顺序下 1<3 仍合法），但关系集合变了
    services.add_relation(conn, g["grid"], g["l2"], g["l3"], "后补", "t")
    assert not services.commit_reorder(conn, preview.id, preview.data["token"]).ok

    again = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    assert services.commit_reorder(conn, again.id, again.data["token"]).ok


def test_preview_token_required_and_single_use(conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 1, g["l2"]: 2, g["l3"]: 3}
    )
    # 错误/缺失令牌
    assert not services.commit_reorder(conn, preview.id, "wrong-token").ok
    # 成功提交后令牌失效，不能重复使用
    assert services.commit_reorder(conn, preview.id, preview.data["token"]).ok
    assert not services.commit_reorder(conn, preview.id, preview.data["token"]).ok

    # 新预览取代旧预览（同探方只保留最新一份）
    newer = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 1, g["l2"]: 2, g["l3"]: 3}
    )
    assert services.get_reorder_preview(conn, preview.id, preview.data["token"]) is None
    assert services.get_reorder_preview(conn, newer.id, newer.data["token"]) is not None


# ---------------------------------------------------------------- 已关闭层位 / 后续录入

def test_closed_layer_participates_but_status_and_artifacts_unchanged(conn, grid_with_layers):
    g = grid_with_layers
    services.register_artifact(conn, g["l2"], "陶片", 5, "关闭前登记", "t")
    services.close_layer(conn, g["l2"], "t")

    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    assert services.commit_reorder(conn, preview.id, preview.data["token"]).ok

    layer2 = services.get_layer(conn, g["l2"])
    assert layer2["seq"] == 1 and layer2["status"] == "closed"
    assert layer2["closed_at"]  # 关闭时间保留
    # 遗物归属不变
    batches = [
        b for b in services.list_batches_for_grid(conn, g["grid"])
        if b["layer_id"] == g["l2"]
    ]
    assert len(batches) == 1 and batches[0]["count"] == 5
    # 关闭状态仍然禁止补录
    rejected = services.register_artifact(conn, g["l2"], "骨器", 1, "补录", "t")
    assert not rejected.ok


def test_subsequent_relation_entry_validated_against_new_order(conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"])

    # 新顺序下 L2(1) 覆盖 L1(2) 合法
    ok = services.add_relation(conn, g["grid"], g["l2"], g["l1"])
    assert ok.ok and not ok.data.get("duplicated")
    # 反向（L1 seq2 → L2 seq1）违反最新顺序，拒绝并存草稿
    bad = services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    assert not bad.ok and "发掘顺序" in bad.error and bad.draft_id


# ---------------------------------------------------------------- 审计 / 回放

def test_audit_log_records_before_after_for_every_layer(conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"], actor="张田野")

    row = conn.execute(
        "SELECT * FROM audit_logs WHERE action='reorder_layers' AND success=1"
    ).fetchone()
    assert row is not None and row["actor"] == "张田野"
    details = json.loads(row["details"])
    by_code = {c["code"]: c for c in details["changes"]}
    assert by_code["L1"]["old_seq"] == 1 and by_code["L1"]["new_seq"] == 2
    assert by_code["L2"]["old_seq"] == 2 and by_code["L2"]["new_seq"] == 1
    assert by_code["L3"]["old_seq"] == 3 and by_code["L3"]["new_seq"] == 3


def test_rejected_reorder_audited_and_not_applied(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 3, g["l2"]: 2, g["l3"]: 1}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"])
    bad = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action='reorder_layers' AND success=0"
    ).fetchone()["c"]
    assert bad == 1
    assert services.get_layer(conn, g["l1"])["seq"] == 1


def test_replay_old_frames_show_old_seq_new_frames_show_new_seq(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l3"], "压覆", "t")
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"], "t")

    state = replay.replay_grid(conn, g["grid"])
    events = state["events"]
    reorder_events = [e for e in events if e["action"] == "reorder_layers"]
    assert len(reorder_events) == 1 and reorder_events[0]["success"]

    reorder_idx = events.index(reorder_events[0])
    def seqs(event):
        return {n["code"]: n["seq"] for n in event["graph"]["nodes"]}

    # 调整前的旧帧：旧顺序
    assert seqs(events[reorder_idx - 1]) == {"L1": 1, "L2": 2, "L3": 3}
    # 调整帧及其后的新帧：新顺序
    assert seqs(events[reorder_idx]) == {"L1": 2, "L2": 1, "L3": 3}
    assert seqs(events[-1]) == {"L1": 2, "L2": 1, "L3": 3}


def test_replay_failed_reorder_keeps_state_and_marks_conflict(conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 3, g["l2"]: 2, g["l3"]: 1}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"])

    events = replay.replay_grid(conn, g["grid"])["events"]
    bad = [e for e in events if e["action"] == "reorder_layers"]
    assert bad and bad[0]["conflict"]
    status = {n["code"]: n["seq"] for n in bad[0]["graph"]["nodes"]}
    assert status == {"L1": 1, "L2": 2, "L3": 3}  # 拒绝事件不改变状态


# ---------------------------------------------------------------- HTTP

def test_reorder_full_http_flow(client, conn, grid_with_layers):
    g = grid_with_layers
    # 工作台包含调整入口
    page = client.get(f"/grids/{g['grid']}").get_data(as_text=True)
    assert "整体调整发掘顺序" in page

    resp = client.post(
        f"/grids/{g['grid']}/reorder/preview",
        data={f"seq_{g['l1']}": "2", f"seq_{g['l2']}": "1", f"seq_{g['l3']}": "3",
              "actor": "t"},
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/reorder/" in location

    preview_page = client.get(location).get_data(as_text=True)
    assert "新旧发掘顺序对照" in preview_page
    assert "L1" in preview_page and "1 → 2" in preview_page

    # 从预览页提取提交地址与令牌
    token_row = conn.execute("SELECT id, token FROM reorder_previews").fetchone()
    commit_resp = client.post(
        f"/grids/{g['grid']}/reorder/{token_row['id']}/commit",
        data={"token": token_row["token"], "actor": "t"},
    )
    assert commit_resp.status_code == 302
    assert services.get_layer(conn, g["l1"])["seq"] == 2
    assert services.get_layer(conn, g["l2"])["seq"] == 1


def test_conflict_preview_page_blocks_submit(client, conn, grid_with_layers):
    g = grid_with_layers
    services.add_relation(conn, g["grid"], g["l1"], g["l2"])
    resp = client.post(
        f"/grids/{g['grid']}/reorder/preview",
        data={f"seq_{g['l1']}": "3", f"seq_{g['l2']}": "2", f"seq_{g['l3']}": "1"},
    )
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "覆盖冲突" in page
    assert "disabled" in page  # 提交按钮禁用


def test_preview_page_bad_token_redirects(client, grid_with_layers):
    g = grid_with_layers
    resp = client.post(
        f"/grids/{g['grid']}/reorder/preview",
        data={f"seq_{g['l1']}": "1", f"seq_{g['l2']}": "2", f"seq_{g['l3']}": "3"},
    )
    location = resp.headers["Location"]
    preview_id = location.rstrip("/").split("/")[-1].split("?")[0]
    bad = client.get(f"/grids/{g['grid']}/reorder/{preview_id}?token=nope")
    assert bad.status_code == 302


def test_audit_export_contains_reorder(client, conn, grid_with_layers):
    g = grid_with_layers
    preview = services.preview_reorder(
        conn, g["grid"], {g["l1"]: 2, g["l2"]: 1, g["l3"]: 3}
    )
    services.commit_reorder(conn, preview.id, preview.data["token"], "t")
    body = client.get("/audit/export").get_data(as_text=True)
    assert "reorder_layers" in body
    assert "整体调整" in body
