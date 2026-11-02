"""Prices By Procedure (#40, direction B "two doors"): the routes and the view logic, from the URL to one template
context.

/by-procedure is one search box over every procedure (code or name: "massage" finds 97124) that filters the list as
you type; /by-procedure/<code> is that procedure's prices: code type, Search in, a "narrow to a provider" picker
offering only providers with rates for the code, then price groups compared like with like, Count by, statistics
with the distribution chart, the rates table, and the hospital rates billed under a revenue code as groups of their
own. The old /search and /codes links redirect here. Input checks (billing code, NPI, EIN, plan) are shared with the
JSON API.
"""

import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ui import chart
from api.errors import NO_DATA, NoData
from api.grouping import DEFAULT_GROUP_BY, GROUP_BY, grouped_stats_by_group, search_grouped, stats_for_search
from ui.labels import modifier_label, modifier_tooltip, place_label, place_tooltip, split_codes
from api.pickers import (
    ein_facts, match_procedures, npi_facts, procedure_list, procedure_sort, providers_for_code, sentence_case,
    sort_procedures,
)
from api.providers import db_file_version, normalize_ein
from api.search_in import find_plans, resolve_search_in, search_in_choices
from ui.formatting import PROCEDURE_PATH, format_money, money_range, notice, procedure_href, provider_href, providers_picker
from api.search import (
    DIRECTIONS, FILTERS, GROUP_COLUMNS, OPERATORS, PERCENTILE_MIN_COUNTS, RANGE_FILTERS, RATE_SORTS, TEXT_FILTERS,
    VALUELESS_OPERATORS, code_info, code_types, column_choices, facility_count, group_key, normalize_code, plan_info,
    names_in_scope, price_explained, price_groups, procedure_names, search_rates,
)

PAGE_SIZE = 100
# CPT, HCPCS, revenue codes, DRGs, ICD: letters, digits, dots and dashes, never long.
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
NO_FILE = "Pick at least one file to search in."
FILES_CHOSEN = "chosen"  # the search form's marker: the file boxes were shown, so an empty choice means none
UNKNOWN_PLAN = "That plan isn’t in the loaded data. Type a plan name or ID and pick it from the list."

# NPI and EIN from the search form (or the API) mean an exact match: equals, unless an operator is given.
IDENTITY_FILTERS = {"npi": "NPI", "ein": "EIN"}
# The data has one 9-digit NPI, so up to 10 digits rather than exactly 10.
NPI_PATTERN = re.compile(r"^\d{1,10}$")
EIN_PATTERN = re.compile(r"^(\d{2}-\d{7}|\d{9})$")


def validate_code(raw: str | None) -> tuple[str, str | None]:
    """Return (normalized code, error message or None)."""
    code = normalize_code(raw or "")
    if not code:
        return code, "Enter a billing code, such as 99213 or 10006."
    if not CODE_PATTERN.match(code):
        return code, f"“{raw.strip()}” isn’t a valid billing code. Use letters and digits only, such as 99213 or J1100."
    return code, None


def exact_identity(filters: dict) -> dict:
    """filters with npi_op / ein_op set to equals where an NPI or EIN is given without an operator."""
    filters = dict(filters)
    for name in IDENTITY_FILTERS:
        value = filters.get(name)
        if value is not None and str(value).strip() and not str(filters.get(f"{name}_op") or "").strip():
            filters[f"{name}_op"] = "equals"
    return filters


def validate_identity(filters: dict) -> str | None:
    """A friendly message for an exact NPI or EIN that can't be one, else None. Partial (contains) values
    from the filter row aren't checked."""
    for name, pattern, message in (
        ("npi", NPI_PATTERN, "An NPI is a number of up to 10 digits, such as 1801055561."),
        ("ein", EIN_PATTERN, "Use 9 digits, with or without the dash, such as 45-0934668."),
    ):
        value = str(filters.get(name) or "").strip()
        if value and filters.get(f"{name}_op") == "equals" and not pattern.match(value):
            return f"“{value}” isn’t a valid {IDENTITY_FILTERS[name]}. {message}"
    return None


def identity_only(filters: dict) -> dict:
    """Just the exact NPI / EIN filters."""
    return {k: v for name in IDENTITY_FILTERS if filters.get(f"{name}_op") == "equals" and name in filters
            for k, v in ((name, filters[name]), (f"{name}_op", "equals"))}


def page_sort(sort: str | None, direction: str | None, allowed, default: str) -> tuple[str, str]:
    """Sort for an HTML page: anything unrecognized falls back to the default instead of erroring."""
    sort = sort if sort in allowed else default
    direction = direction if direction in DIRECTIONS else "asc"
    return sort, direction


