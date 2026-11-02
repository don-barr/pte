"""What an index database holds, read-only, for the Load Data page: its files with their load results, its rate
and code counts, and the index it was built from. Every read opens the file read-only and treats a file that isn't
an index database (no source_files table) as nothing."""

import sqlite3
from contextlib import closing
from pathlib import Path

from api.search import file_name
from etl.run import read_index_info


def read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)


def load_status(db_path: Path) -> dict | None:
    """What the database holds, even when no rate loaded (every file failed): {"rate_count", "code_count",
    "source_files": [{"id", "file", "url", "last_updated_on", "loaded_at", "rate_count", "error", "warnings"}]}.
    None when there is no database with a source_files table at db_path (or no db_path)."""
    if db_path is None:
        return None
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    try:
        with closing(read_only(db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
            if not columns:
                return None
            warnings = "warnings" if "warnings" in columns else "NULL"  # databases from before the column
            rows = conn.execute(
                f"SELECT id, url, last_updated_on, loaded_at, rate_count, error, {warnings} FROM source_files"
                " ORDER BY id").fetchall()
            rates, codes = conn.execute("SELECT COUNT(*), COUNT(DISTINCT billing_code) FROM rates").fetchone()
    except sqlite3.Error:
        return None
    files = [{"id": sid, "file": file_name(url), "url": url, "last_updated_on": updated, "loaded_at": loaded,
              "rate_count": count, "error": error, "warnings": warn}
             for sid, url, updated, loaded, count, error, warn in rows]
    return {"rate_count": rates, "code_count": codes, "source_files": files}


def source_rows(db_path: Path | None) -> dict[str, dict] | None:
    """url -> {"id", "rate_count", "loaded_at", "error", "warnings", "plan_count"} for every file in the database at
    db_path (failed loads too), read-only; None when there is no database with a source_files table there."""
    if db_path is None or not Path(db_path).is_file():
        return None
    try:
        with closing(read_only(db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
            if not columns:
                return None
            warnings = "s.warnings" if "warnings" in columns else "NULL"
            return {url: {"id": sid, "rate_count": count, "loaded_at": loaded_at, "error": error, "warnings": warn,
                          "plan_count": plans}
                    for sid, url, count, loaded_at, error, warn, plans in conn.execute(
                        f"SELECT s.id, s.url, s.rate_count, s.loaded_at, s.error, {warnings},"
                        " (SELECT COUNT(*) FROM plans p WHERE p.source_file_id = s.id) FROM source_files s"
                        " ORDER BY s.id")}
    except sqlite3.Error:
        return None


def index_info(db_path: Path) -> dict | None:
    """The index the database was built from (etl.run.read_index_info), or None for a database from before it was
    recorded, or one that can't be read."""
    try:
        with closing(read_only(db_path)) as conn:
            return read_index_info(conn)
    except sqlite3.Error:
        return None
