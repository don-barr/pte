"""Search in (#39, #40, #43): what a price page searches, from the active index only: all networks, or one in-network
file, named by its plans ("Ambetter · fidelis-ex · 88 plans"). Plans in one file share every rate, so a plan is only
ever a way to pick its file: "Find your plan" looks one up by name or plan ID and states which network it is in
("Ambetter from Fidelis Care Bronze (25303NY011) is in the Ambetter network (fidelis-ex); every plan there shares these
rates."). The query parameter is `in`: "" (all networks), "file-<source file id>" or "plan-<plans row id>" (searches
that plan's file and says so).
"""

import re
import sqlite3

from api.search import list_plans, list_sources, plan_info

UNKNOWN = "That plan or file isn’t in the active index, so all networks are searched."
PLAN_LIMIT = 8


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def loaded_sources(conn: sqlite3.Connection) -> list[dict]:
    return [s for s in list_sources(conn) if not s["error"]]


def network_label(plan_names: list[str], fallback: str) -> str:
    """A short name for a file's plans: the leading words they all share, up to "from" (the issuer) and without
    trailing numbers ("Ambetter from Fidelis Care Bronze" + "… Gold" → "Ambetter"; "Essential Plan 1" + "Essential
    Plan 4" → "Essential Plan"). Nothing shared: fallback (the short file name)."""
    words = [(name or "").split() for name in plan_names]
    common = []
    for column in zip(*words) if words else ():
        if any(w.casefold() != column[0].casefold() for w in column):
            break
        common.append(column[0])
    if any(w.casefold() == "from" for w in common):
        common = common[:[w.casefold() for w in common].index("from")]
    while common and re.search(r"\d", common[-1]):
        common.pop()
    return " ".join(common) or fallback


def networks(conn: sqlite3.Connection) -> dict[int, dict]:
    """Every loaded file as a network, by source file id: {"id", "name" ("Ambetter"), "short_name" ("fidelis-ex"),
    "plan_count"}."""
    return {s["id"]: {"id": s["id"], "name": network_label(s["plan_names"], s["short_name"]),
                      "short_name": s["short_name"], "plan_count": s["plan_count"]} for s in loaded_sources(conn)}


def network_phrase(net: dict) -> str:
    """ "the Ambetter network (fidelis-ex, 88 plans)"."""
    inside = ", ".join(([net["short_name"]] if net["name"] != net["short_name"] else [])
                       + [plural(net["plan_count"], "plan")])
    return f"the {net['name']} network ({inside})"


def search_in_choices(conn: sqlite3.Connection) -> list[dict]:
    """The choices, shown side by side: All networks first, then one per file: [{"value", "name", "detail",
    "label"}], e.g. {"value": "file-2", "name": "Ambetter", "detail": "fidelis-ex · 88 plans", "label": "Ambetter ·
    fidelis-ex · 88 plans"}."""
    nets = list(networks(conn).values())
    files = {1: "1 file", 2: "both files"}.get(len(nets), f"all {len(nets)} files")
    choices = [{"value": "", "name": "All networks",
                "detail": f"{files} · {plural(sum(n['plan_count'] for n in nets), 'plan')}"}]
    for net in nets:
        detail = ([net["short_name"]] if net["name"] != net["short_name"] else []) + [plural(net["plan_count"], "plan")]
        choices.append({"value": f"file-{net['id']}", "name": net["name"], "detail": " · ".join(detail)})
    return [{**c, "label": f"{c['name']} · {c['detail']}"} for c in choices]


def find_plans(conn: sqlite3.Connection, q: str, limit: int = PLAN_LIMIT) -> dict:
    """Find your plan: plans in loaded files whose name or plan ID contains q (ignoring case; empty q: every plan),
    by name then plan ID: {"total", "plans": [{"value" ("plan-<id>"), "plan_id", "plan_name", "network",
    "short_name"}]}."""
    nets = networks(conn)
    needle = (q or "").strip().casefold()
    found = [p for p in list_plans(conn) if p["source_file_id"] in nets
             and (needle in (p["plan_name"] or "").casefold() or needle in (p["plan_id"] or "").casefold())]
    return {"total": len(found), "plans": [
        {"value": f"plan-{p['id']}", "plan_id": p["plan_id"], "plan_name": p["plan_name"],
         "network": nets[p["source_file_id"]]["name"], "short_name": nets[p["source_file_id"]]["short_name"]}
        for p in found[:limit]]}


def resolve_search_in(conn: sqlite3.Connection, raw: str | None) -> dict:
    """What `in` means: {"value" (kept in links), "selected" (the choice shown as picked: a plan picks its file),
    "file_ids" (None: every file), "plan" (plan_info or None), "statement", "plan_note", "error"}. The page shows the
    statement as "Searching: …" and, for a plan, the plan_note saying which network it is in. No choice, or "", is
    all networks; anything that isn't a loaded file or plan searches all networks and says so (error)."""
    nets = networks(conn)
    raw = (raw or "").strip()
    kind, _, number = raw.partition("-")
    if kind == "file" and number.isdigit() and int(number) in nets:
        return {"value": raw, "selected": raw, "file_ids": [int(number)], "plan": None, "error": None,
                "plan_note": None, "statement": network_phrase(nets[int(number)])}
    plan = plan_info(conn, int(number)) if kind == "plan" and number.isdigit() else None
    if plan and plan["files"] and all(f in nets for f in plan["source_file_ids"]):
        mine = [nets[f] for f in plan["source_file_ids"]]
        where = " and ".join(f"the {n['name']} network ({n['short_name']})" for n in mine)
        shared = len(mine) == 1 and mine[0]["plan_count"] > 1
        return {"value": raw, "selected": f"file-{mine[0]['id']}" if len(mine) == 1 else "",
                "file_ids": plan["source_file_ids"], "plan": plan, "error": None,
                "statement": " and ".join(network_phrase(n) for n in mine),
                "plan_note": f"{plan['plan_name']} ({plan['plan_id']}) is in {where}"
                             + ("; every plan there shares these rates." if shared else ".")}
    names = " + ".join(n["short_name"] for n in nets.values())
    total = sum(n["plan_count"] for n in nets.values())
    return {"value": "", "selected": "", "file_ids": None, "plan": None, "plan_note": None,
            "error": UNKNOWN if raw else None,
            "statement": f"All networks ({names}, {plural(total, 'plan')}) in the active index{index_label(conn)}"}


def index_label(conn: sqlite3.Connection) -> str:
    """ " (Centene Management Company LLC, 2026-09-28)": who published the active index and its date, when the
    database recorded them (#37), else ""."""
    try:
        row = conn.execute("SELECT reporting_entity_name, last_updated_on FROM index_info WHERE id = 1").fetchone()
    except sqlite3.Error:
        return ""
    parts = [p for p in (row or ()) if p]
    return f" ({', '.join(parts)})" if parts else ""
