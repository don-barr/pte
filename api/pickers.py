"""What the two doors' pickers offer (#40): the procedures, the providers with rates for one procedure, the procedures
one provider has rates for, and a provider's prices per code against everyone's.

No dead ends: a picker only offers what has rates, and when what was typed (an NPI, an EIN, a name, a code) has
nothing for the other half of the choice, it says so at once with real alternatives that do (a "notice"). Checks
are per EIN or NPI, never per name: one EIN carries many names, and one name can span many EINs.

This is the API: it answers with facts (who was asked about, what they have instead) and no wording or links;
ui.formatting turns a notice's facts into what a page says. A notice's facts have "kind": "none" | "hospital_only" |
"unknown".
"""

import re
import sqlite3
import statistics

from api.providers import normalize_ein, normalize_name
from api.search import (
    GROUP_COLUMNS, GROUP_KEY_SQL, code_types, department_names, file_sql, normalize_code, scope_where,
)

PICKER_LIMIT = 8
# How many of a provider's other codes a notice offers.
ALTERNATIVES = 6


def sentence_case(text: str) -> str:
    """ALL-CAPS file text in sentence case; mixed case is left alone."""
    return text[:1] + text[1:].lower() if text and text == text.upper() else (text or "")


# ---------------------------------------------------------------- procedures

def procedure_list(conn: sqlite3.Connection, file_ids: list[int] | None = None) -> list[dict]:
    """One row per code and code type (731 is a CPT and an RC code): {"billing_code", "billing_code_type", "name",
    "description", "rate_count", "ein_count"}. Procedures (CPT, HCPCS) first, then hospital departments (RC), each
    most rates first. A revenue code is named by its department ("Clinic"), never by the procedure-style names
    Centene also files it under; without one it is "Revenue code 0521", and it has no description to search."""
    files, args = file_sql(file_ids)
    departments = department_names(conn)
    out = []
    for code, code_type, name, description, rates, eins in conn.execute(
            "SELECT billing_code, billing_code_type, MIN(name), MIN(description), COUNT(*), COUNT(DISTINCT ein)"
            f" FROM rates WHERE 1{files} GROUP BY billing_code, billing_code_type", args):
        if code_type == "RC":
            name, description = departments.get(code) or f"Revenue code {code:0>4}", None
        else:
            name, description = sentence_case(name), sentence_case(description)
        out.append({"billing_code": code, "billing_code_type": code_type, "name": name, "description": description,
                    "rate_count": rates, "ein_count": eins})
    out.sort(key=lambda p: (p["billing_code_type"] == "RC", -p["rate_count"], p["billing_code"]))
    return out


def match_procedures(procedures: list[dict], q: str) -> list[dict]:
    """The procedures q names, in list order within each tier: the exact code, then codes starting with q, then
    every word of q in the name or description (ignoring case). A revenue code also matches without its leading
    zeros ("0510")."""
    needle = (q or "").strip().casefold()
    if not needle:
        return list(procedures)
    code_q, words = needle.upper(), needle.split()
    exact, prefix, named = [], [], []
    for p in procedures:
        code = p["billing_code"]
        typed = code_q.lstrip("0") if p["billing_code_type"] == "RC" and code_q.isdigit() else code_q
        if code == typed:
            exact.append(p)
        elif code.startswith(typed):
            prefix.append(p)
        elif all(w in f"{p['name']} {p['description'] or ''}".casefold() for w in words):
            named.append(p)
    return exact + prefix + named


# The list's sortable columns. A text column sorts A to Z on its first click, a count most first. No sort:
# match_procedures's order.
PROCEDURE_SORTS = {
    "code": lambda p: p["billing_code"],
    "name": lambda p: p["name"].casefold(),
    "type": lambda p: p["billing_code_type"],
    "rates": lambda p: p["rate_count"],
    "eins": lambda p: p["ein_count"],
}


def procedure_sort(sort: str | None, direction: str | None) -> tuple[str, str]:
    """(sort, direction) as asked, an unknown sort as "" (the list's own order) and an unknown direction as the
    column's first click."""
    sort = sort if sort in PROCEDURE_SORTS else ""
    if direction not in ("asc", "desc"):
        direction = "desc" if sort in ("rates", "eins", "") else "asc"
    return sort, direction


