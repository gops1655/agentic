"""SQLite log of processed emails so nothing is handled twice."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    key TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    sender TEXT,
    subject TEXT,
    pdf_count INTEGER,
    invoice_count INTEGER,
    status TEXT,
    detail TEXT,
    excel_path TEXT
)
"""


class Store:
    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)
        self.db.commit()

    def seen(self, key: str) -> bool:
        row = self.db.execute("SELECT status FROM processed WHERE key = ?", (key,)).fetchone()
        return row is not None and row[0] in {"sent", "skipped"}

    def record(self, key: str, *, sender: str, subject: str, pdf_count: int, invoice_count: int,
               status: str, detail: str = "", excel_path: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, datetime.now().isoformat(timespec="seconds"), sender, subject, pdf_count,
             invoice_count, status, detail, excel_path),
        )
        self.db.commit()

    def history(self, limit: int = 25) -> list[tuple]:
        return self.db.execute(
            "SELECT processed_at, sender, subject, pdf_count, invoice_count, status, detail, excel_path "
            "FROM processed ORDER BY processed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
