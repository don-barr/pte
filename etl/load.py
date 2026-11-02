"""Load an index file's in-network files into SQLite: source files, plans and flattened rates."""

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Callable, Iterable

from etl.schema import create_schema, local_path_for

BATCH_SIZE = 50_000

# Exit codes of `python -m etl.load`.
EXIT_ALL_LOADED = 0
EXIT_BAD_INDEX = 2
EXIT_SOME_FAILED = 3
EXIT_NONE_LOADED = 4
EXIT_CODES_HELP = f"""exit codes:
  {EXIT_ALL_LOADED}  every in-network file loaded
  {EXIT_BAD_INDEX}  the index could not be read or failed schema validation (nothing written)
  {EXIT_SOME_FAILED}  some files loaded and some failed
  {EXIT_NONE_LOADED}  no file loaded
"""

RATE_COLUMNS = (
    "source_file_id", "billing_code", "billing_code_type", "billing_code_type_version",
    "name", "description", "negotiated_type", "negotiated_rate", "billing_class", "setting",
    "service_codes", "modifiers", "expiration_date", "provider_reference_id", "npi", "ein",
    "business_name", "additional_information",
)
INSERT_RATE = (
    f"INSERT INTO rates ({', '.join(RATE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in RATE_COLUMNS)})"
)
PLAN_FIELDS = ("plan_id", "plan_id_type", "plan_name", "plan_market_type", "issuer_name")


class LoadError(Exception):
    """A file that parsed but can't be loaded; the message is meant for the user as is."""


class NothingLoaded(LoadError):
    """A file parsed but gave no rates because every provider it needs is elsewhere."""


class NotInNetwork(LoadError):
    """The parsed JSON is not an in-network file (e.g. an index given as one)."""

    def __init__(self):
        super().__init__("not an in-network file: no in_network list")


NO_RATES_WARNING = "no rates in file"


def provider_lookup(data: dict) -> dict[int, list[tuple[list[int | None], str, str]]]:
    """Reference id -> [(npis, ein, business_name)] per provider group.

    NPI 0 means "no NPI" and is dropped; a group with no real NPI gets [None] so it
    still produces one row. A reference kept in a remote file (location, no
    provider_groups) is left out: see remote_reference_count.
    """
    lookup = {}
    for ref in data.get("provider_references", []):
        if _is_remote(ref):
            continue
        lookup[ref["provider_group_id"]] = _groups(ref["provider_groups"])
    return lookup


def remote_reference_count(data: dict) -> int:
    """Provider references whose groups live in a remote file (location); these are not fetched."""
    return sum(1 for ref in data.get("provider_references", []) if _is_remote(ref))


def _is_remote(ref: dict) -> bool:
    return "provider_groups" not in ref and "location" in ref


def _groups(provider_groups: list[dict]) -> list[tuple[list[int | None], str, str]]:
    groups = []
    for group in provider_groups:
        npis = [npi for npi in group.get("npi") or [] if npi != 0] or [None]
        tin = group.get("tin") or {}
        groups.append((npis, tin.get("value"), tin.get("business_name")))
    return groups


def _rate_groups(rate: dict, providers: dict) -> list[tuple[int | None, list]]:
    """(reference id, groups) for a negotiated rate: its referenced groups (unknown ids and
    remote references skipped) and any inline provider_groups, with reference id None."""
    if "provider_references" not in rate and "provider_groups" not in rate:
        raise KeyError("provider_references")
    refs = [(ref, providers[ref]) for ref in rate.get("provider_references") or [] if ref in providers]
    if rate.get("provider_groups"):
        refs.append((None, _groups(rate["provider_groups"])))
    return refs


def rate_rows(source_file_id: int, data: dict):
    """One row per negotiated price x provider group x NPI. Unknown reference ids are skipped;
    a rate's inline provider_groups expand the same way, with provider_reference_id NULL."""
    providers = provider_lookup(data)
    for item in data.get("in_network", []):
        item_fields = (
            source_file_id, item["billing_code"], item.get("billing_code_type"),
            item.get("billing_code_type_version"), item.get("name"), item.get("description"),
        )
        for rate in item.get("negotiated_rates", []):
            refs = _rate_groups(rate, providers)
            for price in rate["negotiated_prices"]:
                price_fields = (
                    price.get("negotiated_type"), price["negotiated_rate"],
                    price.get("billing_class"), price.get("setting"),
                    # Sorted, so the same set listed in another order ("GT,95" vs "95,GT") is one group.
                    ",".join(sorted(price.get("service_code") or [])),
                    ",".join(sorted(price.get("billing_code_modifier") or [])),
                    price.get("expiration_date"),
                )
                for ref, groups in refs:
                    for npis, ein, business_name in groups:
                        for npi in npis:
                            yield item_fields + price_fields + (
                                ref, npi, ein, business_name, price.get("additional_information"))


