"""Providers (#23): organizations by EIN, with their names, NPIs and the codes they have rates for; the queries
behind Prices By Provider (ui.provider_page, #40) and the /api/providers JSON API.

An EIN is who gets paid; one EIN often carries several business names (a health system's departments, a
network's practices) that are separately priced, so names are listed separately, never merged. Rates with no
EIN are left out of the list.

Prices only compare within one code and one like-for-like group (billed as, procedure, modifiers, billing class,
place of service), so nothing here shows a price range across codes: a name's lowest and highest rate would span
a $5 lab test and a $1,400 procedure. A code's range is within that code, and code_breakdown splits each code into
its groups with every name's median there, to show whether names under one EIN price the same service differently.
"""

import json
import os
import re
import sqlite3

from api.search import file_sql

PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
# A code's breakdown lists at most this many names per like-for-like group ("and N more").
NAMES_CAP = 10

# How a rate was billed: its own code ("CPT 99213"), or a revenue code with four digits ("RC 0510"); the same
# expression as api.search.BILLED_AS, so groups here read like the search page's.
BILLED_AS = ("CASE WHEN billing_code_type = 'RC' THEN 'RC ' || substr('0000' || billing_code, -4)"
             " ELSE billing_code_type || ' ' || billing_code END")
# A like-for-like group within one code: billed as, procedure, modifiers, billing class, place of service.
GROUP_COLUMNS = ("billed_as", "procedure", "modifiers", "billing_class", "service_codes")
GROUP_SQL = (BILLED_AS, "COALESCE(additional_information, '')", "COALESCE(modifiers, '')",
             "COALESCE(billing_class, '')", "COALESCE(service_codes, '')")

PROVIDER_SORTS = {
    "ein": lambda p: p["ein"],
    "name": lambda p: p["names"][0].strip().casefold() if p["names"] else "",
    "names": lambda p: p["name_count"],
    "npis": lambda p: p["npi_count"],
    "codes": lambda p: p["code_count"],
    "rates": lambda p: p["rate_count"],
}

# Company suffixes and filler words ignored when matching names, so "Conifer Park Inc" finds "Conifer Park, Inc.".
IGNORED_WORDS = {"the", "and", "inc", "incorporated", "llc", "pllc", "pc", "llp", "corp", "co", "ltd"}


def normalize_name(name: str) -> str:
    """Lower case, "&" and punctuation dropped, company suffixes and "the"/"and" removed."""
    words = re.sub(r"[^\w\s]", " ", (name or "").casefold().replace("&", " ")).split()
    return " ".join(w for w in words if w not in IGNORED_WORDS)


def normalize_ein(raw: str) -> str | None:
    """"134194078" or "13-4194078" -> "13-4194078"; None if it isn't nine digits."""
    digits = re.sub(r"[\s-]", "", raw or "")
    if not re.fullmatch(r"\d{9}", digits):
        return None
    return f"{digits[:2]}-{digits[2:]}"


def list_providers(conn: sqlite3.Connection, source_file_ids: list[int] | None = None) -> list[dict]:
    """One row per EIN, sorted by EIN: its names (sorted), and how many names, NPIs, rates and codes it has.
    No price range: it would span every code. Rates with no EIN are left out. source_file_ids: only rates in
    those files (None: every file)."""
    files, file_args = file_sql(source_file_ids)
    cursor = conn.execute(
        "SELECT ein, json_group_array(DISTINCT business_name), COUNT(DISTINCT npi), COUNT(*),"
        " COUNT(DISTINCT billing_code || '|' || COALESCE(billing_code_type, ''))"
        f" FROM rates WHERE ein IS NOT NULL{files} GROUP BY ein ORDER BY ein", file_args
    )
    providers = []
    for ein, names_json, npis, rates, codes in cursor:
        names = sorted(n for n in json.loads(names_json) if n is not None)
        providers.append({
            "ein": ein, "names": names, "name_count": len(names), "npi_count": npis, "rate_count": rates,
            "code_count": codes,
        })
    return providers


def eins_for_npi(conn: sqlite3.Connection, q: str, source_file_ids: list[int] | None = None) -> set[str]:
    """The EINs that have rates for this NPI, when q is one (ten digits); otherwise empty. Uses the NPI index."""
    q = (q or "").strip()
    if not re.fullmatch(r"\d{10}", q):
        return set()
    files, file_args = file_sql(source_file_ids)
    return {ein for (ein,) in conn.execute(
        f"SELECT DISTINCT ein FROM rates WHERE npi = ? AND ein IS NOT NULL{files}", (int(q), *file_args))}


