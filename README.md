# 考古发掘层位记录系统

用于田野考古发掘中记录 **探方（Grid）→ 层位（Layer）→ 遗物批次（Artifact Batch）** 的 Web 应用。
系统按“覆盖 / 被覆盖”关系维护一张**层位有向无环图（DAG）**，并提供发掘顺序校验、
冲突路径展示、草稿保留、按探方回放层位演变与审计记录导出。

技术栈：**Python 3.11 · Flask · SQLite（零外部服务）· 原生 SVG 渲染（无前端构建）· Gunicorn · Docker / Compose**

---

## 一、核心业务规则

| 规则 | 实现位置 |
| --- | --- |
| 每个探方中层位编号唯一、发掘顺序号唯一（正整数，**先发掘/上层数值小**） | `app/services.py: create_layer` |
| 覆盖关系方向：`覆盖方（upper）→ 被覆盖方（lower）`，且两端必须属于同一探方 | `add_relation` |
| **顺序约束**：`upper.seq < lower.seq`，即覆盖方必须先发掘 | `add_relation` 第一道校验 |
| **无环约束（DAG）**：BFS 检测 `lower → … → upper` 已可达时拒绝（第二道防线） | `app/services.py: _find_path` |
| 发现矛盾时**不写入**关系，原始录入**保留为草稿**并显示**冲突路径** | `drafts` 表 / 冲突草稿页 |
| 遗物只能登记到**当前开放（open）**层位；层位**关闭（closed）后禁止补录** | `register_artifact` |
| 田野整理可**整体调整一个探方的发掘顺序**：编号恰好覆盖现存层位、新顺序号为互不重复的正整数、覆盖方新顺序号必须小于被覆盖方；**先预览、凭当次预览一次性提交** | `app/services.py: preview_reorder_layers / commit_reorder_layers` |
| 顺序调整后**预览即失效**：预览后层位（含状态）或覆盖关系发生变化时，旧提交被拒绝，须重新预览 | `commit_reorder_layers`（状态指纹 + 签名令牌） |
| 顺序调整在**单个事务内两阶段更新**（先让出正整数序位再写入），不会因 `UNIQUE(grid_id, seq)` 留下部分更新；审计记录每个层位变更前后顺序 | `commit_reorder_layers` |
| 一切成功与被拒绝的写操作都落入 `audit_logs`（成功/失败均留痕；纯预览不写库、不留痕） | `app/audit.py` |
| 回放与导出均以审计日志为唯一事实源，逐条重放重建当时图状态；顺序调整事件**之前的帧显示旧顺序、之后的帧显示新顺序** | `app/replay.py` |

> 说明：在“seq 严格沿覆盖方向递增”的不变量下，顺序校验本身即可阻止成环；
> BFS 环检测作为第二道防线保留（例如历史数据顺序号未重排时仍能兜住）。
> 被拒绝的事件保留在回放时间线上，但不改变图状态。

## 二、功能页面

- `/` 探方总览：统计、新建探方、最近操作
- `/grids/<id>` 探方工作台：
  - 层位图 SVG（开放层位绿色 / 已关闭灰色，箭头为覆盖方向）
  - 开放层位、登记覆盖关系、关闭层位
  - 「**⇅ 整体调整发掘顺序**」：两步式改序（录入新顺序号 → 预览新旧顺序与受影响覆盖关系 → 凭当次预览提交）
  - 遗物批次登记表单（下拉中**只出现开放层位**）
  - 覆盖关系与遗物批次清单
- `/grids/<id>/layers/reorder` **发掘顺序整体调整**：
  - 为每个现存层位（含已关闭层位）填写新顺序号；
  - 预览页列出各层位变更前后顺序、每条覆盖关系的新顺序校验；
  - 凡覆盖方新顺序号 **≥** 被覆盖方，逐条列出冲突关系并禁止应用；
  - 仅当次预览携带的签名令牌可提交；预览后层位/覆盖关系变化则旧令牌失效，需重新预览；
  - 已关闭层位参与改序但状态与遗物归属不变；改序后关系录入按最新顺序校验。
- `/conflicts` **冲突草稿**：展示冲突路径（如 `L1 → L2 → L3 → L1（成环）`）、
  原始表单、拒绝原因；可“重新校验并提交”或“丢弃”（均留痕）
- `/grids/<id>/replay` **按探方回放层位演变**：逐步前进/后退、跳转，
  每一步显示该时刻的图快照（开放/关闭层位、覆盖边数），冲突事件标红
- `/audit` 审计记录：按探方 / 操作类型 / 关键词 / 仅看冲突筛选
- `/audit/export` 一键导出当前筛选结果为 **UTF-8 BOM 的 CSV**（Excel 可直接打开）

