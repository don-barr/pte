"""Prices By Provider (#40, direction B "two doors"): the routes and the view logic.

/by-provider is one search box over every provider (a name, ignoring case, punctuation and company suffixes; an EIN;
an NPI) that filters the list as you type. /by-provider/<EIN> is that provider's prices: its names and NPIs, Search
in, a "narrow to a procedure" picker offering only the codes it has rates for, and its codes, each compared within
its most common like-for-like group: its median against everyone's median there (flagged when "everyone" is mostly
this provider), with each code's prices by group and name. Prices only compare within one code, never across codes.
Each code links to "this code at this provider" and "this code, all providers". The old /providers pages redirect
here.
"""

import sqlite3
from contextlib import closing
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ui.labels import modifier_label, place_label
from api.pickers import codes_for_provider, provider_prices, sentence_case
from api.errors import NO_DATA, NoData
from api.providers import (
    MAX_PAGE_SIZE, PAGE_SIZE, PROVIDER_SORTS, cached_providers, code_breakdown, eins_for_npi,
    filter_providers, matched_names, normalize_ein, page_bounds, provider_detail, sort_providers,
)
from api.search_in import resolve_search_in, search_in_choices
from ui.formatting import PROVIDER_PATH, codes_picker, label_groups, procedure_href, provider_href

# How the provider list says each order (ascending, descending); the sorts are api.providers'.
PROVIDER_ORDERS = {
    "ein": ("by EIN, lowest first", "by EIN, highest first"),
    "name": ("by name, A to Z", "by name, Z to A"),
    "names": ("fewest names first", "most names first"),
    "npis": ("fewest NPIs first", "most NPIs first"),
    "codes": ("fewest codes first", "most codes first"),
    "rates": ("fewest rates first", "most rates first"),
}

LIST_SIZE = 100  # the empty page lists this many providers; typing narrows them
LIST_SORTS = ("name", "ein", "npis", "codes", "rates")  # the list's columns; a text column's first click is A to Z
CODES_PAGE = 25  # codes per page on a provider's page (each is compared with everyone's prices for that code)


def group_label(group: dict | None, departments: dict) -> str:
    """ "25 · office (11)"; a hospital rate also says its revenue code and procedure ("RC 0510 Clinic · 99213 · …")."""
    if not group:
        return ""
    head = []
    if group["billed_as"].startswith("RC "):
        number = group["billed_as"].split(" ")[1]
        head.append(f"{group['billed_as']} {departments.get(number.lstrip('0'), '')}".strip())
        if group["procedure"]:
            head.append(group["procedure"])
    return " · ".join(head + [modifier_label(group["modifiers"]), place_label(group["service_codes"])])


