"""Backend of "Load new data": read an index, list its files, load chosen files into the DB."""

import functools
import json
import ntpath
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from etl.fetch import download_url, fetch_bytes, fetch_urls
from etl.load import index_plans, load_file, record_fetch_failure
from etl.schema import create_schema, display_name, local_path_for
from etl.validate import validate_index

MODES = ("add", "replace")
# Where each in-network file comes from: the copy in raw_dir, a fresh download kept in
# raw_dir, or the web straight into memory (nothing saved).
SOURCES = ("local", "download", "web")
DOWNLOADS_DISABLED = "not downloaded (downloads disabled)"
# Files SQLite keeps beside a database while writing (or after a crash mid-write). They are
# found by name, so one left beside db_path would be applied to whatever file is there next.
SIDE_FILES = ("-journal", "-wal", "-shm")
# Beside a database while a Replace load builds its successor: what is being loaded, by which process (#42.3).
MARKER_SUFFIX = ".loading"
BUSY_TIMEOUT = 30.0
# The index's own header fields recorded in each index database (write_index_info).
INDEX_HEADER_FIELDS = ("reporting_entity_name", "reporting_entity_type", "last_updated_on", "version")
INDEX_PLAN_FIELDS = ("plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name")
# How much of an in-network file read_file_header reads: CMS files put their scalar fields
# (reporting_entity_name, version, ...) before the big arrays, so they are in the first few bytes.
HEADER_BYTES = 64 * 1024
_WHITESPACE = re.compile(r"[ \t\n\r]*")


class IndexReadError(Exception):
    """The index could not be read; the message is meant for the user."""


def _is_url(location: str) -> bool:
    return urlparse(location).scheme in ("http", "https")


def read_index(location: str, index_dir: Path) -> tuple[dict, Path]:
    """Parse an index from a local path, or from a URL downloaded into index_dir.

    A URL whose file is already in index_dir is not downloaded again. A download that
    turns out not to be a valid index is deleted so a retry fetches it afresh.
    """
    location = location.strip()
    downloaded = False
    if _is_url(location):
        (result,) = fetch_urls([location], Path(index_dir))
        if result.error:
            raise IndexReadError(f"Could not download {location}: {result.error}")
        path, downloaded = result.path, result.downloaded
    else:
        path = Path(location).expanduser()
        if not path.is_file():
            raise IndexReadError(f"Index file not found: {path}")
    try:
        return _parse_index(path), path
    except IndexReadError:
        if downloaded:
            path.unlink(missing_ok=True)
        raise