def filter_providers(providers: list[dict], q: str, npi_eins: set[str] | None = None) -> list[dict]:
    """Providers matching q: the EIN (with or without its dash), any of its names (ignoring case, punctuation
    and company suffixes, see normalize_name), or an NPI. NPIs aren't in the list, so the caller looks the NPI
    up (eins_for_npi) and passes the EINs that have it."""
    q = (q or "").strip()
    if not q:
        return providers
    npi_eins = npi_eins or set()
    # "13-4" matches the EIN as written; "134194078" matches it without its dash.
    ein_part = q if re.fullmatch(r"[\d-]+", q) else None
    needle = normalize_name(q)
    # A query that is only a suffix ("pllc") normalizes to nothing: match it literally instead.
    literal = q.casefold() if not needle else None

    def matches(p: dict) -> bool:
        if p["ein"] in npi_eins:
            return True
        if ein_part and ein_part in (p["ein"] if "-" in ein_part else p["ein"].replace("-", "")):
            return True
        if needle:
            return any(needle in normalize_name(n) for n in p["names"])
        return any(literal in n.casefold() for n in p["names"])

    return [p for p in providers if matches(p)]


def matched_names(provider: dict, q: str) -> list[str]:
    """The provider's names that q names (as filter_providers matches them), most relevant for showing why it was
    found; all its names when q is empty or matched its EIN or an NPI."""
    needle = normalize_name(q or "")
    if not (q or "").strip() or re.fullmatch(r"[\d\s-]+", q.strip()):
        return provider["names"]
    if needle:
        return [n for n in provider["names"] if needle in normalize_name(n)] or provider["names"]
    return [n for n in provider["names"] if q.strip().casefold() in n.casefold()] or provider["names"]


def sort_providers(providers: list[dict], sort: str, direction: str) -> list[dict]:
    key = PROVIDER_SORTS[sort]
    return sorted(providers, key=lambda p: (key(p) is None, key(p) or 0, p["ein"]), reverse=direction == "desc")


def provider_detail(conn: sqlite3.Connection, ein: str, name: str | None = None,
                    source_file_ids: list[int] | None = None) -> dict | None:
    """One EIN: its names (each with its own counts, most rates first), its NPIs (with the names each appears
    under) and the codes it has rates for (most rates first), each with its rate count, how many names bill it
    and its lowest and highest rate within that code. With name, the codes are only that name's, counted for
    that name (names and NPIs stay whole). source_file_ids: only rates in those files (None: every file). None
    if the EIN has no rates (in those files). A code's multi_type is true when its
    number is also another type of code (731: CPT and RC). code_breakdown adds each code's groups."""
    ein = normalize_ein(ein)
    if ein is None:
        return None
    files, file_args = file_sql(source_file_ids)
    names = [
        {"name": n, "rate_count": rates, "npi_count": npis, "code_count": codes}
        for n, rates, npis, codes in conn.execute(
            "SELECT business_name, COUNT(*), COUNT(DISTINCT npi),"
            " COUNT(DISTINCT billing_code || '|' || COALESCE(billing_code_type, ''))"
            f" FROM rates WHERE ein = ?{files} GROUP BY business_name ORDER BY COUNT(*) DESC, business_name",
            (ein, *file_args))
    ]
    if not names:
        return None
    npis: dict = {}
    for npi, npi_name, rates in conn.execute(
        f"SELECT npi, business_name, COUNT(*) FROM rates WHERE ein = ?{files} GROUP BY npi, business_name"
        " ORDER BY npi IS NULL, npi, business_name", (ein, *file_args)
    ):
        entry = npis.setdefault(npi, {"npi": npi, "rate_count": 0, "names": []})
        entry["rate_count"] += rates
        if npi_name is not None:
            entry["names"].append(npi_name)
    name_sql, name_args = (" AND business_name = ?", (name,)) if name else ("", ())
    codes = [
        {"billing_code": code, "billing_code_type": code_type, "name": code_name, "rate_count": rates,
         "name_count": name_count, "min_rate": low, "max_rate": high}
        for code, code_type, code_name, rates, name_count, low, high in conn.execute(
            "SELECT billing_code, billing_code_type, MIN(name), COUNT(*), COUNT(DISTINCT business_name),"
            " MIN(negotiated_rate), MAX(negotiated_rate)"
            f" FROM rates WHERE ein = ?{name_sql}{files} GROUP BY billing_code, billing_code_type"
            " ORDER BY COUNT(*) DESC, billing_code, billing_code_type", (ein, *name_args, *file_args))
    ]
    numbers = sorted({c["billing_code"] for c in codes})
    multi = set()
    for start in range(0, len(numbers), 500):
        chunk = numbers[start:start + 500]
        multi |= {code for (code,) in conn.execute(
            f"SELECT billing_code FROM rates WHERE billing_code IN ({', '.join('?' * len(chunk))}){files}"
            " GROUP BY billing_code HAVING COUNT(DISTINCT billing_code_type) > 1", (*chunk, *file_args))}
    for c in codes:
        c["multi_type"] = c["billing_code"] in multi
    return {
        "ein": ein, "name": name or None, "rate_count": sum(n["rate_count"] for n in names), "names": names,
        "npis": list(npis.values()), "codes": codes,
    }