def sort_procedures(procedures: list[dict], sort: str, direction: str) -> list[dict]:
    """The procedures by one column of PROCEDURE_SORTS, ties by code and code type; no sort keeps their order."""
    if sort not in PROCEDURE_SORTS:
        return list(procedures)
    tied = sorted(procedures, key=lambda p: (p["billing_code"], p["billing_code_type"]))
    return sorted(tied, key=PROCEDURE_SORTS[sort], reverse=direction == "desc")


# ---------------------------------------------------------------- providers for one procedure

def chosen_type(conn, code: str, code_type: str | None, file_ids) -> str | None:
    """The code type asked for, or the one with the most rates (what the procedure page shows)."""
    if code_type:
        return code_type
    types = code_types(conn, code, file_ids) or code_types(conn, code)
    return types[0]["billing_code_type"] if types else None


def ein_name(conn, ein: str) -> str | None:
    """The name an EIN has the most rates under."""
    row = conn.execute("SELECT business_name FROM rates WHERE ein = ? AND business_name IS NOT NULL"
                       " GROUP BY business_name ORDER BY COUNT(*) DESC, business_name LIMIT 1", (ein,)).fetchone()
    return row[0] if row else None


def counts_for(conn, code: str, code_type: str | None, column: str, value, file_ids) -> tuple[int, list]:
    """(the code's own rates for one EIN or NPI in these files, its hospital rates for the code: [(revenue code,
    count, min, max)])."""
    files, fargs = file_sql(file_ids, "r.source_file_id")
    type_sql, targs = (" AND r.billing_code_type = ?", [code_type]) if code_type else ("", [])
    own = conn.execute(f"SELECT COUNT(*) FROM rates r WHERE r.billing_code = ?{type_sql} AND r.{column} = ?{files}",
                       (code, *targs, value, *fargs)).fetchone()[0]
    hospital = [] if code_type == "RC" else conn.execute(
        "SELECT r.billing_code, COUNT(*), MIN(r.negotiated_rate), MAX(r.negotiated_rate) FROM rates r"
        f" WHERE r.billing_code_type = 'RC' AND r.additional_information = ? AND r.{column} = ?{files}"
        " GROUP BY r.billing_code ORDER BY r.billing_code", (code, value, *fargs)).fetchall()
    return own, hospital


def top_codes(conn, column: str, value, file_ids, skip: str | None = None) -> list[tuple]:
    """[(code, code type, name, rates)] of one EIN or NPI in these files, most rates first."""
    files, fargs = file_sql(file_ids)
    return [row for row in conn.execute(
        f"SELECT billing_code, billing_code_type, MIN(name), COUNT(*) FROM rates WHERE {column} = ?{files}"
        " GROUP BY billing_code, billing_code_type ORDER BY COUNT(*) DESC, billing_code", (value, *fargs))
        if row[0] != skip]


def ein_facts(conn, code: str, code_type: str | None, ein: str, file_ids) -> dict | None:
    """None when the EIN has rates for the code in scope (shown as options), else a notice's facts: unknown, or
    hospital_only (its hospital rates for the code: [(revenue code, count, min, max)]), or none (see none_facts)."""
    own, hospital = counts_for(conn, code, code_type, "ein", ein, file_ids)
    name = ein_name(conn, ein)
    if name is None and not conn.execute("SELECT 1 FROM rates WHERE ein = ? LIMIT 1", (ein,)).fetchone():
        return {"kind": "unknown", "what": "EIN", "value": ein}
    if own:
        return None
    who = {"ein": ein, "name": name}
    if hospital:
        return {"kind": "hospital_only", "code": code, "by": "ein", "value": ein, "who": who, "hospital": hospital,
                "page_ein": ein}
    return none_facts(conn, code, code_type, "ein", ein, who, file_ids, ein)


