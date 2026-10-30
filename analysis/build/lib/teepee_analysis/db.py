"""DuckDB connection with the teepee SQLite file attached as `tp`."""
import os
from pathlib import Path

import duckdb


def data_dir() -> Path:
    return Path(os.environ.get("TEEPEE_DATA_DIR", Path.home() / ".local/share/teepee_typing"))


def connect(db: Path | None = None) -> duckdb.DuckDBPyConnection:
    db = db or data_dir() / "teepee.db"
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{db}' AS tp (TYPE sqlite, READ_ONLY);")
    return con