def counted_median(counts: dict[float, int]) -> float:
    """The median of values given as {value: how many times}: statistics.median of them written out."""
    values = sorted(counts)
    n = sum(counts.values())

    def at(i: int) -> float:  # the i-th smallest (from 0)
        for v in values:
            if i < counts[v]:
                return v
            i -= counts[v]
        raise IndexError(i)

    return at(n // 2) if n % 2 else (at(n // 2 - 1) + at(n // 2)) / 2


def code_breakdown(conn: sqlite3.Connection, ein: str, codes: list[dict], names_cap: int = NAMES_CAP,
                   source_file_ids: list[int] | None = None) -> list[dict]:
    """Adds "groups" to each of these codes (provider_detail's): its like-for-like groups under this EIN (billed
    as, procedure, modifiers, billing class, place of service), most rates first, and
    in each, every name with its median price and rate count, most rates first, at most names_cap of them
    (name_count has them all, more_names the ones left out); breakdown_name_count is how many names bill the
    code across its groups. Every name under the EIN is listed, even when the
    codes were picked for one name, so its prices can be compared. source_file_ids: only rates in those files
    (None: every file). Returns codes."""
    ein = normalize_ein(ein)
    files, file_args = file_sql(source_file_ids)
    wanted = {(c["billing_code"], c["billing_code_type"]) for c in codes}
    prices: dict = {}  # (code, type) -> group key -> name -> {rate: how many rows}
    numbers = sorted({code for code, _ in wanted})
    for start in range(0, len(numbers), 500):
        chunk = numbers[start:start + 500]
        # Grouped by price too: a network's 150,000 rows come back as a few hundred.
        for code, code_type, *key, name, rate, count in conn.execute(
            f"SELECT billing_code, billing_code_type, {', '.join(GROUP_SQL)}, business_name, negotiated_rate,"
            f" COUNT(*) FROM rates WHERE ein = ? AND billing_code IN ({', '.join('?' * len(chunk))}){files}"
            " GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9", (ein, *chunk, *file_args)
        ):
            if (code, code_type) in wanted:
                prices.setdefault((code, code_type), {}).setdefault(tuple(key), {}).setdefault(name, {})[rate] = count
    for c in codes:
        groups = []
        for key, by_name in prices.get((c["billing_code"], c["billing_code_type"]), {}).items():
            names = sorted(({"name": n, "median": counted_median(r), "rate_count": sum(r.values())}
                            for n, r in by_name.items()),
                           key=lambda n: (-n["rate_count"], n["name"] is None, n["name"] or ""))
            group = dict(zip(GROUP_COLUMNS, key))
            group.update(rate_count=sum(n["rate_count"] for n in names), name_count=len(names),
                         names=names[:names_cap], more_names=max(len(names) - names_cap, 0))
            groups.append(group)
        groups.sort(key=lambda g: (-g["rate_count"], *(g[k] for k in GROUP_COLUMNS)))
        c["groups"] = groups
        c["breakdown_name_count"] = len({n for by_name in prices.get((c["billing_code"], c["billing_code_type"]), {})
                                         .values() for n in by_name})
    return codes


def page_bounds(limit: str | int | None, offset: str | int | None) -> tuple[int, int]:
    """(limit, offset) from query values, leniently: anything that isn't a whole number falls back to the
    default (100, 0), and limit is kept within 1–500."""
    def whole(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    limit = min(max(whole(limit, PAGE_SIZE), 1), MAX_PAGE_SIZE)
    return limit, max(whole(offset, 0), 0)


def db_file_version(conn: sqlite3.Connection):
    """(path, mtime) of the connection's DB file: changes whenever the file is reloaded."""
    path = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    try:
        return path, os.stat(path).st_mtime_ns
    except OSError:
        return path, None


_provider_lists: dict = {}


def cached_providers(conn: sqlite3.Connection, source_file_ids: list[int] | None = None) -> list[dict]:
    """list_providers, read once per version of the database file and file set (grouping 1M+ rows takes about
    0.7 s); shared by the Prices By Provider list and /api/providers."""
    version = db_file_version(conn)
    key = None if source_file_ids is None else tuple(sorted(source_file_ids))
    if version[1] is None or _provider_lists.get("version") != version:
        _provider_lists.clear()
        _provider_lists["version"] = version
    if version[1] is None or key not in _provider_lists:
        _provider_lists[key] = list_providers(conn, source_file_ids)
    return _provider_lists[key]
