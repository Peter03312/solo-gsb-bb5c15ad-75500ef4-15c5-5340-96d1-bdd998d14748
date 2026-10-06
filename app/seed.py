"""首次初始化时写入演示数据：一个完整探方 + 一个必然冲突的草稿。"""

from . import services


def seed_if_empty(conn) -> None:
    if conn.execute("SELECT COUNT(*) c FROM grids").fetchone()["c"] > 0:
        return

    # 探方 T01：L1(表土,最先发掘) 覆盖 L2 覆盖 L3(生土,最后发掘)
    g = services.create_grid(conn, "T01", "半坡遗址（演示）", "演示数据：探方 T01", "张田野")
    gid = g.id
    l1 = services.create_layer(conn, gid, "L1", 1, "耕土/表土层", "现代耕土层", "松散灰褐土", "张田野").id
    l2 = services.create_layer(conn, gid, "L2", 2, "文化层（灰褐土）", "仰韶文化晚期", "含陶片、红烧土", "张田野").id
    l3 = services.create_layer(conn, gid, "L3", 3, "生土层", "新石器时代早期", "致密黄土，出土石器", "张田野").id

    services.add_relation(conn, gid, l1, l2, "直接压覆", "张田野")
    services.add_relation(conn, gid, l2, l3, "直接压覆", "张田野")

    services.register_artifact(conn, l2, "陶片", 36, "彩陶腹片为主，可辨器形为钵", "李器物")
    services.register_artifact(conn, l3, "石器", 2, "磨制石斧残件", "李器物")

    # 关闭 L2：之后补录遗物会被拒绝（审计中可见）
    services.close_layer(conn, l2, "张田野")
    services.register_artifact(conn, l2, "骨器", 1, "（尝试在关闭后补录，应被拒绝）", "李器物")

    # 人为制造一个必成环的关系草稿：已有 L1→L2→L3，再登记 L3 覆盖 L1 即成环
    services.add_relation(conn, gid, l3, l1, "误录：下层覆盖上层", "张田野")

    # 第二个探方，便于演示回放与筛选
    g2 = services.create_grid(conn, "T02", "半坡遗址（演示）", "演示数据：探方 T02", "王考古")
    services.create_layer(conn, g2.id, "L1", 1, "表土层", "现代", "", "王考古")
