"""DuckDB connection with the teepee SQLite file attached as `tp`."""
import os
import shutil
import subprocess
from pathlib import Path

import duckdb


def data_dir() -> Path:
    """`$TEEPEE_DATA_DIR`, else whatever the installed `teepee` binary says."""
    if env := os.environ.get("TEEPEE_DATA_DIR"):
        return Path(env)
    exe = shutil.which("teepee") or str(Path.home() / ".cargo/bin/teepee")
    return Path(subprocess.run([exe, "data"], check=True, capture_output=True, text=True).stdout.strip())


def connect(db: Path | None = None) -> duckdb.DuckDBPyConnection:
    db = db or data_dir() / "teepee.db"
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{db}' AS tp (TYPE sqlite, READ_ONLY);")
    return con
