import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    local_path TEXT,
    reporting_entity_name TEXT,
    reporting_entity_type TEXT,
    version TEXT,
    last_updated_on TEXT,
    loaded_at TEXT,
    rate_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    -- Set when the file loaded but part of it was skipped (e.g. remote provider references).
    warnings TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    plan_id TEXT,
    plan_id_type TEXT,
    plan_name TEXT,
    plan_market_type TEXT,
    issuer_name TEXT
);

-- One row per negotiated price x provider group x NPI. npi is NULL when the group
-- lists no real NPI (only 0). service_codes and modifiers are comma-joined.
CREATE TABLE IF NOT EXISTS rates (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    billing_code TEXT NOT NULL,
    billing_code_type TEXT,
    billing_code_type_version TEXT,
    name TEXT,
    description TEXT,
    negotiated_type TEXT,
    negotiated_rate REAL NOT NULL,
    billing_class TEXT,
    setting TEXT,
    service_codes TEXT,
    modifiers TEXT,
    expiration_date TEXT,
    provider_reference_id INTEGER,
    npi INTEGER,
    ein TEXT,
    business_name TEXT,
    -- The price's free-text additional_information. For revenue code (RC) prices Centene puts the
    -- procedure code here (CPT/HCPCS, e.g. "99213"): the RC is the hospital department, this is
    -- the procedure. NULL when the file gives none.
    additional_information TEXT
);

CREATE INDEX IF NOT EXISTS rates_billing_code ON rates(billing_code);
CREATE INDEX IF NOT EXISTS rates_npi ON rates(npi);
CREATE INDEX IF NOT EXISTS rates_ein ON rates(ein);
CREATE INDEX IF NOT EXISTS rates_source_file ON rates(source_file_id);
CREATE INDEX IF NOT EXISTS rates_additional_information ON rates(additional_information);
CREATE INDEX IF NOT EXISTS plans_source_file ON plans(source_file_id);

-- The index this database was built from (#37.5: one database per index), so the app can describe the
-- database without reading the index again. One row; written by etl.run.write_index_info.
CREATE TABLE IF NOT EXISTS index_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    location TEXT,
    file_name TEXT,
    reporting_entity_name TEXT,
    reporting_entity_type TEXT,
    last_updated_on TEXT,
    version TEXT,
    valid INTEGER,
    problems TEXT,   -- JSON list of schema problems (empty when valid)
    loaded_at TEXT
);

-- Every in-network file the index lists, loaded or not, in index order, with its plans (JSON list).
CREATE TABLE IF NOT EXISTS index_in_network_files (
    position INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    plans TEXT NOT NULL
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Databases built before source_files.warnings existed get it added (NULL for old rows).
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
    if "warnings" not in columns:
        conn.execute("ALTER TABLE source_files ADD COLUMN warnings TEXT")
        conn.commit()


_UNSAFE_CHARS = ("/", "\\", "\x00")
_MAX_STEM = 150  # keeps <stem>.<hash>.json well under the usual 255-byte name limit


def _decoded_basename(url: str) -> str:
    """The URL path's last segment, percent-decoded. It may still be unsafe (e.g. "..%2F")."""
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])


def _is_safe_name(name: str) -> bool:
    return name not in ("", ".", "..") and not any(c in name for c in _UNSAFE_CHARS)


def display_name(url: str) -> str:
    """A readable, path-safe file name for url: its decoded basename with any /, \\ or NUL
    replaced by _, or "file" when the basename is empty, "." or ".."."""
    name = _decoded_basename(url)
    for c in _UNSAFE_CHARS:
        name = name.replace(c, "_")
    return name if name not in ("", ".", "..") else "file"


def local_path_for(url: str, raw_dir: Path) -> Path:
    """Where url's file is kept in raw_dir: <readable stem>.<sha256(url)[:12]>.json, so two
    URLs with the same basename never share a file.

    A copy saved under the plain decoded basename (the old naming) is still used when it
    exists and that basename is safe. The result is always directly inside raw_dir.
    """
    raw_dir = Path(raw_dir)
    plain = _decoded_basename(url)
    if _is_safe_name(plain) and (raw_dir / plain).is_file():
        path = raw_dir / plain
    else:
        stem = display_name(url)
        if stem.lower().endswith(".json"):
            stem = stem[:-5]
        stem = stem.encode()[:_MAX_STEM].decode(errors="ignore") or "file"
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        path = raw_dir / f"{stem}.{digest}.json"
    if path.resolve().parent != raw_dir.resolve():
        raise ValueError(f"refusing a local path outside {raw_dir}: {path}")
    return path
