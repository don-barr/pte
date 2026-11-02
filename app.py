"""FastAPI app: the Load Data page, the two price pages (Prices By Procedure, Prices By Provider; #40), a JSON
search API and a health check over the active index's database (#37: one database per index in data/parsed/, one of
them active; see etl.indexes)."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.load_page import make_router as load_router
from ui.docs_pages import make_router as docs_router
from api.grouping import GROUP_BY, grouped_stats_by_group, search_grouped, stats_for_search
from ui.labels import modifier_tooltip, place_tooltip
from ui.provider_page import make_api_router as provider_api_router, make_router as provider_page_router
from api.providers import db_file_version
from api.search import (
    DIRECTIONS, FILTERS, RATE_SORTS, TEXT_FILTERS, code_info, code_types, department_names,
    facility_count, filter_codes, list_codes, price_explained, price_groups,
    procedure_names, short_name, suggest_eins, suggest_npis,
)
from api.errors import NO_DATA, NoData
from ui.formatting import format_money
from ui.procedure_page import (
    FILES_CHOSEN, NO_FILE, PAGE_SIZE, exact_identity, make_router as procedure_router,
    procedure_cache, resolve_plan, validate_code, validate_identity,
)
from etl.indexes import IndexStore

RESTART_TRIGGER = Path(__file__).parent / "ui" / "_dev_reload_trigger.py"
DATA_DIR = Path("data")


def create_app(db_path: str | os.PathLike | None = None, dev_tools: bool = False,
               restart_trigger: Path = RESTART_TRIGGER,
               raw_dir: Path = DATA_DIR / "raw" / "in_network_files",
               index_dir: Path = DATA_DIR / "raw" / "index_files",
               backup_dir: Path | None = None,
               parsed_dir: Path = DATA_DIR / "parsed",
               download: bool = True, select_limit: int = 50, suggest_limit: int = 2000,
               group_limit: int = 12) -> FastAPI:
    """db_path: use exactly this database (tests, or RATES_DB); loading any index rebuilds it. Without it, the index
    databases in parsed_dir: every request reads the active one (IndexStore.active), and loading an index builds its
    own. backup_dir defaults to the backups folder beside the database(s).
    select_limit / suggest_limit: a filter column with at most this many distinct values gets a dropdown /
    a box with suggestions (see column_choices). group_limit: how many price-group chips show before
    "Show all"."""
    if backup_dir is None:
        backup_dir = (Path(db_path).parent if db_path is not None else Path(parsed_dir)) / "backups"
    store = IndexStore(parsed_dir, backup_dir, fixed=db_path)
    # Swagger UI moves to /api/docs so /docs can be the project's own docs (#30).
    app = FastAPI(title="Price Transparency Explorer", docs_url="/api/docs", redoc_url=None)
    templates = Jinja2Templates(directory=Path(__file__).parent / "ui" / "templates")
    templates.env.filters["money"] = format_money
    templates.env.filters["short_name"] = short_name
    templates.env.globals["dev_tools"] = dev_tools
    # Changes whenever the static file changes, so browsers never run a stale cached copy.
    templates.env.globals["static_version"] = \
        lambda name="tables.js": int((Path(__file__).parent / "ui" / "static" / name).stat().st_mtime)
    templates.env.globals["op_labels"] = {"contains": "contains", "not_contains": "does not contain",
                                          "equals": "equals", "not_equals": "does not equal",
                                          "empty": "is empty", "not_empty": "is not empty"}
    templates.env.globals["group_limit"] = group_limit
    templates.env.filters["modifier_tooltip"] = modifier_tooltip
    templates.env.filters["place_tooltip"] = place_tooltip
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "ui" / "static"), name="static")

    def connect() -> sqlite3.Connection:
        """Open the active index's DB read-only (looked up on every call, so switching takes effect at once); raise
        NoData if there is nothing to search."""
        db_path = store.active()
        if db_path is None or not db_path.is_file():
            raise NoData
        try:
            conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise NoData from exc
        try:
            if conn.execute("SELECT EXISTS (SELECT 1 FROM rates)").fetchone()[0] == 0:
                raise NoData
        except (sqlite3.Error, NoData):
            conn.close()
            raise NoData from None
        return conn

    app.include_router(provider_api_router(connect))  # /api/providers (#23)
    app.include_router(docs_router(templates))  # /docs: README, modifiers, requirements (#30)
    app.include_router(load_router(connect, templates, NoData, store, raw_dir, index_dir, download))  # / and /load…

    cached_codes: dict = {}
    cached_departments: dict = {}

    def all_departments(conn: sqlite3.Connection) -> dict:
        """Every revenue code's department name (department_names), read once per version of the DB file."""
        version = db_file_version(conn)
        if cached_departments.get("version") != version:
            cached_departments.update(version=version, names=department_names(conn))
        return cached_departments["names"]

    # The two doors (#40): /by-procedure and /by-provider, their pickers' API, and redirects from the old pages
    procedures = procedure_cache()
    app.include_router(procedure_router(connect, templates, all_departments, procedures, select_limit, suggest_limit,
                                        group_limit))
    app.include_router(provider_page_router(connect, templates, all_departments, procedures))

    @app.get("/api/search")
    def api_search(
        billing_code: str | None = None,
        limit: int = Query(PAGE_SIZE, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        sort: str = "rate",
        dir: str = "asc",
        source_file_id: list[int] | None = Query(None),
        provider: str | None = None,
        ein: str | None = None,
        npi: str | None = None,
        billing_class: str | None = None,
        setting: str | None = None,
        service_codes: str | None = None,
        modifiers: str | None = None,
        rate_min: float | None = None,
        rate_max: float | None = None,
        provider_op: str | None = None,
        ein_op: str | None = None,
        npi_op: str | None = None,
        billing_class_op: str | None = None,
        setting_op: str | None = None,
        service_codes_op: str | None = None,
        modifiers_op: str | None = None,
        code_type: str | None = None,
        plan: str | None = None,
        procedure: str | None = None,
        procedure_op: str | None = None,
        billed_as: str | None = None,
        billed_as_op: str | None = None,
        include_facility: bool = True,
        group_by: str = "none",
        files: str | None = None,
    ):
        """group_by (#24): what one result and one value in the statistics is: "none" (default) every row,
        "provider" one provider price (EIN + price + group + file; results then list their npis and names),
        "npi" one clinician's price; total, stats and stats_by_group's count follow it, row_count gives the rows.
        files=chosen says the caller chose files: with no source_file_id (and no plan) that is an error, not every
        file (#34).
        Text filters take <name>_op: contains (default), not_contains, equals, not_equals, or empty /
        not_empty (no value needed). include_facility (default true) adds, for a CPT/HCPCS code, the hospital
        (facility) rates billed under a revenue code with this code as the procedure; their billed_as says so
        ("RC 0510"), and they form price groups of their own. plan (a plans row id) limits the search to that plan's file(s), overriding
        source_file_id. npi and ein default to equals (an exact match) and are then validated. price_groups lists the (modifiers, billing class, place of service)
        groups in scope, ignoring the filters on those three columns; stats summarises every matching rate
        (rate_stats) and stats_by_group gives each group's count, providers, min, median and max, and its median
        relative to the reference group (relative, reference; see relative_prices). price_explained: how much
        of the spread in log price place of service and modifier account for (or None; see price_explained)."""
        given = locals()
        filters = exact_identity({name: given[name] for name in (*FILTERS, *(f"{n}_op" for n in TEXT_FILTERS))
                                  if given[name] is not None})
        type_asked = given["code_type"]
        code, error = validate_code(billing_code)
        error = error or validate_identity(filters)
        if error is None and files == FILES_CHOSEN and not source_file_id and not (plan or "").strip():
            error = NO_FILE  # an explicitly empty choice of files (#34); no source_file_id at all means every file
        if error is None and group_by not in GROUP_BY:
            error = f"can't group by {group_by!r}; choose one of {', '.join(GROUP_BY)}"
        if error is None and (sort not in RATE_SORTS or dir not in DIRECTIONS):
            error = f"sort must be one of {', '.join(RATE_SORTS)} and dir asc or desc"
        if error:
            return JSONResponse({"detail": error}, status_code=422)
        try:
            with closing(connect()) as conn:
                chosen_plan = None
                if plan is not None and plan.strip():  # a plan (plans row id) decides the files
                    chosen_plan, plan_error = resolve_plan(conn, plan)
                    if plan_error:
                        return JSONResponse({"detail": plan_error}, status_code=422)
                    source_file_id = chosen_plan["source_file_ids"]
                types = code_types(conn, code, source_file_id)
                names = [t["billing_code_type"] for t in types]
                if type_asked and type_asked not in names:
                    return JSONResponse({"detail": f"{code} has no {type_asked} rates; types: {', '.join(names)}"},
                                        status_code=422)
                chosen = type_asked or (names[0] if names else None)
                result = search_grouped(conn, code, limit, offset, sort, dir, source_file_id, filters, chosen,
                                        include_facility, group_by)
                result["code"] = code_info(conn, code, chosen)
                result["code_types"] = types
                result["price_groups"] = price_groups(conn, code, source_file_id, chosen, filters,
                                                      include_facility=include_facility)
                result["stats"] = stats_for_search(conn, code, source_file_id, chosen, filters, include_facility,
                                                   group_by)
                result["stats_by_group"] = grouped_stats_by_group(conn, code, source_file_id, chosen, filters,
                                                                  include_facility, group_by)
                result["price_explained"] = price_explained(conn, code, source_file_id, chosen, filters)
                # Names for the procedure codes and revenue codes in the groups and this page of results.
                rows, groups = result["results"], result["price_groups"]
                result["facility_count"] = facility_count(conn, code, source_file_id, chosen) if include_facility else 0
                result["procedure_names"] = procedure_names(
                    conn, {g["procedure"] for g in groups} | {r["procedure"] for r in rows})
                codes = {r["billing_code"] for r in rows if r["billing_code_type"] == "RC"} | {
                    g["billed_as"].split(" ")[1].lstrip("0") for g in groups if g["billed_as"].startswith("RC ")}
                departments = all_departments(conn)
                result["department_names"] = {c: departments[c] for c in sorted(codes) if c in departments}
                if chosen_plan:
                    result["plan"] = chosen_plan
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        except ValueError as exc:  # an unknown operator
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return result


    @app.get("/api/suggest/{kind}")
    def api_suggest(kind: str, q: str = "", billing_code: str = "", limit: int = Query(20, ge=1, le=200)):
        """NPIs or EINs (kind npi / ein) starting with q, for the search bar's boxes (#23): among the billing
        code's rates while it is a valid code, else every rate. {"billing_code", "q", "suggestions": [{"value",
        "label" (a business name), "names", "rates"}]}, most rates first."""
        suggest = {"npi": suggest_npis, "ein": suggest_eins}.get(kind)
        if suggest is None:
            return JSONResponse({"detail": "suggest npi or ein"}, status_code=404)
        code, error = validate_code(billing_code)
        code = None if error else code
        try:
            with closing(connect()) as conn:
                found = suggest(conn, q, code, limit)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        return {"billing_code": code, "q": q, "suggestions": found}

    def all_codes(source_file_ids: list[int] | None = None) -> list[dict]:
        """The code list for source_file_ids (None: every file), read once per version of the DB file and file set
        (grouping 1M+ rows takes ~0.7 s)."""
        with closing(connect()) as conn:
            key = None if source_file_ids is None else tuple(sorted(source_file_ids))
            version = db_file_version(conn)  # the active DB file and its mtime
            if cached_codes.get("version") != version:
                cached_codes.clear()
                cached_codes["version"] = version
            if key not in cached_codes:
                cached_codes[key] = list_codes(conn, source_file_ids)
        return cached_codes[key]

    @app.get("/api/codes")
    def api_codes(q: str = "", source_file_id: list[int] | None = Query(None)):
        """Every code (or the codes in source_file_id's files) whose code, name or description contains q."""
        try:
            codes = all_codes(source_file_ids=source_file_id or None)
        except NoData:
            return JSONResponse({"detail": NO_DATA}, status_code=503)
        return {"q": q, "codes": filter_codes(codes, q)}

    if dev_tools:
        @app.post("/dev/restart", response_class=HTMLResponse)
        def dev_restart():
            """Local dev only: touching the trigger file makes `uvicorn --reload` restart the whole process
            (same approach as up-football's admin_dev_restart)."""
            restart_trigger.write_text(
                '"""ui/_dev_reload_trigger.py — see dev_restart() in app.py."""\n'
                f"# last triggered: {datetime.now(timezone.utc).isoformat()}\n"
            )
            return HTMLResponse(
                '<meta http-equiv="refresh" content="2;url=/by-procedure">'
                '<p style="font-family: system-ui, sans-serif;">Restarting local server&hellip; '
                'back to <a href="/by-procedure">Prices By Procedure</a> in a couple of seconds.</p>'
            )

    return app


# RATES_DB pins one database file; otherwise the index databases in PARSED_DIR (default data/parsed), one active.
app = create_app(os.environ.get("RATES_DB") or None, parsed_dir=Path(os.environ.get("PARSED_DIR", "data/parsed")),
                 dev_tools=os.environ.get("DEV_TOOLS") == "1")