def none_facts(conn, code, code_type, column, value, who: dict, file_ids, page_ein: str | None) -> dict:
    """The EIN or NPI has no rates (nor hospital rates) for the code here: "elsewhere" (its rates for the code in
    other files, when files are chosen), "other_codes" (its top codes: [(code, name)]), "other_code_count",
    "under_other_names" (for an NPI: its EINs' rates for the code under other names, [(ein, count)])."""
    elsewhere = 0
    if file_ids:
        own, hospital = counts_for(conn, code, code_type, column, value, None)
        elsewhere = own + sum(h[1] for h in hospital)
    codes = top_codes(conn, column, value, file_ids, skip=code)
    return {"kind": "none", "code": code, "by": column, "value": value, "who": who, "here": bool(file_ids),
            "elsewhere": elsewhere, "other_codes": [(other, name) for other, _, name, _ in codes[:ALTERNATIVES]],
            "other_code_count": len(codes), "page_ein": page_ein, "under_other_names": []}


def npi_facts(conn, code, code_type, npi: int, file_ids, include_facility) -> dict | None:
    """ein_facts for an NPI; checked per NPI, then its EINs' rates for the code under other names and NPIs."""
    own, hospital = counts_for(conn, code, code_type, "npi", npi, file_ids)
    rows = conn.execute("SELECT ein, business_name, COUNT(*) FROM rates WHERE npi = ? GROUP BY 1, 2"
                        " ORDER BY COUNT(*) DESC", (npi,)).fetchall()
    if not rows:
        return {"kind": "unknown", "what": "NPI", "value": npi}
    if own:
        return None
    eins = list(dict.fromkeys(e for e, _, _ in rows if e))
    names = list(dict.fromkeys(n for _, n, _ in rows if n))
    who = {"npi": npi, "name": names[0], "ein": eins[0] if eins else None}
    page_ein = eins[0] if eins else None
    if hospital:
        return {"kind": "hospital_only", "code": code, "by": "npi", "value": npi, "who": who, "hospital": hospital,
                "page_ein": page_ein}
    facts = none_facts(conn, code, code_type, "npi", npi, who, file_ids, page_ein)
    for ein in eins:
        n, hosp = counts_for(conn, code, code_type, "ein", ein, file_ids)
        n += sum(h[1] for h in hosp) if include_facility else 0
        if n:
            facts["under_other_names"].insert(0, (ein, n))
    return facts


def providers_for_code(conn: sqlite3.Connection, code: str, q: str = "", code_type: str | None = None,
                       file_ids: list[int] | None = None, include_facility: bool = True,
                       limit: int = PICKER_LIMIT) -> dict:
    """The providers (EIN + name) with rates for this code in scope that q names (a name, ignoring case,
    punctuation and company suffixes; an EIN with or without its dash, or its first digits; a 10-digit NPI), most
    rates first: {"code", "code_type", "q", "total", "providers": [{"ein", "name", "npi" (when q is one),
    "rate_count", "npi_count", "hospital_count"}], "notice" (its facts, or None)}. With q empty, the top `limit`."""
    code = normalize_code(code)
    code_type = chosen_type(conn, code, code_type, file_ids)
    q = (q or "").strip()
    digits = re.sub(r"[\s-]", "", q)
    where, args = scope_where(code, file_ids, code_type, None, include_facility)
    hospital_sql = "SUM(r.billing_code_type = 'RC' AND r.billing_code_type IS NOT ?)"
    select = (f"SELECT r.ein, r.business_name, COUNT(*), COUNT(DISTINCT r.npi), {hospital_sql} FROM rates r"
              f" WHERE {where} AND r.ein IS NOT NULL")
    group = " GROUP BY r.ein, r.business_name ORDER BY COUNT(*) DESC, r.business_name"
    notice = None
    npi = None
    if re.fullmatch(r"\d{10}", digits) and "-" not in q:
        npi = int(digits)
        rows = conn.execute(select + " AND r.npi = ?" + group, (code_type, *args, npi)).fetchall()
        if not rows:
            notice = npi_facts(conn, code, code_type, npi, file_ids, include_facility)
    else:
        rows = conn.execute(select + group, (code_type, *args)).fetchall()
        ein = normalize_ein(q)
        if ein:
            rows = [r for r in rows if r[0] == ein]
            own = sum(r[2] - r[4] for r in rows)
            if not own:
                notice = ein_facts(conn, code, code_type, ein, file_ids)
        elif re.fullmatch(r"[\d-]+", q):
            rows = [r for r in rows if r[0].replace("-", "").startswith(digits)]
        elif q:
            needle = normalize_name(q) or q.casefold()
            rows = [r for r in rows if needle in (normalize_name(r[1]) or (r[1] or "").casefold())]
            if not rows:
                notice = name_facts(conn, code, code_type, q, needle, file_ids, include_facility)
    providers = [{"ein": e, "name": n, "rate_count": rates, "npi_count": npis, "hospital_count": hosp or 0,
                  **({"npi": str(npi)} if npi else {})}
                 for e, n, rates, npis, hosp in rows]
    return {"code": code, "code_type": code_type, "q": q, "total": len(providers), "providers": providers[:limit],
            "notice": notice}


