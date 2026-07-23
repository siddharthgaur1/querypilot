"""Shared pytest fixtures for QueryPilot tests."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_db(tmp_path) -> Path:
    """A small SQLite DB with one table, for schema/execution tests."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT
        );
        INSERT INTO customers VALUES (1, 'Alice', 'Mumbai'), (2, 'Bob', 'Delhi');
        CREATE TABLE accounts (
            account_id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            balance REAL
        );
        INSERT INTO accounts VALUES (1, 1, 100.0), (2, 2, 50.0);
    """)
    conn.commit()
    conn.close()
    return db_path