def load_in_network(conn: sqlite3.Connection, source_file_id: int, data: dict,
                    on_rows: Callable[[int], None] | None = None) -> int:
    """Insert the rates of one parsed in-network file; returns the row count. No commit.
    on_rows(rows so far) is called before the first batch (0) and after each one (#42.2).

    Raises NotInNetwork unless data has an in_network list (an empty one is fine).
    """
    if not isinstance(data, dict) or not isinstance(data.get("in_network"), list):
        raise NotInNetwork()
    rows = rate_rows(source_file_id, data)
    count = 0
    if on_rows:
        on_rows(0)
    while batch := list(islice(rows, BATCH_SIZE)):
        conn.executemany(INSERT_RATE, batch)
        count += len(batch)
        if on_rows:
            on_rows(count)
    return count


def index_plans(index: dict) -> dict[str, list[dict]]:
    """In-network URL -> plans of every reporting structure listing it, in index order."""
    files: dict[str, list[dict]] = {}
    for structure in index["reporting_structure"]:
        for file in structure.get("in_network_files", []):
            files.setdefault(file["location"], []).extend(structure.get("reporting_plans", []))
    return files


def _reset_source(
    conn: sqlite3.Connection, url: str, local_path: Path | None, plans: list[dict],
    existing_id: int | None,
) -> int:
    """Give url a clean source_files row (reusing existing_id, its rates and plans deleted)
    and insert its plans. Runs inside the caller's transaction."""
    local_path = str(local_path) if local_path else None
    if existing_id is None:
        source_file_id = conn.execute(
            "INSERT INTO source_files (url, local_path) VALUES (?, ?)", (url, local_path)
        ).lastrowid
    else:
        source_file_id = existing_id
        conn.execute("DELETE FROM rates WHERE source_file_id = ?", (source_file_id,))
        conn.execute("DELETE FROM plans WHERE source_file_id = ?", (source_file_id,))
        conn.execute(
            "UPDATE source_files SET local_path = ?, reporting_entity_name = NULL, "
            "reporting_entity_type = NULL, version = NULL, last_updated_on = NULL, "
            "loaded_at = NULL, rate_count = 0, error = NULL, warnings = NULL WHERE id = ?",
            (local_path, source_file_id),
        )
    conn.executemany(
        f"INSERT INTO plans (source_file_id, {', '.join(PLAN_FIELDS)}) "
        f"VALUES (?, {', '.join('?' for _ in PLAN_FIELDS)})",
        [(source_file_id, *(plan.get(f) for f in PLAN_FIELDS)) for plan in plans],
    )
    return source_file_id


def load_file(
    conn: sqlite3.Connection, url: str, local_path: Path | None, plans: list[dict],
    content: bytes | None = None, on_rows: Callable[[int], None] | None = None,
) -> tuple[int, int, str | None]:
    """Load one in-network file in a single transaction; returns (source_file_id, rates, error).

    The file is read from local_path, or parsed from content when given (a file fetched
    straight from the web, with local_path None).

    A URL already in source_files is reloaded in place: its row is reused and its old rates
    and plans replaced. On any read, parse or shape error the transaction rolls back: a new
    URL still gets a source_files row (with its plans) carrying the error, while a URL that
    was already loaded keeps its previous data untouched. JSON with no in_network list is
    such an error; an empty in_network list loads 0 rates with the warning "no rates in file".
    on_rows: see load_in_network.
    """
    row = conn.execute("SELECT id FROM source_files WHERE url = ?", (url,)).fetchone()
    existing_id = row[0] if row else None
    try:
        if content is None:
            with local_path.open() as f:
                data = json.load(f)
        else:
            data = json.loads(content)
        with conn:
            source_file_id = _reset_source(conn, url, local_path, plans, existing_id)
            count = load_in_network(conn, source_file_id, data, on_rows)
            warnings = _remote_warning(remote_reference_count(data))
            if count == 0 and warnings:
                raise NothingLoaded(f"no rates loaded: {warnings}")
            if count == 0:
                warnings = NO_RATES_WARNING
            conn.execute(
                "UPDATE source_files SET reporting_entity_name = ?, reporting_entity_type = ?, "
                "version = ?, last_updated_on = ?, loaded_at = ?, rate_count = ?, warnings = ? "
                "WHERE id = ?",
                (
                    data.get("reporting_entity_name"), data.get("reporting_entity_type"),
                    data.get("version"), data.get("last_updated_on"),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), count, warnings,
                    source_file_id,
                ),
            )
        return source_file_id, count, None
    except Exception as e:  # any read, parse or shape error: record it, keep going
        error = str(e) if isinstance(e, LoadError) else f"{type(e).__name__}: {e}"
        if existing_id is not None:
            return existing_id, 0, error
        with conn:
            source_file_id = _reset_source(conn, url, local_path, plans, None)
            conn.execute("UPDATE source_files SET error = ? WHERE id = ?", (error, source_file_id))
        return source_file_id, 0, error


