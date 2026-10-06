-- 考古发掘层位记录系统数据库结构

CREATE TABLE IF NOT EXISTS grids (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL UNIQUE,          -- 探方编号，如 T01
    site       TEXT    NOT NULL DEFAULT '',      -- 遗址名称
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS layers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id     INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    code        TEXT    NOT NULL,                 -- 层位编号，如 L3
    seq         INTEGER NOT NULL,                 -- 发掘顺序（先发掘/上层数值小）
    name        TEXT    NOT NULL DEFAULT '',      -- 层位名称/土质描述
    period      TEXT    NOT NULL DEFAULT '',      -- 年代归属
    description TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at   TEXT    NOT NULL,
    closed_at   TEXT,
    created_by  TEXT    NOT NULL DEFAULT '',
    UNIQUE (grid_id, code),
    UNIQUE (grid_id, seq)
);

-- “覆盖/被覆盖”关系：upper 层覆盖 lower 层
-- 不变量：upper.seq < lower.seq；同探方内关系图必须无环（DAG）
CREATE TABLE IF NOT EXISTS layer_relations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id    INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    upper_id   INTEGER NOT NULL REFERENCES layers(id) ON DELETE CASCADE,
    lower_id   INTEGER NOT NULL REFERENCES layers(id) ON DELETE CASCADE,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    UNIQUE (upper_id, lower_id),
    CHECK (upper_id <> lower_id)
);

CREATE TABLE IF NOT EXISTS artifact_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_id    INTEGER NOT NULL REFERENCES layers(id) ON DELETE CASCADE,
    batch_no    TEXT    NOT NULL UNIQUE,          -- 批次号（系统生成）
    category    TEXT    NOT NULL DEFAULT '',      -- 遗物类别：陶片/石器/骨器…
    count       INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    description TEXT    NOT NULL DEFAULT '',
    recorded_by TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

-- 矛盾录入保留为草稿（关系成环 / 违反发掘顺序）
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id       INTEGER REFERENCES grids(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL CHECK (kind IN ('relation', 'artifact', 'layer')),
    payload       TEXT    NOT NULL,               -- JSON 原始表单
    reason        TEXT    NOT NULL DEFAULT '',
    conflict_path TEXT    NOT NULL DEFAULT '',    -- 形如 L3 → L2 → L1 的冲突路径
    created_at    TEXT    NOT NULL
);

-- 层位发掘顺序整体调整的预览（一次性令牌：提交后或被新版本取代后失效）
-- fingerprint 为预览时刻“层位 + 覆盖关系”的状态指纹；提交时不一致即判定预览过期
CREATE TABLE IF NOT EXISTS reorder_previews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id     INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    token       TEXT    NOT NULL UNIQUE,          -- 提交凭据（随机、不可猜测）
    fingerprint TEXT    NOT NULL,                 -- 预览时刻的状态指纹（SHA-256）
    payload     TEXT    NOT NULL,                 -- JSON：new_order + 新旧顺序/关系快照
    actor       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

-- 完整审计日志，回放与导出均以此为准
CREATE TABLE IF NOT EXISTS audit_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,                   -- UTC ISO8601
    action    TEXT    NOT NULL,
    entity    TEXT    NOT NULL,
    entity_id INTEGER,
    grid_id   INTEGER,
    actor     TEXT    NOT NULL DEFAULT '',
    success   INTEGER NOT NULL DEFAULT 1,
    summary   TEXT    NOT NULL DEFAULT '',
    details   TEXT    NOT NULL DEFAULT '{}'       -- JSON
);

CREATE INDEX IF NOT EXISTS idx_layers_grid        ON layers(grid_id);
CREATE INDEX IF NOT EXISTS idx_relations_grid     ON layer_relations(grid_id);
CREATE INDEX IF NOT EXISTS idx_batches_layer      ON artifact_batches(layer_id);
CREATE INDEX IF NOT EXISTS idx_audit_grid_ts      ON audit_logs(grid_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts           ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS idx_reorder_grid       ON reorder_previews(grid_id);
