"""层位图（DAG）布局计算：为同一探方的层位与“覆盖”边计算 SVG 坐标。

约定：图上边方向 upper(覆盖方) -> lower(被覆盖方)。
页面按发掘顺序自上而下排列（先发掘/上层在顶部）。
"""

X_CENTER = 240
X_GAP = 150          # 同序列多列时的横向间距
Y_TOP = 60
Y_GAP = 110
NODE_W = 110
NODE_H = 46


def _node_positions(layers: list[dict]) -> dict[int, tuple[float, float, int]]:
    """返回 layer_id -> (cx, cy, seq)。同一 seq 横向排开。"""
    by_seq: dict[int, list[dict]] = {}
    for layer in layers:
        by_seq.setdefault(layer["seq"], []).append(layer)

    pos: dict[int, tuple[float, float, int]] = {}
    for row, seq in enumerate(sorted(by_seq)):
        group = by_seq[seq]
        n = len(group)
        for col, layer in enumerate(group):
            cx = X_CENTER + (col - (n - 1) / 2) * X_GAP
            cy = Y_TOP + row * Y_GAP
            pos[layer["id"]] = (cx, cy, seq)
    return pos


def build_graph(layers: list[dict], relations: list[dict]) -> dict:
    """构造可直接渲染的图结构。

    返回 dict：
      nodes: [{id, code, seq, name, status, x, y}]
      edges: [{id, x1, y1, x2, y2, upper, lower, note}]
      width, height
    """
    pos = _node_positions(layers)

    nodes = []
    for layer in layers:
        cx, cy, _ = pos[layer["id"]]
        nodes.append(
            {
                "id": layer["id"],
                "code": layer["code"],
                "seq": layer["seq"],
                "name": layer["name"],
                "status": layer["status"],
                "x": cx,
                "y": cy,
            }
        )

    edges = []
    for rel in relations:
        if rel["upper_id"] not in pos or rel["lower_id"] not in pos:
            continue
        x1, y1, _ = pos[rel["upper_id"]]
        x2, y2, _ = pos[rel["lower_id"]]
        edges.append(
            {
                "id": rel["id"],
                "x1": x1,
                "y1": y1 + NODE_H / 2,
                "x2": x2,
                "y2": y2 - NODE_H / 2,
                "upper": rel["upper_id"],
                "lower": rel["lower_id"],
                "note": rel.get("note", ""),
            }
        )

    xs = [n["x"] for n in nodes] or [X_CENTER]
    height = (len({l["seq"] for l in layers})) * Y_GAP + Y_TOP
    width = max(480, (max(xs) - min(xs)) + 260)
    return {"nodes": nodes, "edges": edges, "width": width, "height": max(height, 180)}


def node_box():
    return NODE_W, NODE_H