def record_fetch_failure(
    conn: sqlite3.Connection, url: str, local_path: Path | None, plans: list[dict], error: str,
) -> int | None:
    """Record that url's file could not be fetched: a source_files row (with its plans)
    carrying error and no rates. Returns its id, or None when nothing was written.

    A URL that already has loaded rates is left untouched (None): a failed re-fetch never
    wipes a good earlier load. A URL whose row has none (e.g. an earlier failure) reuses it.
    """
    row = conn.execute("SELECT id, rate_count FROM source_files WHERE url = ?", (url,)).fetchone()
    if row is not None:
        has_rates = row[1] or conn.execute(
            "SELECT 1 FROM rates WHERE source_file_id = ? LIMIT 1", (row[0],)
        ).fetchone()
        if has_rates:
            return None
    with conn:
        source_file_id = _reset_source(conn, url, local_path, plans, row[0] if row else None)
        conn.execute("UPDATE source_files SET error = ? WHERE id = ?", (error, source_file_id))
    return source_file_id


def _remote_warning(remote: int) -> str | None:
    if not remote:
        return None
    noun = "reference" if remote == 1 else "references"
    return f"skipped {remote} provider {noun} stored in a remote file (location), not fetched"


def load_index(
    conn: sqlite3.Connection, index: dict, raw_dir: Path, urls: Iterable[str] | None = None
) -> None:
    """Load the in-network files the index references (only those in urls, if given).

    Each URL gets a source_files row and its plans rows, then its local file (from
    local_path_for) is parsed and its rates inserted. A missing or unparseable file sets
    source_files.error and is skipped; the others still load. A URL already loaded is
    reloaded in place.
    """
    wanted = None if urls is None else set(urls)
    for url, plans in index_plans(index).items():
        if wanted is None or url in wanted:
            load_file(conn, url, local_path_for(url, raw_dir), plans)


def load_summary(results: list[tuple[str, str | None]]) -> tuple[int, str]:
    """(exit code, one-line summary) from each file's (url, error or None)."""
    loaded = sum(1 for _, error in results if error is None)
    failed = len(results) - loaded
    line = f"{loaded} of {len(results)} files loaded"
    if failed:
        line += f": {failed} failed, see the errors above"
    if loaded == 0:
        return EXIT_NONE_LOADED, line
    return (EXIT_SOME_FAILED if failed else EXIT_ALL_LOADED), line


def main() -> None:
    from etl.indexes import IndexStore
    from etl.run import IndexReadError, read_index, run_load
    from etl.validate import validate_index

    parser = argparse.ArgumentParser(
        description="Build an index's SQLite database from the index file and its in-network "
        "files, downloading any in-network file not already in --raw-dir. One database per index "
        "(--parsed-dir/<index file name>.db, made the active one), unless --db names another file. "
        "An existing database is backed up to --backup-dir and replaced only if a file loaded.",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--index", required=True, help="index JSON file: a local path or an http(s) URL"
    )
    parser.add_argument(
        "--index-dir", type=Path, default=Path("data/raw/index_files"),
        help="folder an index given by URL is downloaded into (default: %(default)s)",
    )
    parser.add_argument(
        "--raw-dir", type=Path, default=Path("data/raw/in_network_files"),
        help="folder holding the downloaded in-network files (default: %(default)s)",
    )
    parser.add_argument(
        "--parsed-dir", type=Path, default=Path("data/parsed"),
        help="folder of the index databases (default: %(default)s)",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="SQLite file to (re)build instead (default: <parsed-dir>/<index file name>.db)",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=Path("data/parsed/backups"),
        help="where the database being replaced is copied first (default: %(default)s)",
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="only use in-network files already in --raw-dir; never download",
    )
    args = parser.parse_args()

    start = time.perf_counter()
    try:
        index, index_path = read_index(args.index, args.index_dir)
    except IndexReadError as e:
        parser.exit(EXIT_BAD_INDEX, f"error: {e}\n")
    problems = validate_index(index)
    if problems:
        parser.exit(
            EXIT_BAD_INDEX,
            f"error: {args.index} does not match the CMS index schema:\n"
            + "".join(f"  {p}\n" for p in problems),
        )
    store = IndexStore(args.parsed_dir, args.backup_dir)
    db = args.db or store.db_for(index_path)
    urls = list(index_plans(index))
    result = run_load(index, urls, db, args.raw_dir, "replace", args.backup_dir,
                      download=not args.no_download, index_location=str(index_path))
    for f in result["files"]:
        if f["downloaded"]:
            print(f"downloaded {f['file']}")
        print(f"{f['file']}: ERROR {f['error']}" if f["error"] else f"{f['file']}: {f['rate_count']:,} rates")
    if result["backup_path"]:
        print(f"Backed up the previous database to {result['backup_path']}")
    if result["replaced"]:
        if args.db is None:
            store.set_active(db.name)
        print(f"Done in {time.perf_counter() - start:.1f}s -> {db}"
              + (" (now the active index)" if args.db is None else ""))
    else:
        print(f"Done in {time.perf_counter() - start:.1f}s: no file loaded, {db} unchanged")
    code, summary = load_summary([(f["url"], f["error"]) for f in result["files"]])
    print(summary)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