def provider_context(conn: sqlite3.Connection, raw_ein: str, params, departments) -> dict:
    """Everything provider.html shows for one EIN and these query parameters (in, name, limit, codes_offset,
    npis_offset)."""
    limit, codes_offset = page_bounds(params.get("limit", CODES_PAGE), params.get("codes_offset"))
    npis_offset = page_bounds(limit, params.get("npis_offset"))[1]
    name = (params.get("name") or "").strip() or None
    ein = normalize_ein(raw_ein)
    scope = resolve_search_in(conn, params.get("in"))
    scope_pairs = [("in", scope["value"])] if scope["value"] else []
    context = {"ein": ein or raw_ein, "provider": None, "notice": None, "name": name, "limit": limit,
               "codes_offset": codes_offset, "npis_offset": npis_offset, "search_in": scope,
               "search_in_choices": search_in_choices(conn), "scope_pairs": scope_pairs,
               "search_in_action": f"{PROVIDER_PATH}/{ein or raw_ein}",
               "search_in_keep": [(k, v) for k, v in (("name", name),) if v]}
    if ein is None:
        context["notice"] = {"kind": "unknown", "message": f"“{raw_ein}” isn’t an EIN: use 9 digits, with or without"
                             " the dash, such as 11-3322995.", "alternatives": [
                                 {"label": "Find a provider by name, EIN or NPI", "href": PROVIDER_PATH}]}
        return context
    provider = provider_detail(conn, ein, name, scope["file_ids"])
    if provider is None:
        elsewhere = scope["file_ids"] and provider_detail(conn, ein, name)
        alternatives = ([{"label": "Search all plans", "href": provider_href(ein)}] if elsewhere else []) + [
            {"label": "Find a provider by name, EIN or NPI", "href": PROVIDER_PATH}]
        where = f"{scope['statement']}" if elsewhere else "the loaded files"
        who = f"EIN {ein}" + (f" under the name {name}" if name else "")
        context["notice"] = {"kind": "none" if elsewhere else "unknown", "alternatives": alternatives,
                             "message": f"{who} has no rates in {where}."}
        return context
    code_count = len(provider["codes"])
    page = label_groups(code_breakdown(conn, ein, provider["codes"][codes_offset:codes_offset + limit],
                                       source_file_ids=scope["file_ids"]))
    rows = []
    for row in provider_prices(conn, ein, page, scope["file_ids"]):
        code_type = row["billing_code_type"] if row["multi_type"] else None
        rows.append({**row, "name": sentence_case(row["name"] or ""), "group_label": group_label(row["group"], departments),
                     "here_href": procedure_href(row["billing_code"], scope_pairs, ein=ein, code_type=code_type),
                     "all_href": procedure_href(row["billing_code"], scope_pairs, code_type=code_type)})
    provider.update(codes=rows, code_count=code_count)
    context["provider"] = provider
    context["picker_api"] = f"/api/providers/{ein}/codes?" + urlencode(scope_pairs)
    base = f"{PROVIDER_PATH}/{ein}"

    def url(**changes) -> str:
        """This page with some parameters changed (None drops one); defaults stay out of the link."""
        values = {"in": scope["value"], "name": name, "limit": limit if limit != CODES_PAGE else None,
                  "codes_offset": codes_offset or None, "npis_offset": npis_offset or None, **changes}
        pairs = [(k, v) for k, v in values.items() if v not in (None, "")]
        return base + (f"?{urlencode(pairs)}" if pairs else "")

    context["url"] = url
    return context


def make_router(connect, templates, departments, procedures) -> APIRouter:
    """/by-provider and /by-provider/{ein} (the pages), /api/providers/{ein}/codes (the narrow-to-a-procedure
    picker), and the redirects from the old /providers pages. connect() opens the active index's DB or raises
    NoData; departments(conn) names the revenue codes; procedures(conn) is ui.procedure_page.procedure_cache's."""
    router = APIRouter()

    @router.get("/by-provider", response_class=HTMLResponse)
    def provider_list_page(request: Request, q: str = "", sort: str = "", dir: str = ""):
        sort = sort if sort in LIST_SORTS else "rates"
        direction = dir if dir in ("asc", "desc") else "asc" if sort in ("name", "ein") else "desc"
        context = {"q": q, "providers": [], "total": 0, "all": 0, "no_data": False, "sort": sort,
                   "direction": direction, "order": PROVIDER_ORDERS[sort][direction == "desc"]}
        try:
            with closing(connect()) as conn:
                providers = cached_providers(conn)
                found = filter_providers(providers, q, eins_for_npi(conn, q))
        except NoData:
            context["no_data"] = True
        else:
            context.update(total=len(found), all=len(providers), providers=[
                {**p, "matched_names": matched_names(p, q), "href": f"{PROVIDER_PATH}/{p['ein']}"}
                for p in sort_providers(found, sort, direction)[:LIST_SIZE]])
        return templates.TemplateResponse(request, "providers.html", context)

    @router.get("/by-provider/{ein}", response_class=HTMLResponse)
    def provider_page(request: Request, ein: str):
        try:
            with closing(connect()) as conn:
                context = provider_context(conn, ein, request.query_params, departments(conn))
        except NoData:
            context = {"ein": ein, "provider": None, "no_data": True, "notice": None}
        status = 404 if context.get("notice") and context["notice"]["kind"] == "unknown" else 200
        return templates.TemplateResponse(request, "provider.html", context, status_code=status)

    @router.get("/api/providers/{ein}/codes")
    def api_codes_for_provider(ein: str, q: str = "", limit: int = Query(8, ge=1, le=500),
                               scope_in: str = Query("", alias="in")):
        """The narrow-to-a-procedure picker: the codes this EIN has rates for in the Search in scope (its own, and
        the procedures its hospital rates name) that q names, and a notice with alternatives when q names a code it
        has none for (see api.pickers.codes_for_provider)."""
        if normalize_ein(ein) is None:
            return JSONResponse({"detail": f"“{ein}” isn’t an EIN."}, status_code=422)
        try:
            with closing(connect()) as conn:
                listed = procedures(conn)[0]
                scope = resolve_search_in(conn, scope_in)
                pairs = [("in", scope["value"])] if scope["value"] else []
                found = codes_picker(codes_for_provider(conn, ein, q, listed, scope["file_ids"], limit), pairs)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        return {**found, "searching": scope["statement"]}

    @router.get("/providers")
    def old_providers(q: str = ""):
        return RedirectResponse(PROVIDER_PATH + (f"?{urlencode({'q': q})}" if q.strip() else ""), status_code=307)

    @router.get("/providers/{ein}")
    def old_provider(ein: str, name: str = ""):
        target = f"{PROVIDER_PATH}/{normalize_ein(ein) or ein}"
        return RedirectResponse(target + (f"?{urlencode({'name': name})}" if name.strip() else ""), status_code=307)

    return router