def name_facts(conn, code, code_type, q, needle, file_ids, include_facility) -> dict:
    """No provider named q has rates for the code: checked on the EINs that carry that name (per EIN). The first
    EIN's facts (ein_facts), or "under_other_names_only" ((ein, count)) when it has the code under other names, plus
    "named" ({"q", "other_eins"}: how many other EINs carry the name)."""
    word = max(needle.split() or [q], key=len)
    eins = [e for e, n, _ in conn.execute(
        "SELECT ein, business_name, COUNT(*) FROM rates WHERE business_name LIKE ? AND ein IS NOT NULL"
        " GROUP BY 1, 2 ORDER BY 3 DESC", (f"%{word}%",)) if needle in (normalize_name(n) or n.casefold())]
    eins = list(dict.fromkeys(eins))
    if not eins:
        return {"kind": "unknown", "what": "name", "value": q}
    facts = ein_facts(conn, code, code_type, eins[0], file_ids)
    if facts is None:  # the EIN has the code, under other names
        own, hospital = counts_for(conn, code, code_type, "ein", eins[0], file_ids)
        n = own + (sum(h[1] for h in hospital) if include_facility else 0)
        facts = {"kind": "none", "code": code, "under_other_names_only": (eins[0], n)}
    return {**facts, "named": {"q": q, "other_eins": len(eins) - 1}}


# ---------------------------------------------------------------- procedures for one provider

def codes_for_provider(conn: sqlite3.Connection, ein: str, q: str, procedures: list[dict],
                       file_ids: list[int] | None = None, limit: int = PICKER_LIMIT) -> dict:
    """The procedures this EIN has rates for in scope that q names (see match_procedures), most rates first: its own
    codes, and the procedures its hospital rates name (billed under a revenue code). {"ein", "total", "codes":
    [{"billing_code", "billing_code_type", "name", "rate_count", "hospital_count", "multi_type" (its number is
    more than one code)}], "notice" (its facts, or None)}.
    procedures: procedure_list over every file, for names and to tell a real code from a typo."""
    ein = normalize_ein(ein) or ein
    files, fargs = file_sql(file_ids)
    by_code = {(p["billing_code"], p["billing_code_type"]): p for p in procedures}
    types_of: dict = {}
    for code, code_type in by_code:
        types_of.setdefault(code, []).append(code_type)
    entries: dict = {}
    for code, code_type, rates in conn.execute(
            f"SELECT billing_code, billing_code_type, COUNT(*) FROM rates WHERE ein = ?{files} GROUP BY 1, 2",
            (ein, *fargs)):
        entries[(code, code_type)] = {"own": rates, "hospital": 0}
    for code, rates in conn.execute(
            "SELECT additional_information, COUNT(*) FROM rates WHERE ein = ? AND billing_code_type = 'RC'"
            f" AND additional_information IS NOT NULL{files} GROUP BY 1", (ein, *fargs)):
        code_type = next((t for t in types_of.get(code, []) if t != "RC"), None)
        entries.setdefault((code, code_type), {"own": 0, "hospital": 0})["hospital"] += rates
    rows = []
    for (code, code_type), n in entries.items():
        known = by_code.get((code, code_type)) or {}
        multi = len(types_of.get(code, [])) > 1
        rows.append({"billing_code": code, "billing_code_type": code_type, "name": known.get("name") or "",
                     "description": known.get("description"), "rate_count": n["own"] + n["hospital"],
                     "hospital_count": n["hospital"], "multi_type": multi})
    rows.sort(key=lambda r: (-r["rate_count"], r["billing_code"]))
    found = match_procedures(rows, q)
    notice = None
    q = (q or "").strip()
    if q and not found:
        notice = code_facts(conn, ein, q, procedures, rows, file_ids)
    for r in found:
        r.pop("description", None)
    return {"ein": ein, "q": q, "total": len(found), "codes": found[:limit], "notice": notice}


