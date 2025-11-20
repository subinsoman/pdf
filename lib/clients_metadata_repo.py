from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    MetaData,
    select,
    func,
)
from sqlalchemy.engine import Engine, Result


class ClientsMetadataRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.metadata = MetaData()
        self.table = Table(
            "n8n_clients_metadata",
            self.metadata,
            Column("sl_no", Integer, primary_key=True, autoincrement=True),
            Column("client_name", String(50)),
            Column("client_email", String(50)),
            Column("client_id", String(100)),
            Column("sessionId", String(100)),
            Column("msg_id", String(100)),
            Column("knowledge_id", String(100)),
            Column("time", String(100)),
            Column("msg_count", Integer),
            extend_existing=True,
        )
        # Do not create the table here; assume it already exists in the DB

    def _row_to_dict(self, row) -> Dict[str, Any]:
        return dict(row._mapping)

    def list_all(self) -> List[Dict[str, Any]]:
        stmt = select(self.table).order_by(self.table.c.sl_no.desc())
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            rows = result.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        stmt = select(self.table).order_by(self.table.c.sl_no.desc()).limit(limit)
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            rows = result.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_distinct_sessions(self) -> int:
        stmt = select(func.count(func.distinct(self.table.c.sessionId)))
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            val = result.scalar() or 0
        return int(val)

    def total_requests(self) -> int:
        # Total requests = total records in n8n_clients_metadata
        stmt = select(func.count()).select_from(self.table)
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            val = result.scalar() or 0
        return int(val)

    def count_unique_users(self) -> int:
        # Treat empty strings as NULL for uniqueness
        col = func.nullif(self.table.c.client_email, "")
        stmt = select(func.count(func.distinct(col)))
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            val = result.scalar() or 0
        return int(val)

    def most_used_knowledge(self) -> Optional[Dict[str, Any]]:
        # Returns top knowledge by total rows; also includes msgs_sum for reference
        stmt = (
            select(
                self.table.c.knowledge_id,
                func.count().label("rows_count"),
                func.coalesce(func.sum(self.table.c.msg_count), 0).label("msgs_sum"),
            )
            .where(self.table.c.knowledge_id.isnot(None))
            .group_by(self.table.c.knowledge_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            row = result.fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        return {
            "knowledge_id": d.get("knowledge_id"),
            "rows_count": int(d.get("rows_count") or 0),
            "msgs_sum": int(d.get("msgs_sum") or 0),
        }