def make_api_router(connect) -> APIRouter:
    """/api/providers and /api/providers/{ein}, the providers' JSON API. connect() opens the active index's DB
    read-only or raises NoData. Every file unless given source_file_id."""
    router = APIRouter()

    @router.get("/api/providers")
    def api_providers(q: str = "", sort: str = "ein", dir: str = "asc",
                      limit: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE), offset: int = Query(0, ge=0),
                      source_file_id: list[int] | None = Query(None)):
        """total: how many providers match q (a name, ignoring case, punctuation and company suffixes; an EIN; an
        NPI); all: how many there are. Sorted (by EIN unless sort says otherwise; order says how, in words), then
        paged. Each provider has matched_names (its names q named) and href (its Prices By Provider page)."""
        sort = sort if sort in PROVIDER_SORTS else "ein"
        direction = dir if dir in ("asc", "desc") else "asc"
        source_file_ids = source_file_id or None
        try:
            with closing(connect()) as conn:
                providers = cached_providers(conn, source_file_ids)
                found = filter_providers(providers, q, eins_for_npi(conn, q, source_file_ids))
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        page = [{**p, "matched_names": matched_names(p, q), "href": f"{PROVIDER_PATH}/{p['ein']}"}
                for p in sort_providers(found, sort, direction)[offset:offset + limit]]
        return {"q": q, "sort": sort, "dir": direction, "order": PROVIDER_ORDERS[sort][direction == "desc"],
                "total": len(found), "all": len(providers), "limit": limit, "offset": offset, "providers": page}

    @router.get("/api/providers/{ein}")
    def api_provider(ein: str, name: str = "", limit: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
                     codes_offset: int = Query(0, ge=0), source_file_id: list[int] | None = Query(None)):
        """Its names and NPIs, and one page of its codes with their breakdown (code_count: all its codes)."""
        source_file_ids = source_file_id or None
        try:
            with closing(connect()) as conn:
                provider = provider_detail(conn, ein, name or None, source_file_ids)
                if provider is not None:
                    provider["code_count"] = len(provider["codes"])
                    provider["codes"] = label_groups(code_breakdown(
                        conn, ein, provider["codes"][codes_offset:codes_offset + limit], source_file_ids=source_file_ids))
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        if provider is None:
            return JSONResponse({"detail": f"No organization with EIN {ein} in the loaded files."}, status_code=404)
        return {**provider, "limit": limit, "codes_offset": codes_offset}

    return router