def code_facts(conn, ein, q, procedures, rows, file_ids) -> dict:
    """The EIN has no rates for the code q names: "elsewhere" (it has, in other files), "near" (its nearest codes by
    number when q is a code, else its top codes), "other_code_count"; unknown when q names no code at all."""
    known = match_procedures(procedures, q)
    if not known:
        return {"kind": "unknown", "what": "code", "value": q}
    target = known[0]
    code = target["billing_code"]
    elsewhere = False
    if file_ids:
        own, hospital = counts_for(conn, code, target["billing_code_type"], "ein", ein, None)
        elsewhere = bool(own + sum(h[1] for h in hospital))
    numeric = re.sub(r"\D", "", code)
    if numeric and q.strip().upper() == code:
        near = sorted(rows, key=lambda r: (abs(int(re.sub(r"\D", "", r["billing_code"]) or 0) - int(numeric)),
                                           r["billing_code"]))
        near_by = "number"
    else:
        near, near_by = rows, "rates"
    return {"kind": "none", "ein": ein, "code": code, "code_name": target["name"], "ein_count": target["ein_count"],
            "elsewhere": elsewhere, "near": near[:4], "near_by": near_by, "other_code_count": len(rows)}


# ---------------------------------------------------------------- a provider's prices per code

MOSTLY = 0.5  # more than half of a group's offers from this provider: "everyone" is mostly this provider


def provider_prices(conn: sqlite3.Connection, ein: str, codes: list[dict],
                    file_ids: list[int] | None = None) -> list[dict]:
    """For each of these codes (dicts with billing_code and billing_code_type, e.g. one page of provider_detail's),
    the provider's most common like-for-like group (billed as, procedure, modifiers, billing class, place of
    service) and, counted by provider offer (EIN + price + group + file, as the procedure page counts by default),
    its median there against everyone's median in the same group. Adds "group" (with labels), "group_count", "count"
    and "median" (this provider), "everyone_count" and "everyone_median", "share" (its offers over everyone's),
    "mostly_this" (share over a half) and "difference" (its median over everyone's, minus one)."""
    ein = normalize_ein(ein) or ein
    if not codes:
        return []
    files, fargs = file_sql(file_ids, "r.source_file_id")
    numbers = sorted({c["billing_code"] for c in codes})
    offers: dict = {}  # (code, type) -> group key -> list of (ein, rate)
    for start in range(0, len(numbers), 200):
        chunk = numbers[start:start + 200]
        for code, code_type, *rest in conn.execute(
                f"SELECT r.billing_code, r.billing_code_type, {GROUP_KEY_SQL}, r.ein, r.negotiated_rate FROM rates r"
                f" WHERE r.billing_code IN ({', '.join('?' * len(chunk))}){files}"
                f" GROUP BY 1, 2, 3, 4, 5, 6, 7, r.ein, r.negotiated_rate, r.source_file_id", (*chunk, *fargs)):
            *key, offer_ein, rate = rest
            offers.setdefault((code, code_type), {}).setdefault(tuple(key), []).append((offer_ein, rate))
    out = []
    for c in codes:
        groups = offers.get((c["billing_code"], c["billing_code_type"]), {})
        mine = {key: sorted(rate for e, rate in group if e == ein) for key, group in groups.items()}
        mine = {key: rates for key, rates in mine.items() if rates}
        if not mine:
            out.append({**c, "group": None, "group_count": 0})
            continue
        key = min(mine, key=lambda k: (-len(mine[k]), k))
        everyone = [rate for _, rate in groups[key]]
        group = dict(zip(GROUP_COLUMNS, key))
        median, everyone_median = statistics.median(mine[key]), statistics.median(everyone)
        share = len(mine[key]) / len(everyone)
        out.append({**c, "group": group, "group_count": len(mine), "count": len(mine[key]), "median": median,
                    "everyone_count": len(everyone), "everyone_median": everyone_median, "share": share,
                    "mostly_this": share > MOSTLY,
                    "difference": median / everyone_median - 1 if everyone_median else None})
    return out
