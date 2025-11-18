from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    MetaData,
    select,
    insert,
    update,
    delete,
)
from sqlalchemy.engine import Engine, Result


class PdfMetadataRepository:
    """Repository for CRUD operations on the pdf_metadata table.

    Expected table structure:
      id          INT (PK, auto-increment)
      name        VARCHAR
      operation   VARCHAR
      description TEXT
      pdf_path    VARCHAR
      created_by  VARCHAR
      created_at  DATETIME
      updated_by  VARCHAR
      updated_at  DATETIME
      status      VARCHAR
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.metadata = MetaData()

        self.table = Table(
            "pdf_metadata",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("name", String(255), nullable=False),
            Column("operation", String(100), nullable=True),
            Column("description", Text, nullable=True),
            Column("pdf_path", String(500), nullable=True),
            Column("created_by", String(100), nullable=True),
            Column("created_at", DateTime, nullable=True),
            Column("updated_by", String(100), nullable=True),
            Column("updated_at", DateTime, nullable=True),
            Column("status", String(50), nullable=True),
            extend_existing=True,
        )

        # Create table if it does not exist. In production you might manage this via migrations instead.
        self.metadata.create_all(self.engine, tables=[self.table], checkfirst=True)

    # ---------------------- Helpers ----------------------

    def _row_to_dict(self, row) -> Dict[str, Any]:
        return dict(row._mapping)  # SQLAlchemy 1.4+/2.0 style

    # ---------------------- CRUD Methods ----------------------

    def list_all(self) -> List[Dict[str, Any]]:
        stmt = select(self.table).order_by(self.table.c.created_at.desc())
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            rows = result.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, id: int) -> Optional[Dict[str, Any]]:
        stmt = select(self.table).where(self.table.c.id == id)
        with self.engine.connect() as conn:
            result: Result = conn.execute(stmt)
            row = result.fetchone()
        return self._row_to_dict(row) if row else None

    def insert(self, data: Dict[str, Any]) -> int:
        now = datetime.utcnow()
        if not data.get("created_at"):
            data["created_at"] = now
        if not data.get("updated_at"):
            data["updated_at"] = now

        stmt = insert(self.table).values(**data)
        with self.engine.begin() as conn:
            result: Result = conn.execute(stmt)
            new_id = result.inserted_primary_key[0]
        return int(new_id)

    def update(self, id: int, data: Dict[str, Any]) -> None:
        data = dict(data)  # shallow copy
        data["updated_at"] = data.get("updated_at") or datetime.utcnow()

        stmt = (
            update(self.table)
            .where(self.table.c.id == id)
            .values(**data)
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def delete(self, id: int) -> None:
        stmt = delete(self.table).where(self.table.c.id == id)
        with self.engine.begin() as conn:
            conn.execute(stmt)