## 三、一键启动（Docker Compose，推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

首次启动会自动建表；由于 Compose 中 `SEED_DEMO=1`，数据库为空时会写入演示数据：

- 探方 **T01**：层位 `L1(顺序1,表土) → L2(顺序2,文化层) → L3(顺序3,生土)`，
  含覆盖关系、遗物批次；L2 已关闭并附带一条“关闭后补录被拒绝”的审计；
  另有一条顺序冲突关系 `L3 → L1` 保留为草稿（可在“冲突草稿”页看到）。
- 探方 **T02**：仅一个开放层位，便于看空白图与回放。

数据持久化在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`）。

停止 / 清除：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷（清空所有记录）
```

## 四、本地开发运行（不使用 Docker）

需要 Python 3.11+。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export FLASK_APP=app
export DATABASE_PATH="$PWD/archaeo.db"
export SEED_DEMO=1            # 可选：写入演示数据

flask init-db                 # 建表（SEED_DEMO=1 时附带演示数据）
flask run --debug --port 8000
```

生产方式（容器内同此命令）：

```bash
gunicorn --bind 0.0.0.0:8000 --workers 2 "app:create_app()"
```

## 五、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 项目目录下 `archaeo.db`（镜像内 `/data/archaeo.db`） | SQLite 数据库文件路径，父目录自动创建 |
| `SEED_DEMO` | `0`（Compose 中为 `1`） | 仅在数据库为空时写入演示数据；已有数据不受影响 |
| `SECRET_KEY` | 内置开发默认值 | Flask 会话密钥，**生产部署请通过环境变量覆盖** |
| `GUNICORN_WORKERS` | `2` | Gunicorn worker 数 |

## 六、运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：顺序冲突、重复关系幂等、跨探方关系拒绝、DAG 环检测（第二道防线）、
草稿重试/丢弃、关闭层位补录拒绝、回放快照重建、HTTP 全流程与 CSV 导出，
以及发掘顺序整体调整（编号恰好覆盖/正整数互不重复校验、覆盖冲突列举与禁止、
两阶段原子提交不产生部分更新、预览后变化导致旧提交失效、已关闭层位参与、
改序后关系校验、调整前后顺序审计与回放旧/新帧）。

## 七、目录结构

```
app/
  __init__.py      Flask 应用工厂 / 配置 / init-db 命令
  schema.sql       表结构与索引（grids / layers / layer_relations /
                   artifact_batches / drafts / audit_logs）
  db.py            SQLite 连接、建表
  audit.py         审计日志写入（统一 UTC ISO8601 时间戳）
  services.py      核心业务逻辑与全部校验（探方/层位/关系/遗物/草稿）
  graph.py         层位图布局：为节点和覆盖边计算 SVG 坐标
  replay.py        基于审计日志逐步重建图状态（回放）
  routes.py        页面路由与 CSV 导出
  seed.py          演示数据
  templates/       Jinja2 模板（含 _graph.html 层位图宏）
  static/style.css 样式
tests/             pytest 测试
Dockerfile         python:3.11-slim + gunicorn
docker-compose.yml 一键启动（含数据卷与 SEED_DEMO）
entrypoint.sh      启动前 flask init-db，再拉起 gunicorn
```

## 八、使用流程示例（典型田野工作流）

1. 新建探方 `T03`；
2. 按发掘进展依次开放层位 `L1(顺序1) → L2(顺序2) → L3(顺序3)`；
3. 随时登记覆盖关系 `L1→L2`、`L2→L3`（系统即时校验顺序与无环）；
4. 发掘某层期间，把遗物批次登记到该**开放**层位；
5. 该层发掘结束 → **关闭层位**，此后任何人尝试补录都会被拒绝并记入审计；
6. 若误录了反向/成环关系，在“冲突草稿”页查看冲突路径，待修正后重试或丢弃；
7. 田野整理阶段如需**整体重排发掘顺序**（如改用 10/20/30 编号、纠正层位误判）：
   在探方工作台点「⇅ 整体调整发掘顺序」→ 为所有现存层位填新顺序号 → **先看预览**
   （新旧顺序、受影响覆盖关系；有冲突会逐条列出并禁止应用）→ 凭当次预览一次性提交。
   调整在单事务内原子生效，审计中记录每个层位变更前后顺序；预览后若有人改动了层位或
   覆盖关系，提交会被拒绝并要求重新预览；已关闭层位可参与，状态与遗物归属不变；
8. 通过“回放”向检查方展示层位演变过程（调整事件旧帧为旧顺序、新帧为新顺序），
   或在“审计记录”按操作类型“整体调整顺序”筛选并导出 CSV 归档。
