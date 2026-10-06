"""回放重建与 HTTP/导出路由冒烟测试。"""

import io

from app import replay, services


def _setup_demo(conn):
    gid = services.create_grid(conn, "T01", "遗址", "", "张").id
    l1 = services.create_layer(conn, gid, "L1", 1, "表土", "", "", "张").id
    l2 = services.create_layer(conn, gid, "L2", 2, "文化层", "", "", "张").id
    services.add_relation(conn, gid, l1, l2, "压覆", "张")
    services.register_artifact(conn, l2, "陶片", 5, "", "李")
    services.close_layer(conn, l2, "张")
    # 一次冲突：顺序非法
    services.add_relation(conn, gid, l2, l1, "误录", "张")
    return gid


def test_replay_snapshots_reflect_history(conn):
    gid = _setup_demo(conn)
    state = replay.replay_grid(conn, gid)
    events = state["events"]
    assert state["total"] >= 6

    # 找到关系建立事件：其后边数为 1
    edge_events = [e for e in events if e["action"] == "add_relation" and e["success"]]
    assert edge_events and edge_events[0]["edge_count"] == 1

    # 关闭事件后 closed 计数 +1
    close_events = [e for e in events if e["action"] == "close_layer"]
    assert close_events[-1]["closed_count"] == 1
    assert close_events[-1]["open_count"] == 1

    # 冲突事件被标记且不改变边数
    conflicts = [e for e in events if e["conflict"]]
    assert conflicts and all(e["edge_count"] == 1 for e in conflicts)

    # 最终图中 L2 为关闭样式
    final = events[-1]
    statuses = {n["code"]: n["status"] for n in final["graph"]["nodes"]}
    assert statuses["L2"] == "closed"
    assert statuses["L1"] == "open"


def test_index_and_grid_pages(client, app):
    with app.app_context():
        from app.db import get_db
        _setup_demo(get_db())

    assert client.get("/").status_code == 200
    assert client.get("/grids/1").status_code == 200
    assert client.get("/grids/1/replay").status_code == 200
    assert client.get("/audit").status_code == 200
    assert client.get("/conflicts").status_code == 200


def test_full_conflict_flow_over_http(client, app):
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        gid = services.create_grid(conn, "T09", "", "", "t").id
        l1 = services.create_layer(conn, gid, "L1", 1, "", "", "", "t").id
        l2 = services.create_layer(conn, gid, "L2", 2, "", "", "", "t").id

    # 顺序非法的关系录入：302 跳转到冲突页
    resp = client.post(
        f"/grids/{gid}/relations",
        data={"upper_id": l2, "lower_id": l1, "actor": "t"},
    )
    assert resp.status_code == 302
    assert "/conflicts" in resp.headers["Location"]

    conflicts_page = client.get("/conflicts").get_data(as_text=True)
    assert "冲突路径" in conflicts_page
    assert "L2" in conflicts_page


def test_audit_export_csv(client, app):
    with app.app_context():
        from app.db import get_db
        _setup_demo(get_db())

    resp = client.get("/audit/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "时间(UTC)" in body
    assert "add_relation" in body  # 动作列
    # 含拒绝记录
    assert "拒绝" in body