def page_filters(query_params) -> tuple[dict, dict]:
    """(filters to apply, values to show back in the boxes). A rate that isn't a number is ignored, and
    an unknown operator falls back to contains. Shown values include each text column's "<name>_op"."""
    shown = {name: query_params.get(name, "") for name in FILTERS}
    applied = {}
    for name, value in shown.items():
        if not value.strip():
            continue
        if name in RANGE_FILTERS:
            try:
                applied[name] = float(value)
            except ValueError:
                continue
        else:
            applied[name] = value
    for name in TEXT_FILTERS:
        op = query_params.get(f"{name}_op", "").strip()
        if name in IDENTITY_FILTERS and name in applied and not op:
            op = "equals"  # an NPI or EIN on its own (the search form's boxes) is an exact match
        shown[f"{name}_op"] = op if op in OPERATORS else ""
        if op in VALUELESS_OPERATORS:  # is empty / is not empty: no value needed, and any value is ignored
            applied.pop(name, None)
            applied[f"{name}_op"] = op
        elif name in applied and shown[f"{name}_op"]:
            applied[f"{name}_op"] = op
    return applied, shown


GROUP_FILTER_PARAMS = {*GROUP_COLUMNS, *(f"{c}_op" for c in GROUP_COLUMNS)}


def group_params(group: dict, columns) -> list[tuple[str, str]]:
    """Query pairs that pick exactly this group: equals its value, or is empty when it has none."""
    pairs = []
    for column in columns:
        value = group[column]
        pairs += [(column, value), (f"{column}_op", "equals")] if value else [(f"{column}_op", "empty")]
    return pairs


def billed_as_label(billed_as: str, departments: dict) -> str:
    """ "RC 0510" -> "RC 0510 · Clinic" when the revenue code's department is known; anything else as it is."""
    kind, _, code = billed_as.partition(" ")
    department = departments.get(code.lstrip("0")) if kind == "RC" else None
    return f"{billed_as} · {department}" if department else billed_as


def procedure_label(code: str, names: dict) -> str:
    """ "96365" -> "96365 · THER/PROPH/DIAG IV INF INIT" (name from our own CPT/HCPCS rates), or just the code."""
    if not code:
        return "no procedure code"
    return f"{code} · {names[code]}" if code in names else code


def include_facility_param(values: list[str]) -> bool:
    """The search form sends include_facility=false from a hidden field, and true too when the box is ticked;
    a link without it means the default, included."""
    return "true" in values or not any(v.strip().lower() in ("false", "0", "off") for v in values)


def cent_below(value: float) -> Decimal:
    """The highest whole cent strictly below value: rates under a low fence are at most this."""
    return (Decimal(repr(value)) * 100).to_integral_value(rounding="ROUND_CEILING") / 100 - Decimal("0.01")


def cent_above(value: float) -> Decimal:
    """The lowest whole cent strictly above value: rates over a high fence are at least this."""
    return (Decimal(repr(value)) * 100).to_integral_value(rounding="ROUND_FLOOR") / 100 + Decimal("0.01")


def resolve_plan(conn: sqlite3.Connection, raw) -> tuple[dict | None, str | None]:
    """(plan_info, None) for a plans row id, else (None, UNKNOWN_PLAN)."""
    raw = str(raw).strip()
    plan = plan_info(conn, int(raw)) if raw.isdigit() else None
    return plan, None if plan else UNKNOWN_PLAN


VIEWS = ("compare", "stats", "rates")
# What one value counts, per Count by choice: (singular, plural).
UNITS = {"none": ("rate", "rates"), "provider": ("provider price", "provider prices"),
         "npi": ("NPI price", "NPI prices")}
COUNT_BY_LABELS = {"provider": "Provider", "npi": "NPI", "none": "Every row"}
COLUMN_LABELS = {"npi": "NPI", "ein": "EIN", "billed_as": "Billed As", "procedure": "Procedure", "provider": "Provider",
                 "billing_class": "Billing Class", "setting": "Setting", "service_codes": "Place",
                 "modifiers": "Modifiers"}
OP_WORDS = {"contains": "contains", "not_contains": "does not contain", "equals": "is", "not_equals": "is not",
            "empty": "is empty", "not_empty": "is not empty"}
# Chips: the search bar's NPI and EIN first, then the columns in table order, then the rate range.
CHIP_ORDER = ("npi", "ein", "billed_as", "procedure", "provider", "billing_class", "setting", "service_codes",
              "modifiers")
# Columns of the rates table, in order; folded into "Same for every row" when they hold one value.
FOLDABLE = ("billed_as", "procedure", "billing_class", "setting", "service_codes", "modifiers")
PERCENTILE_LABELS = (("p1", "P1"), ("p90", "P90"), ("p95", "P95"), ("p99", "P99"), ("p99_9", "P99.9"),
                     ("p99_99", "P99.99"))


# How the procedure list's count line says each order (ascending, descending); the sorts are api.pickers'.
PROCEDURE_ORDERS = {
    "": ("most rates first", "most rates first"),
    "code": ("by code, lowest first", "by code, highest first"),
    "name": ("by name, A to Z", "by name, Z to A"),
    "type": ("by type, A to Z", "by type, Z to A"),
    "rates": ("fewest rates first", "most rates first"),
    "eins": ("fewest providers first", "most providers first"),
}


class Links:
    """Links to this page (path, e.g. /by-procedure/99213) with one thing changed. Query order: scope (Search in,
    hospital rates, code type), filters, then group_by, view, sort, dir, gsort, gdir and offset; defaults are left
    out."""

    TAIL = ("group_by", "view", "sort", "dir", "gsort", "gdir", "offset")

    def __init__(self, path: str, scope: list[tuple], filters: list[tuple], **tail):
        self.path, self.scope, self.filters, self.tail = path, scope, filters, tail

    def pairs(self, drop=(), add=(), **tail) -> list[tuple]:
        merged = {**self.tail, **tail}
        return (self.scope + [(k, v) for k, v in self.filters if k not in drop] + list(add)
                + [(k, merged[k]) for k in self.TAIL if merged.get(k) not in (None, "")])

    def __call__(self, drop=(), add=(), **tail) -> str:
        pairs = self.pairs(drop, add, **tail)
        return self.path + (f"?{urlencode(pairs)}" if pairs else "")


def code_title(code: dict | None) -> str:
    """What the header calls a code: its description (the long form) in sentence case, cut back to a whole word
    when the file cut it off at 100 characters; else its name."""
    if not code:
        return ""
    text = code["description"] or code["name"] or ""
    if len(text) >= 100:
        text = text[:text.rfind(" ")].rstrip(",;:/- ") + "…"
    return sentence_case(text)


def plural(n: int, words: tuple[str, str]) -> str:
    return f"{n:,} {words[0] if n == 1 else words[1]}"


def chip_label(name: str, value, op: str | None) -> str:
    if name in IDENTITY_FILTERS and op == "equals":
        return f"{COLUMN_LABELS[name]} {value}"
    words = OP_WORDS[op or "contains"]
    return f"{COLUMN_LABELS[name]} {words}" + ("" if op in VALUELESS_OPERATORS else f" {str(value).replace(',', ', ')}")


def rate_chip_label(low, high) -> str:
    if low is not None and high is not None:
        return f"Rate {format_money(low)}–{format_money(high)}"
    return f"Rate ≥ {format_money(low)}" if low is not None else f"Rate ≤ {format_money(high)}"


def explained_parts(explained: dict) -> list[dict]:
    """What explains price as one bar adding to 100%: the stronger of place of service and modifier alone, what
    the other adds on top (both − the first), and the rest (mostly which provider). Rounded so the parts sum to
    100."""
    if explained["place"] >= explained["modifier"]:
        first, second = ("Place of service", explained["place"]), "Modifier"
    else:
        first, second = ("Modifier", explained["modifier"]), "Place of service"
    a = round(first[1] * 100)
    b = max(0, round(explained["both"] * 100) - a)
    return [{"label": first[0], "pct": a, "cls": "p"}, {"label": f"{second} adds", "pct": b, "cls": "m"},
            {"label": "Other, mostly which provider", "pct": 100 - a - b, "cls": "o"}]


def procedure_context(connect, raw_code: str, query_params, departments, select_limit: int = 50,
                      suggest_limit: int = 2000, group_limit: int = 12) -> dict:
    """Everything procedure.html shows for one procedure and these query parameters. connect() opens the DB
    (raising NoData); departments(conn) gives every revenue code's department name."""
    offset = query_params.get("offset", "0")
    sort, direction = page_sort(query_params.get("sort"), query_params.get("dir"), RATE_SORTS, "rate")
    gsort = query_params.get("gsort")
    group_sort = (gsort, query_params.get("gdir") if query_params.get("gdir") in DIRECTIONS
                  else GROUP_SORTS[gsort][1]) if gsort in GROUP_SORTS else DEFAULT_GROUP_SORT
    group_by = query_params.get("group_by") if query_params.get("group_by") in GROUP_BY else DEFAULT_GROUP_BY
    context = {"query": raw_code or "", "error": None, "no_data": False, "result": None, "code": None,
               "sort": sort, "direction": direction, "group_by": group_by, "group_limit": group_limit,
               "group_sort": group_sort, "group_sorts": {k: first for k, (_, first) in GROUP_SORTS.items()},
               "count_by_labels": COUNT_BY_LABELS, "column_labels": COLUMN_LABELS, "search_in": None,
               "search_in_choices": [], "notice": None, "narrowed": None, "scope_pairs": []}
    filters, context["filter_values"] = page_filters(query_params)
    include_facility = include_facility_param(query_params.getlist("include_facility"))
    context["include_facility"] = include_facility
    try:
        with closing(connect()) as conn:
            context["search_in"] = scope = resolve_search_in(conn, query_params.get("in"))
            context["search_in_choices"] = search_in_choices(conn)
    except NoData:
        context["no_data"] = True
        return context
    context["source_filter"] = scope["file_ids"]
    # The scope every link on the page keeps: Search in and hidden hospital rates.
    context["scope_pairs"] = ([("in", scope["value"])] if scope["value"] else []) + (
        [] if include_facility else [("include_facility", "false")])
    code, context["error"] = validate_code(raw_code)
    context["error"] = context["error"] or validate_identity(filters)
    if context["error"] is not None:
        return context
    context["path"] = f"{PROCEDURE_PATH}/{quote(code, safe='')}"
    start = int(offset) if offset.isdigit() else 0
    try:
        with closing(connect()) as conn:
            results_context(context, conn, code, query_params.get("code_type"), filters, start,
                            query_params.get("view"), departments, select_limit, suggest_limit)
    except NoData:
        context["no_data"] = True
    return context


def narrowed_context(conn, code: str, code_type, exact: dict, source_filter, links: "Links", scope_pairs) -> dict:
    """The provider the page is narrowed to (an exact EIN and/or NPI): {"label", "names" (its names with rates for
    this code here, most first), "eins", "clear_url" (any provider), "provider_url" (its page)}."""
    rows = names_in_scope(conn, code, source_filter, code_type, exact)
    ein = normalize_ein(exact.get("ein", "")) if exact.get("ein") else None
    npi = str(exact.get("npi", "")).strip() or None
    eins = [ein] if ein else list(dict.fromkeys(e for _, e, _ in rows if e))
    label = " · ".join(([f"NPI {npi}"] if npi else []) + ([f"EIN {ein}"] if ein else []))
    return {"label": label, "names": [(n, c) for n, _, c in rows if n], "eins": eins,
            "provider_url": provider_href(eins[0], scope_pairs) if eins else None,
            "clear_url": links(drop=("ein", "ein_op", "npi", "npi_op"), offset=None)}


def identity_notice(conn, code, code_type, exact: dict, source_filter, scope_pairs, include_facility) -> dict | None:
    """Why an exact NPI / EIN has no rates for this code here, with alternatives (see api.pickers); checked per NPI
    when one is given, else per EIN."""
    if exact.get("npi"):
        return notice(npi_facts(conn, code, code_type, int(exact["npi"]), source_filter, include_facility), scope_pairs)
    if exact.get("ein"):
        return notice(ein_facts(conn, code, code_type, normalize_ein(exact["ein"]), source_filter), scope_pairs)
    return None


def results_context(context: dict, conn: sqlite3.Connection, code: str, code_type: str | None, filters: dict,
                    start: int, view: str | None, departments, select_limit: int, suggest_limit: int) -> None:
    sort, direction, include_facility = context["sort"], context["direction"], context["include_facility"]
    group_by, source_filter = context["group_by"], context["source_filter"]
    context["units"] = UNITS[group_by]
    # One number can be more than one code (CPT 731 vs RC 731): show one type at a time, the one asked for or else
    # the one with the most rates.
    types = code_types(conn, code, source_filter)
    names = [t["billing_code_type"] for t in types]
    chosen = code_type if code_type in names else (names[0] if names else None)
    context["code_types"], context["code_type"] = types, chosen
    context["code"] = code_info(conn, code, chosen)
    context["code_title"] = code_title(context["code"])
    scope_pairs = context["scope_pairs"]
    scope = scope_pairs + ([("code_type", chosen)] if len(types) > 1 else [])
    filter_pairs = [(name, context["filter_values"][name]) for name in context["filter_values"] if name in filters]
    path = context["path"]
    links = Links(path, scope, filter_pairs, group_by=None if group_by == DEFAULT_GROUP_BY else group_by,
                  view=view if view in VIEWS else None,
                  sort=None if sort == "rate" else sort, dir=None if direction == "asc" else direction,
                  **group_sort_params(context["group_sort"]))
    context["url"] = links
    keep = [p for p in links.pairs() if p[0] == "group_by"]
    context["type_url"] = lambda t: f"{path}?{urlencode(scope_pairs + [('code_type', t)] + keep)}"
    flipped = [p for p in scope if p[0] == "in"] + ([("include_facility", "false")] if include_facility else []) + [
        p for p in scope if p[0] == "code_type"]
    context["facility_url"] = Links(path, flipped, filter_pairs, **links.tail)()
    # What the narrow-to-a-provider picker asks (the providers with rates for this code here).
    context["picker_api"] = f"/api/procedures/{quote(code)}/providers?" + urlencode(scope)
    context["provider_url"] = lambda ein: provider_href(ein, scope_pairs) if ein else None

    # Groups (billed as, procedure, modifiers, billing class, place of service): every one in scope, and those the
    # filtered results span.
    groups = grouped_stats_by_group(conn, code, source_filter, chosen, filters, include_facility, group_by)
    result_groups = price_groups(conn, code, source_filter, chosen, filters, keep_group_filters=True,
                                 include_facility=include_facility)
    context["facility_count"] = facility_count(conn, code, source_filter, chosen)
    context["show_billed_as"] = include_facility and context["facility_count"] > 0
    context["show_procedure"] = chosen == "RC"
    table = search_grouped(conn, code, 0, 0, sort, direction, source_filter, filters, chosen, include_facility,
                           group_by)
    context["result"] = table
    exact = identity_only(filters)
    everyone = None
    if exact:
        context["narrowed"] = narrowed_context(conn, code, chosen, exact, source_filter, links, scope_pairs)
        others = {k: v for k, v in filters.items() if k not in exact}
        everyone = {group_key(g): g for g in grouped_stats_by_group(conn, code, source_filter, chosen, others,
                                                                    include_facility, group_by)}
    if table["total"] == 0 and filters:
        # Say why nothing matched: no rates for the code at all, or none from this NPI / EIN (with alternatives).
        context["code_missing"] = search_rates(
            conn, code, 1, 0, source_file_ids=source_filter, code_type=chosen,
            include_facility=include_facility)["total"] == 0
        if exact and not context["code_missing"] and search_rates(
                conn, code, 1, 0, source_file_ids=source_filter, filters=exact,
                code_type=chosen, include_facility=include_facility)["total"] == 0:
            context["identity_miss"] = " and ".join(
                f"{IDENTITY_FILTERS[name]} {exact[name].strip()}" for name in IDENTITY_FILTERS if name in exact)
            context["notice"] = identity_notice(conn, code, chosen, exact, source_filter, scope_pairs,
                                                include_facility)
    context["clear_url"] = links(drop=[n for n, _ in filter_pairs], view=None)

    groups_context(context, conn, groups, result_groups, filters, links, departments, everyone)
    picked = context["group_picked"]
    context["chips"] = chips(filters, context["filter_values"], links,
                             skip=(GROUP_FILTER_PARAMS if picked else set()) | set(exact) | {f"{n}_op" for n in exact})

    # Tabs: Compare Groups only when there is more than one group; Statistics by default for one group.
    view = view if view in VIEWS else ("compare" if len(result_groups) > 1 else "stats")
    if view == "compare" and len(groups) < 2:
        view = "stats"
    context["view"] = view
    context["tabs"] = ([("compare", "Compare Groups", len(groups))] if len(groups) > 1 else []) + [
        ("stats", "Statistics", None), ("rates", "Rates", table["row_count"])]
    context["tab_url"] = lambda v: links(view=v)
    if table["total"] == 0:
        return
    if view == "compare":
        context["explained"] = price_explained(conn, code, source_filter, chosen, filters)
        context["explained_parts"] = explained_parts(context["explained"]) if context["explained"] else None
    elif view == "stats" and len(result_groups) == 1:
        stats_context(context, conn, code, source_filter, chosen, filters, include_facility, group_by, links)
    elif view == "rates":
        rates_context(context, conn, code, source_filter, chosen, filters, start, links, select_limit,
                      suggest_limit)

def chips(filters: dict, shown: dict, links: Links, skip=()) -> list[dict]:
    """Every active filter as {"label", "href"} (the link drops it), the rate range as one chip."""
    out = []
    for name in CHIP_ORDER:
        if name in skip or (name not in filters and f"{name}_op" not in filters):
            continue
        out.append({"label": chip_label(name, shown[name], filters.get(f"{name}_op")),
                    "href": links(drop=(name, f"{name}_op"), offset=None)})
    if "rate_min" in filters or "rate_max" in filters:
        out.append({"label": rate_chip_label(filters.get("rate_min"), filters.get("rate_max")),
                    "href": links(drop=("rate_min", "rate_max"))})
    return out


# Compare Groups' sorts: key -> (the value a group sorts by, the direction a first click gives). Text sorts A to Z
# first, prices low to high, counts and outliers most first. None (no Everyone, no reference) always sorts last.
GROUP_SORTS = {
    "lead": (lambda g: g["lead"], "asc"),
    "modifier": (lambda g: split_codes(g["modifiers"]), "asc"),
    "place": (lambda g: split_codes(g["service_codes"]), "asc"),
    "min": (lambda g: g["min_rate"], "asc"), "q1": (lambda g: g["q1"], "asc"),
    "median": (lambda g: g["median"], "asc"), "q3": (lambda g: g["q3"], "asc"),
    "max": (lambda g: g["max_rate"], "asc"), "everyone": (lambda g: g.get("everyone_median"), "asc"),
    "count": (lambda g: g["count"], "desc"), "providers": (lambda g: g["provider_count"], "desc"),
    "clinicians": (lambda g: g["npi_count"], "desc"), "ref": (lambda g: g["relative"], "asc"),
    "outliers": (lambda g: g["low_outliers"] + g["high_outliers"], "desc"),
}
DEFAULT_GROUP_SORT = ("count", "desc")


def group_sort_params(group_sort: tuple[str, str]) -> dict:
    """gsort / gdir for a link, each left out when it is the default."""
    key, direction = group_sort
    if group_sort == DEFAULT_GROUP_SORT:
        return {"gsort": None, "gdir": None}
    return {"gsort": key, "gdir": None if direction == GROUP_SORTS[key][1] else direction}


def sort_groups(groups: list[dict], key: str, direction: str) -> list[dict]:
    """groups by GROUP_SORTS[key], biggest first among ties; a missing value (None) last either way."""
    value = GROUP_SORTS[key][0]
    ties = sorted(groups, key=lambda g: (-g["count"], -g["rate_count"]))
    present = sorted((g for g in ties if value(g) is not None), key=value, reverse=direction == "desc")
    return present + [g for g in ties if value(g) is None]


MOSTLY = 0.5  # a narrowed provider holding more than half of a group's offers: "everyone" is mostly this provider


def groups_context(context: dict, conn, groups: list[dict], result_groups: list[dict], filters: dict, links: Links,
                   departments, everyone: dict | None = None) -> None:
    """The group picker and the Compare Groups rows: each group's label, link (it as the scope, on Statistics),
    statistics and box-plot strip; "hospital" for the hospital rates billed under a revenue code (shown apart from
    the code's own); with everyone (group key -> that group without the NPI / EIN narrowing), everyone's median and
    count there, and whether they are mostly this provider."""
    show_procedure, show_billed_as = context["show_procedure"], context["show_billed_as"]
    context["procedure_names"] = procedure_names(conn, {g["procedure"] for g in groups}) if show_procedure else {}
    names_by_department = departments(conn) if show_billed_as else {}
    context["departments"] = names_by_department
    if show_procedure and any(g["procedure"] for g in groups):
        lead = lambda g: procedure_label(g["procedure"], context["procedure_names"])
    elif show_billed_as:
        lead = lambda g: billed_as_label(g["billed_as"], names_by_department) if g["billed_as"].startswith("RC ") \
            else None
    else:
        lead = lambda g: None
    # Billed as, procedure and billing class go in a group's link only when the groups differ by them.
    columns = [c for c in ("billed_as", "procedure") if len({g[c] for g in groups}) > 1]
    columns += ["modifiers", "service_codes"]
    if len({g["billing_class"] for g in groups}) > 1:
        columns.append("billing_class")
    chosen = group_key(result_groups[0]) if len(result_groups) == 1 else None
    drop = [*GROUP_FILTER_PARAMS, "rate_min", "rate_max"]
    out = []
    for g in groups:
        name = f"{modifier_label(g['modifiers'])} · {place_label(g['service_codes'])}"
        g = {**g, "lead": lead(g), "name": name, "label": (f"{lead(g)} · " if lead(g) else "") + name,
             "active": group_key(g) == chosen, "modifier_name": modifier_label(g["modifiers"]),
             "place_name": place_label(g["service_codes"]), "modifier_title": modifier_tooltip(g["modifiers"]),
             "place_title": place_tooltip(g["service_codes"]),
             "title": modifier_tooltip(g["modifiers"]) + "\n" + place_tooltip(g["service_codes"]),
             "hospital": context["code_type"] != "RC" and g["billed_as"].startswith("RC "),
             "href": links(drop=drop, add=group_params(g, columns), view="stats", offset=None)}
        if everyone is not None and group_key(g) in everyone:
            other = everyone[group_key(g)]
            g.update(everyone_median=other["median"], everyone_count=other["count"],
                     mostly_this=g["count"] / other["count"] > MOSTLY)
        out.append(g)
    # Biggest first in what Count by counts (rows, or offers), so the table and the picker lead with the groups
    # that matter for this choice.
    out.sort(key=lambda g: (-g["count"], -g["rate_count"]))
    context["groups"] = out
    # The Compare Groups tables, in the order their headers ask for (gsort, gdir).
    shown = sort_groups(out, *context["group_sort"])
    context["own_groups"] = [g for g in shown if not g["hospital"]]
    context["hospital_groups"] = [g for g in shown if g["hospital"]]
    context["reference_group"] = next((g for g in out if g.get("reference")), None)
    context["group_picked"] = chosen is not None and any(name in GROUP_FILTER_PARAMS for name in filters)
    context["current_group"] = next((g for g in out if g["active"]), None) if chosen else None
    context["all_groups_url"] = links(drop=drop, offset=None)
    context["has_base_rate"] = any(g["modifiers"] == "" for g in groups)


def stats_context(context: dict, conn, code, source_filter, chosen, filters, include_facility, group_by,
                  links: Links) -> None:
    stats = stats_for_search(conn, code, source_filter, chosen, filters, include_facility, group_by)
    context["stats"] = stats
    if not stats:
        return
    context["chart"] = chart.distribution_chart(stats, context["units"])
    context["chart_geometry"] = {"width": chart.WIDTH, "left": chart.LEFT, "right": chart.WIDTH - chart.RIGHT}
    context["range_url"] = links(drop=("rate_min", "rate_max"), view="stats", offset=None)
    context["ranged"] = "rate_min" in filters or "rate_max" in filters
    keep = ("rate_min", "rate_max")
    if stats["low_fence"] >= 0 and stats["low_outliers"]:
        context["low_outlier_url"] = links(drop=keep, add=[("rate_max", str(cent_below(stats["low_fence"])))],
                                           view="rates", offset=None)
    if stats["high_outliers"]:
        context["high_outlier_url"] = links(drop=keep, add=[("rate_min", str(cent_above(stats["high_fence"])))],
                                            view="rates", offset=None)
    context["percentiles"] = [
        (label, stats[key], None if stats[key] is not None else
         f"{label} needs at least {PERCENTILE_MIN_COUNTS[key]:,} {context['units'][1]}; these are {stats['count']:,}.")
        for key, label in PERCENTILE_LABELS]


def rates_context(context: dict, conn, code, source_filter, chosen, filters, start, links: Links, select_limit,
                  suggest_limit) -> None:
    sort, direction, group_by = context["sort"], context["direction"], context["group_by"]
    include_facility = context["include_facility"]
    table = search_grouped(conn, code, PAGE_SIZE, start, sort, direction, source_filter, filters, chosen,
                           include_facility, group_by)
    context["result"] = table
    choices = column_choices(conn, code, source_filter, select_limit, suggest_limit, chosen, include_facility)
    context["choices"] = choices
    rows = table["results"]
    context["billed_labels"] = {b: billed_as_label(b, context["departments"] or {})
                                for b in {r["billed_as"] for r in rows}}
    if context["show_procedure"]:
        context["procedure_names"] = {**context["procedure_names"],
                                      **procedure_names(conn, {r["procedure"] for r in rows})}
    # Columns with one value across the scope fold into "Same for every row": constant for the code (unless
    # being filtered on), and the group's columns once one group is picked.
    same = {}
    for name in FOLDABLE:
        choice = choices.get(name) or {}
        filtered = name in filters or f"{name}_op" in filters
        if choice.get("control") == "fixed" and choice.get("values") and not filtered:
            same[name] = choice["values"][0]
    group = context["current_group"]
    if context["group_picked"] and group:
        same.update({c: group[c] for c in GROUP_COLUMNS if group[c]})
    shown_columns = (["billed_as"] if context["show_billed_as"] else []) + (
        ["procedure"] if context["show_procedure"] else []) + [
        "provider", "ein", "npi", "billing_class", "setting", "service_codes", "modifiers"]
    context["columns"] = [c for c in shown_columns if c not in same]
    display = {"billed_as": lambda v: billed_as_label(v, context["departments"] or {}),
               "procedure": lambda v: procedure_label(v, context["procedure_names"]),
               "service_codes": place_label, "modifiers": modifier_label}
    context["same"] = [display.get(c, lambda v: v)(same[c]) for c in shown_columns if c in same]
    filter_fields = {"rate_min", "rate_max"} | {n for c in context["columns"] if c not in IDENTITY_FILTERS
                                                for n in (c, f"{c}_op")}
    context["filter_count"] = sum(1 for name in filters if name in filter_fields and not name.endswith("_op")) + sum(
        1 for name in filters if name.endswith("_op") and name in filter_fields and filters[name] in VALUELESS_OPERATORS)
    context["rates_params"] = [p for p in links.pairs(view="rates") if p[0] not in ("sort", "dir")]
    context["hidden_params"] = [p for p in links.pairs(view="rates") if p[0] not in filter_fields | {"sort", "dir"}]
    context["page_url"] = lambda offset: links(view="rates", sort=sort, dir=direction, offset=offset or None)


LIST_SIZE = 100  # the empty page lists this many procedures; typing narrows them


def search_redirect(params) -> str:
    """Where an old /search link goes (#40): /by-procedure/<code> with the same meaning. plan=<id> becomes
    in=plan-<id>, one source=<id> in=file-<id> (both files, or none, is all plans), hidden hospital rates stay hidden;
    every other parameter (filters, NPI, EIN, group_by, view, sort, paging, code_type) is kept. No code: the list."""
    code = normalize_code(params.get("billing_code") or "")
    pairs = []
    plan, sources = (params.get("plan") or "").strip(), sorted({v for v in params.getlist("source") if v.isdigit()})
    if plan.isdigit():
        pairs.append(("in", f"plan-{plan}"))
    elif len(sources) == 1:
        pairs.append(("in", f"file-{sources[0]}"))
    if not include_facility_param(params.getlist("include_facility")):
        pairs.append(("include_facility", "false"))
    old = {"billing_code", "plan", "plan_q", "source", "files", "include_facility"}
    pairs += [(k, v) for k, v in params.multi_items() if k not in old and v.strip()]
    path = f"{PROCEDURE_PATH}/{quote(code, safe='')}" if code else PROCEDURE_PATH
    return path + (f"?{urlencode(pairs)}" if pairs else "")


def procedure_json(p: dict, multi: set) -> dict:
    """A procedure for the API, with the link to its prices (naming its type when its number is two codes)."""
    return {**p, "href": procedure_href(p["billing_code"],
                                        code_type=p["billing_code_type"] if p["billing_code"] in multi else None)}