def _parse_index(path: Path) -> dict:
    try:
        with path.open() as f:
            index = json.load(f)
    except json.JSONDecodeError as e:
        raise IndexReadError(f"{path.name} is not valid JSON: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise IndexReadError(f"Could not read {path}: {e}") from e
    return _check_index(index, path.name)


def parse_index_bytes(name: str, content: bytes) -> dict:
    """An uploaded index, checked before anything is saved: raises IndexReadError like read_index."""
    try:
        index = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise IndexReadError(f"{name} is not valid JSON: {e}") from e
    return _check_index(index, name)


def _check_index(index, name: str) -> dict:
    if not isinstance(index, dict) or not isinstance(index.get("reporting_structure"), list):
        raise IndexReadError(f"{name} is not an index file: it has no reporting_structure list")
    return index


def _index_counts(index: dict) -> tuple[int, int]:
    """(distinct in-network file URLs, reporting plans) of an index that may not fit the schema."""
    urls, plans = set(), 0
    for structure in index["reporting_structure"]:
        if not isinstance(structure, dict):
            continue
        files = structure.get("in_network_files")
        if isinstance(files, list):
            urls.update(f["location"] for f in files if isinstance(f, dict) and "location" in f)
        reporting_plans = structure.get("reporting_plans")
        if isinstance(reporting_plans, list):
            plans += len(reporting_plans)
    return len(urls), plans


def list_local_indexes(index_dir: Path) -> list[dict]:
    """Every *.json in index_dir, newest name first, with its counts and schema validity.

    A file that can't be read as an index is still listed, with error set.
    """
    listed = []
    for path in sorted(Path(index_dir).glob("*.json"), key=lambda p: p.name, reverse=True):
        stat = path.stat()
        entry = {
            "path": str(path), "name": path.name, "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "file_count": 0, "plan_count": 0, "valid": False, "error": None,
        }
        try:
            index = _parse_index(path)
        except IndexReadError as e:
            entry["error"] = str(e)
        else:
            entry["file_count"], entry["plan_count"] = _index_counts(index)
            entry["valid"] = not validate_index(index)
        listed.append(entry)
    return listed


def save_index_upload(filename: str, content: bytes, index_dir: Path) -> Path:
    """Save an uploaded index into index_dir under its own basename; returns the saved path.

    The same content already saved under that name is reused; a different file with the
    name gets -1, -2, ... before .json. Raises ValueError unless the name ends in .json.
    """
    name = ntpath.basename(filename or "")  # strips both / and \ directory parts
    if not name.lower().endswith(".json") or name.lower() == ".json":
        raise ValueError(f"“{filename}” is not a .json file.")
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    stem, suffix = name[:-5], name[-5:]
    path, n = index_dir / name, 0
    while path.exists() and path.read_bytes() != content:
        n += 1
        path = index_dir / f"{stem}-{n}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return path


def _file_name(url: str) -> str:
    """The readable name shown for url's file (its stored name adds a hash of the URL)."""
    return display_name(url)


def read_file_header(path: Path, limit: int = HEADER_BYTES) -> dict:
    """The top-level scalar fields (string, number, true/false/null) of the JSON object in path
    that come before its first array or object, read from its first `limit` bytes only: the
    rest of the file is neither read nor parsed. A value cut off by the end of the chunk is left
    out. {} when the file doesn't start like a JSON object."""
    with open(path, "rb") as f:
        text = f.read(limit).decode("utf-8-sig", errors="ignore")  # a character cut at the end is dropped
    decoder, fields = json.JSONDecoder(), {}

    def skip(pos: int) -> int:
        return _WHITESPACE.match(text, pos).end()

    pos = skip(0)
    if text[pos:pos + 1] != "{":
        return fields
    pos = skip(pos + 1)
    while text[pos:pos + 1] == '"':
        try:
            key, pos = decoder.raw_decode(text, pos)
        except ValueError:
            break
        pos = skip(pos)
        if text[pos:pos + 1] != ":":
            break
        pos = skip(pos + 1)
        if text[pos:pos + 1] in ("[", "{", ""):  # the big arrays start here
            break
        try:
            value, pos = decoder.raw_decode(text, pos)
        except ValueError:
            break
        pos = skip(pos)
        if text[pos:pos + 1] not in (",", "}"):  # e.g. a number cut short by the chunk's end
            break
        fields.setdefault(key, value)
        if text[pos] == "}":
            break
        pos = skip(pos + 1)
    return fields


def index_files(index: dict) -> list[dict]:
    """One entry per distinct in-network file URL, in index order, with all its plans."""
    return [
        {
            "url": url,
            "file": _file_name(url),
            "plans": [
                {f: plan.get(f) for f in INDEX_PLAN_FIELDS} for plan in plans
            ],
        }
        for url, plans in index_plans(index).items()
    ]


def write_index_info(conn: sqlite3.Connection, index: dict, location: str) -> None:
    """Record in conn's database which index it was built from (#37.5): the index's location and file name, its
    header (reporting entity, last_updated_on, version), whether it fits the CMS schema, when, and every in-network
    file it lists with its plans. Replaces what was recorded before; commits."""
    create_schema(conn)
    problems = validate_index(index)
    name = display_name(location) if _is_url(location) else Path(location).name
    with conn:
        conn.execute("DELETE FROM index_info")
        conn.execute("DELETE FROM index_in_network_files")
        conn.execute(
            "INSERT INTO index_info (id, location, file_name, reporting_entity_name, reporting_entity_type,"
            " last_updated_on, version, valid, problems, loaded_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(location), name, *(_scalar(index.get(k)) for k in INDEX_HEADER_FIELDS), int(not problems),
             json.dumps(problems), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.executemany("INSERT INTO index_in_network_files (position, url, plans) VALUES (?, ?, ?)",
                         [(n, f["url"], json.dumps(f["plans"])) for n, f in enumerate(index_files(index))])


def read_index_info(conn: sqlite3.Connection) -> dict | None:
    """What write_index_info recorded: {"location", "file_name", "reporting_entity_name", "reporting_entity_type",
    "last_updated_on", "version", "valid" (bool), "problems", "loaded_at", "files": index_files()-shaped list}, or None
    for a database with nothing recorded (e.g. one built before #37)."""
    try:
        row = conn.execute(
            "SELECT location, file_name, reporting_entity_name, reporting_entity_type, last_updated_on, version,"
            " valid, problems, loaded_at FROM index_info WHERE id = 1").fetchone()
        listed = conn.execute("SELECT url, plans FROM index_in_network_files ORDER BY position").fetchall()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    keys = ("location", "file_name", *INDEX_HEADER_FIELDS, "valid", "problems", "loaded_at")
    info = dict(zip(keys, row))
    info["valid"] = bool(info["valid"])
    info["problems"] = json.loads(info["problems"] or "[]")
    info["files"] = [{"url": url, "file": _file_name(url), "plans": json.loads(plans)} for url, plans in listed]
    return info


def _scalar(value):
    """A header value as stored: strings and numbers as they are, anything else (a list, an object) as JSON."""
    return value if value is None or isinstance(value, (str, int, float)) else json.dumps(value)


def run_load(
    index: dict,
    urls: list[str],
    db_path: Path,
    raw_dir: Path,
    mode: str,
    backup_dir: Path,
    download: bool = True,
    sources: dict[str, str] | None = None,
    index_location: str | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Fetch the chosen in-network files and load them, adding to or replacing the database.

    sources maps a URL to "local", "download" or "web" (see SOURCES); a URL not in it uses
    its local copy if there is one, else downloads and keeps it. download=False refuses
    every "download" and "web" file.

    "add" loads into db_path (created if new); a file already loaded is reloaded in place.
    "replace" builds a fresh database beside db_path and swaps it in only if at least one
    file loaded; the old database is first copied to backup_dir. Fetch and parse failures
    are reported per file, never raised. A file that could not be fetched still gets a
    source_files row carrying the error, unless (in "add") its URL already has loaded rates,
    which are kept as they were.

    index_location (the index's path) also records the index in the database (write_index_info).

    Each file is fetched and then loaded before the next one starts. progress, if given, is called with a dict per
    step as it happens (#42.2): {"stage": "downloading", "url", "bytes", "total" (None when unknown)}, then
    {"stage": "reading", "url"}, {"stage": "saving", "url", "rows" (so far), "db_bytes" (the database file's size)},
    and {"stage": "loaded", "url", "rate_count", "error"} (also for a file that failed earlier); finally, in
    "replace" when the new database is about to be swapped in, {"stage": "activating"}.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    sources = sources or {}
    for url, source in sources.items():
        if source not in SOURCES:
            raise ValueError(f"source for {url} must be one of {SOURCES}, not {source!r}")
    start = time.perf_counter()
    db_path, raw_dir, backup_dir = Path(db_path), Path(raw_dir), Path(backup_dir)
    plans_by_url = index_plans(index)
    chosen = list(dict.fromkeys(urls))

    files = {
        url: {"url": url, "file": _file_name(url), "source": _source_for(url, sources, raw_dir),
              "downloaded": False, "rate_count": 0, "plan_count": 0, "error": None}
        for url in chosen
    }
    report = progress or (lambda event: None)
    for url in chosen:
        if url not in plans_by_url:
            files[url]["error"] = "not in the index"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    replaced = False
    info = (index, index_location) if index_location is not None else None
    load = functools.partial(_load_into, chosen=chosen, plans_by_url=plans_by_url, files=files, info=info,
                             raw_dir=raw_dir, download=download, report=report)
    if mode == "add":
        load(db_path)
    else:
        # The marker says what was being loaded, so a load killed part-way can be cleaned up and reported
        # (recover_interrupted). It is written before the half-built database exists and removed after it's gone.
        marker = _marker_for(db_path)
        record = {"index_location": str(index_location) if index_location is not None else None,
                  "db": db_path.name, "pid": os.getpid(),
                  "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tmp": None}
        _write_marker(marker, record)
        tmp = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, prefix=db_path.name + ".", suffix=".new")
            os.close(fd)
            tmp = Path(tmp_name)
            _write_marker(marker, dict(record, tmp=tmp.name))
            load(tmp)
            if any(f["error"] is None for f in files.values()):
                report({"stage": "activating"})
                backup_path = _swap_in(tmp, db_path, backup_dir)
                replaced = True
        finally:
            if tmp is not None:
                _delete_with_side_files(tmp)
            marker.unlink(missing_ok=True)

    return {
        "mode": mode,
        "backup_path": str(backup_path) if backup_path else None,
        "replaced": replaced,
        "files": [files[url] for url in chosen],
        "seconds": round(time.perf_counter() - start, 3),
    }


def _marker_for(db_path: Path) -> Path:
    return db_path.parent / (db_path.name + MARKER_SUFFIX)


def _write_marker(marker: Path, record: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=marker.parent, prefix=marker.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.replace(tmp, marker)


def _delete_with_side_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    for suffix in SIDE_FILES:
        Path(str(path) + suffix).unlink(missing_ok=True)


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def recover_interrupted(parsed_dir: Path) -> list[dict]:
    """Clean up after Replace loads that were killed part-way (#42.3), e.g. by the dev server restarting: delete each
    half-built <db>.*.new (with its journal) and the marker run_load left, and return what they were, oldest first:
    [{"index" (the index file's name, else the database's stem), "index_location" (or None), "db", "at" (when the
    load last wrote anything, UTC ISO)}]. A load whose process is still alive is left alone (this one's too). The
    databases themselves are never touched: a Replace load only swaps its new database in once it is complete."""
    parsed_dir = Path(parsed_dir)
    if not parsed_dir.is_dir():
        return []
    found, busy = [], set()
    for marker in sorted(parsed_dir.glob("*" + MARKER_SUFFIX)):
        db = marker.name[:-len(MARKER_SUFFIX)]
        try:
            record = json.loads(marker.read_text())
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
        if _alive(record.get("pid")):
            busy.add(db)
            continue
        location = record.get("index_location")
        found.append({"index": Path(location).name if location else Path(db).stem, "index_location": location,
                      "db": db, "at": _last_write(parsed_dir, db, marker)})
        _delete_half_built(parsed_dir, db)
        marker.unlink(missing_ok=True)
    reported = {f["db"] for f in found}
    # Half-built files with no marker (e.g. from before markers): their database's name is in theirs.
    for path in sorted(parsed_dir.glob("*.db.*.new*")):
        match = re.fullmatch(r"(.+\.db)\.[^.]+\.new(-journal|-wal|-shm)?", path.name)
        if match is None or match.group(1) in busy or not path.exists():
            continue
        db = match.group(1)
        if db not in reported:
            reported.add(db)
            found.append({"index": Path(db).stem, "index_location": None, "db": db,
                          "at": _last_write(parsed_dir, db, None)})
        _delete_half_built(parsed_dir, db)
    return sorted(found, key=lambda f: f["at"])


def _half_built(parsed_dir: Path, db: str) -> list[Path]:
    return [p for p in parsed_dir.glob(glob_escape(db) + ".*.new*")
            if re.fullmatch(re.escape(db) + r"\.[^.]+\.new(-journal|-wal|-shm)?", p.name)]


def _delete_half_built(parsed_dir: Path, db: str) -> None:
    for path in _half_built(parsed_dir, db):
        path.unlink(missing_ok=True)


def _last_write(parsed_dir: Path, db: str, marker: Path | None) -> str:
    times = [p.stat().st_mtime for p in _half_built(parsed_dir, db) + ([marker] if marker else []) if p.exists()]
    when = datetime.fromtimestamp(max(times), timezone.utc) if times else datetime.now(timezone.utc)
    return when.isoformat(timespec="seconds")


def _source_for(url: str, sources: dict[str, str], raw_dir: Path) -> str:
    """The chosen source, else the local copy if there is one, else download and keep."""
    if url in sources:
        return sources[url]
    return "local" if local_path_for(url, raw_dir).exists() else "download"


def _fetch(entry: dict, raw_dir: Path, download: bool, on_bytes=None) -> Path | None:
    """Make entry's file ready to load: its local path ("local", "download"), or None for
    "web" (fetched later, while loading). Failures are set on entry["error"]."""
    url, source = entry["url"], entry["source"]
    if source == "local":
        path = local_path_for(url, raw_dir)
        if not path.exists():
            entry["error"] = "no local copy"
        return path
    if not download:
        entry["error"] = DOWNLOADS_DISABLED
        return None
    if source == "web":
        return None
    result = download_url(url, raw_dir, on_bytes)
    entry["downloaded"], entry["error"] = result.downloaded, result.error
    return result.path


def _load_into(db_path: Path, chosen: list[str], plans_by_url: dict, files: dict,
               info: tuple[dict, str] | None, raw_dir: Path, download: bool, report) -> None:
    """Fetch then load each chosen file in the index (one at a time), reporting progress (see run_load)."""
    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)
        if info is not None:
            write_index_info(conn, *info)
        for url in chosen:
            entry = files[url]
            if url not in plans_by_url:
                continue

            def on_bytes(received: int, total: int | None, url=url) -> None:
                report({"stage": "downloading", "url": url, "bytes": received, "total": total})

            if entry["source"] != "local" and download:
                on_bytes(0, None)  # before the server answers (it may take a while)
            path = _fetch(entry, raw_dir, download, on_bytes)
            plans = plans_by_url[url]
            content = None
            if entry["error"] is None and path is None:  # "web": one file in memory at a time
                content, entry["error"] = fetch_bytes(url, on_bytes)
            if entry["error"] is not None:
                # Recorded so health and the Load data page can show it; a URL that already has
                # loaded rates keeps them (the failure is reported in the run result only).
                kept = path if path is not None and path.exists() else None
                record_fetch_failure(conn, url, kept, plans, entry["error"])
                report({"stage": "loaded", "url": url, "rate_count": 0, "error": entry["error"]})
                continue

            def on_rows(rows: int, url=url) -> None:
                report({"stage": "saving", "url": url, "rows": rows, "db_bytes": _size(db_path)})

            report({"stage": "reading", "url": url})
            _, count, error = load_file(conn, url, path, plans, content, on_rows)
            entry["error"] = error
            entry["rate_count"] = count
            entry["plan_count"] = 0 if error else len(plans)
            report({"stage": "loaded", "url": url, "rate_count": count, "error": error})
    finally:
        conn.close()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _swap_in(new_db: Path, db_path: Path, backup_dir: Path) -> Path | None:
    """Back up db_path (if any) to backup_dir (backup_db) and atomically replace it with new_db.
    No journal/WAL file may remain beside db_path when new_db takes its name."""
    for suffix in SIDE_FILES:
        if Path(str(new_db) + suffix).exists():
            raise RuntimeError(f"{new_db.name}{suffix} exists: the new database is not complete")
    if not db_path.exists():
        _move_side_files(db_path, None)
        os.replace(new_db, db_path)
        return None
    backup = backup_db(db_path, backup_dir)
    os.replace(new_db, db_path)
    return backup


def backup_db(db_path: Path, backup_dir: Path) -> Path:
    """Copy db_path to backup_dir/<its stem>-<UTC timestamp>.db and return the copy's path; db_path stays.

    The database is opened read-write first, so SQLite rolls back any hot journal a killed
    load left behind; the backup is then taken with SQLite's backup API, so it is the last
    committed state. A file SQLite cannot open as a database is copied byte for byte instead.
    Any journal/WAL file still beside db_path is moved next to the backup, so it can never be
    applied to whatever takes db_path's name next.
    """
    db_path = Path(db_path)
    backup = _backup_name(Path(backup_dir), db_path.stem)
    try:
        conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT, isolation_level=None)
    except sqlite3.Error:
        conn = None
    try:
        try:
            # The first read on a read-write connection rolls back a hot journal (waiting
            # out a load still running); the one-step backup then copies committed pages.
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            dest = sqlite3.connect(backup)
            try:
                conn.backup(dest)
            finally:
                dest.close()
        except (sqlite3.DatabaseError, AttributeError) as e:
            if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
                raise
            # Not a database SQLite can read: keep its bytes as they are.
            backup.unlink(missing_ok=True)
            shutil.copy2(db_path, backup)
        _move_side_files(db_path, backup)
    finally:
        if conn is not None:
            conn.close()
    return backup


def _move_side_files(db_path: Path, backup: Path | None) -> None:
    """Move any journal/WAL file still beside db_path next to backup (or delete it when there
    is no backup), so it can never be applied to the database that replaces db_path."""
    for suffix in SIDE_FILES:
        side = Path(str(db_path) + suffix)
        if side.exists():
            if backup is None:
                side.unlink()
            else:
                os.replace(side, Path(str(backup) + suffix))


def _backup_name(backup_dir: Path, stem: str = "rates") -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{stem}-{stamp}.db"
    n = 1
    while backup.exists():
        backup = backup_dir / f"{stem}-{stamp}-{n}.db"
        n += 1
    return backup
