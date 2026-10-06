"""审计日志写入。"""

import json
from datetime import datetime, timezone

from flask import g


def now_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(
    conn,
    action: str,
    entity: str,
    entity_id: int | None = None,
    grid_id: int | None = None,
    actor: str = "",
    success: bool = True,
    summary: str = "",
    details: dict | None = None,
    commit: bool = True,
) -> int:
    """写一条审计记录，返回其 id。调用方若在同一事务中可传 commit=False。"""
    cur = conn.execute(
        """INSERT INTO audit_logs
               (ts, action, entity, entity_id, grid_id, actor, success, summary, details)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            now_ts(),
            action,
            entity,
            entity_id,
            grid_id,
            actor,
            1 if success else 0,
            summary,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid
