import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open a database and create the v0.1 schema if it is empty."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='funds'").fetchone():
        con.executescript(SCHEMA.read_text())
    return con