def procedure_cache():
    """procedures(conn): every procedure in the active index (procedure_list, ~2 s on the full data) and the numbers
    that are more than one code, read once per version of the DB file. One is shared by both doors."""
    cached: dict = {}

    def procedures(conn) -> tuple[list[dict], set]:
        version = db_file_version(conn)
        if version[1] is None or cached.get("version") != version:
            listed = procedure_list(conn)
            count: dict = {}
            for p in listed:
                count[p["billing_code"]] = count.get(p["billing_code"], 0) + 1
            cached.update(version=version, list=listed, multi={c for c, n in count.items() if n > 1})
        return cached["list"], cached["multi"]

    return procedures


def make_router(connect, templates, departments, procedures, select_limit: int = 50, suggest_limit: int = 2000,
                group_limit: int = 12) -> APIRouter:
    """/by-procedure and /by-procedure/{code} (the pages), /api/procedures and /api/procedures/{code}/providers
    (what the pages' boxes ask), and the redirects from the old /search and /codes. connect() opens the active
    index's DB or raises NoData; departments(conn) names the revenue codes; procedures(conn) is a procedure_cache."""
    router = APIRouter()

    @router.get("/by-procedure", response_class=HTMLResponse)
    def procedure_list_page(request: Request, q: str = "", sort: str = "", dir: str = ""):
        sort, direction = procedure_sort(sort, dir)
        context = {"q": q, "procedures": [], "total": 0, "all": 0, "no_data": False, "door": "procedure",
                   "sort": sort, "direction": direction, "order": PROCEDURE_ORDERS[sort][direction == "desc"]}
        try:
            with closing(connect()) as conn:
                listed, multi = procedures(conn)
        except NoData:
            context["no_data"] = True
        else:
            found = sort_procedures(match_procedures(listed, q), sort, direction)
            context.update(total=len(found), all=len(listed),
                           procedures=[procedure_json(p, multi) for p in found[:LIST_SIZE]])
        return templates.TemplateResponse(request, "procedures.html", context)

    @router.get("/by-procedure/{code}", response_class=HTMLResponse)
    def procedure_page(request: Request, code: str):
        context = procedure_context(connect, code, request.query_params, departments, select_limit, suggest_limit,
                                    group_limit)
        context["door"] = "procedure"
        return templates.TemplateResponse(request, "procedure.html", context)

    @router.get("/api/procedures")
    def api_procedures(q: str = "", sort: str = "", dir: str = "", limit: int = Query(50, ge=1, le=5000)):
        """Procedures q names (a code, its start, or words of its name), procedures before hospital departments,
        most rates first, or sorted by one column (api.pickers.PROCEDURE_SORTS), then cut to limit: {"q", "sort",
        "dir", "order" (how the count line says it), "total", "procedures": [{"billing_code", "billing_code_type",
        "name", "description", "rate_count", "ein_count", "href"}]}."""
        sort, direction = procedure_sort(sort, dir)
        try:
            with closing(connect()) as conn:
                listed, multi = procedures(conn)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        found = sort_procedures(match_procedures(listed, q), sort, direction)
        return {"q": q, "sort": sort, "dir": direction, "order": PROCEDURE_ORDERS[sort][direction == "desc"],
                "total": len(found), "procedures": [procedure_json(p, multi) for p in found[:limit]]}

    @router.get("/api/procedures/{code}/providers")
    def api_providers_for_code(code: str, q: str = "", code_type: str | None = None,
                               include_facility: list[str] = Query([]), limit: int = Query(8, ge=1, le=200),
                               scope_in: str = Query("", alias="in")):
        """The narrow-to-a-provider picker: providers with rates for this code in the Search in scope that q names
        (name, EIN, NPI), and a notice with alternatives when what was typed has none (see
        api.pickers.providers_for_code)."""
        code, error = validate_code(code)
        if error:
            return JSONResponse({"detail": error}, status_code=422)
        hospital = include_facility_param(include_facility)
        try:
            with closing(connect()) as conn:
                scope = resolve_search_in(conn, scope_in)
                pairs = ([("in", scope["value"])] if scope["value"] else []) + (
                    [] if hospital else [("include_facility", "false")])
                found = providers_picker(
                    providers_for_code(conn, code, q, code_type or None, scope["file_ids"], hospital, limit), pairs)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        return {**found, "searching": scope["statement"]}

    @router.get("/api/plans")
    def api_plans(q: str = "", limit: int = Query(8, ge=1, le=200)):
        """Find your plan (#43): plans in the active index whose name or plan ID contains q, each with the network
        (in-network file) it is in: {"q", "total", "plans": [{"value" ("plan-<id>", for Search in), "plan_id",
        "plan_name", "network", "short_name"}]}."""
        try:
            with closing(connect()) as conn:
                found = find_plans(conn, q, limit)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        return {"q": q, **found}

    @router.get("/search")
    def old_search(request: Request):
        return RedirectResponse(search_redirect(request.query_params), status_code=307)

    @router.get("/codes")
    def old_codes(q: str = ""):
        return RedirectResponse(PROCEDURE_PATH + (f"?{urlencode({'q': q})}" if q.strip() else ""), status_code=307)

    return router
