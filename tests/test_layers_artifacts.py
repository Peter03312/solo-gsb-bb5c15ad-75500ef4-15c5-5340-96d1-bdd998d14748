"""层位开放/关闭与遗物归属规则。"""

from app import services


def test_layer_seq_and_code_unique_in_grid(conn, grid_with_layers):
    g = grid_with_layers
    dup_code = services.create_layer(conn, g["grid"], "L1", 9, "", "", "", "t")
    dup_seq = services.create_layer(conn, g["grid"], "L9", 1, "", "", "", "t")
    assert not dup_code.ok
    assert not dup_seq.ok


def test_artifact_requires_open_layer(conn, grid_with_layers):
    g = grid_with_layers
    ok = services.register_artifact(conn, g["l2"], "陶片", 10, "彩陶片", "t")
    assert ok.ok and ok.data["batch_no"].startswith("BT01-L2-")

    # 关闭 L2 后禁止补录
    assert services.close_layer(conn, g["l2"], "t").ok
    rejected = services.register_artifact(conn, g["l2"], "骨器", 1, "补录", "t")
    assert not rejected.ok
    assert "关闭" in rejected.error and "补录" in rejected.error

    # L2 下仍只有关闭前的 1 个批次
    batches = services.list_batches_for_grid(conn, g["grid"])
    l2_batches = [b for b in batches if b["layer_id"] == g["l2"]]
    assert len(l2_batches) == 1

    # 拒绝事件进入审计
    bad = conn.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE action='register_artifact' AND success=0"
    ).fetchone()["c"]
    assert bad == 1


def test_close_already_closed_layer(conn, grid_with_layers):
    g = grid_with_layers
    services.close_layer(conn, g["l1"], "t")
    again = services.close_layer(conn, g["l1"], "t")
    assert not again.ok


def test_other_open_layer_still_accepts_artifacts(conn, grid_with_layers):
    g = grid_with_layers
    services.close_layer(conn, g["l1"], "t")
    assert services.register_artifact(conn, g["l2"], "石器", 1, "", "t").ok
