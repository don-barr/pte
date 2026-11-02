"""The Load Data page (/, with /load/open, /load/upload, /load/status/<job>, /load/preview, /indexes/show and
/indexes/remove) and the health check (/api/health).

The page leads with the active index (#41.3), then the other loaded indexes, then "Load an index".
"Loaded indexes" (#37) lists the index databases (one per index, etl.indexes.IndexStore): each says which index it
was built from and which of that index's in-network files it holds, each loaded file with the same preview as step 2,
and offers Show this one (make it the active index every other page reads) and Remove (backed up first, after a
confirmation).

Step 1 opens an index file: one from the list in index_dir (a GET link), or another one by URL or local path, or
from the user's computer (POSTs that save it into index_dir, so whatever is opened joins the list). Steps 2-3 load
the chosen in-network files in a background thread, one load at a time, while /load/status/<job> shows progress.
Loading builds the index's own database (an existing one is backed up and replaced only if a file loaded) and makes
it the active index.
Every POST must come from this app's own pages (same Origin, or Referer when there is no Origin).
Each file in step 2 has a preview (#35): its plans from the index, and its local copy's size, date and header fields
(read from the first bytes only). It never fetches anything, so it is also a GET: /load/preview (the fragment) and
/api/load/preview (JSON), for an index in the list.
"""

import ntpath
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2.filters import do_filesizeformat
from starlette.concurrency import run_in_threadpool

from etl.indexes import IndexStore
from etl.run import (
    SOURCES, IndexReadError, index_files, list_local_indexes, read_file_header, read_index, run_load,
    parse_index_bytes, recover_interrupted,
    save_index_upload,
)
from etl.schema import local_path_for
from etl.validate import validate_index
from ui.origin import foreign_origin, refused
from api.index_db import index_info, load_status, source_rows
from api.search import file_name, short_name
from api.errors import NO_DATA

NOT_LISTED = ("{location} isn’t in your index files. Open it with “Open Another” below: a URL is downloaded and a "
              "path is copied into the list first.")

# Big loads (#42.3, #42.5): a heads-up when this many files, or this many known bytes, are ticked; Load refuses to
# start with less than MIN_FREE_BYTES free (two Ambetter files, 39 MB of JSON, made a 2.4 GB database).
BIG_FILE_COUNT = 5
BIG_BYTES = 200 * 1000 ** 2
MIN_FREE_BYTES = 2 * 1000 ** 3
LOW_DISK = ("Only {free} is free on the disk that holds the index databases; a load needs at least {floor} free "
            "(a database can grow to several GB), so it didn’t start. Free some space and try again.")
INTERRUPTED = "A load of {index} was interrupted at {when} UTC; nothing was changed."
GONE_FROM_LIST = "That index file no longer exists ({name}); it was removed from your index files."
NOT_IN_INDEX = "{url} isn’t one of this index’s in-network files."
NO_SUCH_DB = "There’s no index database called “{name}”."
# The header fields a preview shows; read_file_header finds them before a CMS file's big arrays.
PREVIEW_HEADER_FIELDS = ("reporting_entity_name", "reporting_entity_type", "version", "last_updated_on")


def file_preview(files: list[dict], url: str, raw_dir: Path, db_row: dict | None) -> dict:
    """What one in-network file covers, without loading or fetching it. files: index_files() of its index; db_row:
    its source_files row ({"rate_count", "loaded_at", "error"}) or None. Plans are de-duplicated and sorted by name,
    then plan ID; "names" groups their IDs by name; "shared_with" lists the index's other files that cover any of the
    same plans (same plan ID and type); "local" is None when there is no local copy."""
    (file,) = [f for f in files if f["url"] == url]

    def plan_key(plan: dict) -> tuple:
        return tuple(str(plan.get(k) or "") for k in ("plan_name", "plan_id", "plan_id_type", "plan_market_type",
                                                      "issuer_name"))

    plans = sorted({plan_key(p): p for p in file["plans"]}.values(), key=plan_key)
    names: dict[str, list[str]] = {}
    for p in plans:
        ids = names.setdefault(p.get("plan_name") or "", [])
        if p.get("plan_id") not in ids:
            ids.append(p.get("plan_id"))
    ids = {(p.get("plan_id_type"), p.get("plan_id")) for p in plans}
    shared = []
    for other in files:
        common = ids & {(p.get("plan_id_type"), p.get("plan_id")) for p in other["plans"]}
        if other["url"] != url and common:
            shared.append({"url": other["url"], "file": other["file"],
                           "plan_ids": sorted({str(plan_id) for _, plan_id in common})})
    local = None
    path = local_path_for(url, raw_dir)
    if path.is_file():
        stat = path.stat()
        try:
            header = read_file_header(path)
        except OSError:
            header = {}
        local = {"path": str(path), "size": stat.st_size,
                 "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                 "header": {k: header.get(k) for k in PREVIEW_HEADER_FIELDS}}
    return {"url": url, "file": file["file"], "plan_count": len(plans), "name_count": len(names),
            "plan_id_count": len(ids), "plans": plans,
            "names": [{"plan_name": n, "plan_ids": sorted(i, key=str)} for n, i in names.items()],
            "shared_with": shared, "local": local, "loaded": db_row}


def disk_free(path: Path) -> int | None:
    """Free bytes on the disk holding path (or its nearest existing parent); None if that can't be told."""
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def describe_db(db_path: Path, raw_dir: Path, active: bool) -> dict | None:
    """One "Loaded indexes" entry (#37.2, #37.6): {"name", "active", "info" (read_index_info, or None for a database
    from before #37), "files", "loaded_count", "listed_count"}. "files" has every file the index lists, in index
    order, then any other file the database holds: {"url", "file", "short_name", "listed", "loaded" (rates loaded,
    no error), "row" (its source_files row or None), "preview" (file_preview, for a listed file)}. None for a file
    that isn't an index database (no source_files table)."""
    rows = source_rows(db_path)
    if rows is None:
        return None
    info = index_info(db_path)
    listed = info["files"] if info else []
    urls = [f["url"] for f in listed] + [url for url in rows if url not in {f["url"] for f in listed}]
    files = []
    for url in urls:
        row = rows.get(url)
        is_listed = any(f["url"] == url for f in listed)
        files.append({"url": url, "file": file_name(url), "short_name": short_name(file_name(url)),
                      "listed": is_listed, "row": row, "loaded": row is not None and row["error"] is None,
                      "preview": file_preview(listed, url, raw_dir, row) if is_listed else None})
    return {"name": Path(db_path).name, "active": active, "info": info, "files": files,
            "loaded_count": sum(1 for f in files if f["loaded"] and f["listed"]), "listed_count": len(listed)}


def link_indexes(local_indexes: list[dict], entries: list[dict], store: IndexStore) -> None:
    """Link step 1's index files and the loaded index databases both ways (#41.2), in place: each local index gets
    "db" (the name of its loaded database, or None) and "db_active"; each entry gets "index_path" (its index file in
    the list, or None). An index file's database is the one loading it builds (store.db_for), and, when the database
    records its index, only if that index has the same file name."""
    by_name = {e["name"]: e for e in entries}
    for e in entries:
        e.setdefault("index_path", None)
    for i in local_indexes:
        entry = by_name.get(store.db_for(i["path"]).name)
        if entry is not None and (entry["info"] is None or entry["info"]["file_name"] == i["name"]):
            i["db"], i["db_active"] = entry["name"], entry["active"]
            if entry["index_path"] is None:
                entry["index_path"] = i["path"]
        else:
            i["db"], i["db_active"] = None, False


FILE_STAGES = ("waiting", "downloading", "reading", "saving", "loaded")


class Jobs:
    """Background loads, one at a time. Each job: {"id", "state" ("running" or "done"), "location", "index" (its
    file name), "files", "stage" ("loading", then "activating"), "started", "started_at", "result", "error",
    "events"}. Each file: {"url", "file", "source", "stage" (FILE_STAGES), "bytes", "total", "rows", "db_bytes",
    "rate_count", "error"}. The load's thread reports progress (etl.run.run_load's events) through progress(); pages
    read a consistent copy with snapshot()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.running: str | None = None

    def start(self, location: str, files: list[dict], work) -> tuple[dict, bool]:
        """(the new job, True), or (the job already running, False). work(report) runs in a thread; report(event)
        records one progress event."""
        with self.lock:
            if self.running is not None:
                return self.jobs[self.running], False
            job = {"id": secrets.token_hex(8), "state": "running", "location": location,
                   "index": Path(location).name,
                   "files": [dict(f, stage="waiting", bytes=None, total=None, rows=None, db_bytes=None,
                                  rate_count=None, error=None) for f in files],
                   "stage": "loading", "started": time.monotonic(),
                   "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "result": None, "error": None, "events": 0}
            self.jobs[job["id"]] = job
            self.running = job["id"]
        threading.Thread(target=self._run, args=(job, work), daemon=True, name=f"load-{job['id']}").start()
        return job, True

    def _run(self, job: dict, work) -> None:
        try:
            result = work(lambda event: self.progress(job["id"], event))
            with self.lock:
                job["result"] = result
        except Exception as exc:  # shown on the status page rather than lost in a thread
            with self.lock:
                job["error"] = f"The load stopped: {type(exc).__name__}: {exc}"
        finally:
            with self.lock:
                job["state"] = "done"
                job["seconds"] = time.monotonic() - job["started"]
                self.running = None

    def progress(self, job_id: str, event: dict) -> None:
        """Apply one run_load progress event to the job."""
        with self.lock:
            job = self.jobs[job_id]
            job["events"] += 1
            if event["stage"] == "activating":
                job["stage"] = "activating"
                return
            for f in job["files"]:
                if f["url"] == event.get("url"):
                    f["stage"] = event["stage"]
                    f.update({k: v for k, v in event.items() if k not in ("stage", "url")})

    def snapshot(self, job_id: str) -> dict | None:
        """A copy of the job, with "elapsed" (seconds), "files_done", "files_total", "rates" (saved so far) and
        "current" (the file being worked on, or None). None for an unknown job."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            copy = {k: v for k, v in job.items() if k != "files"}
            copy["files"] = [dict(f) for f in job["files"]]
        files = copy["files"]
        copy["elapsed"] = copy.get("seconds") or time.monotonic() - copy["started"]
        copy["files_done"] = sum(1 for f in files if f["stage"] == "loaded")
        copy["files_total"] = len(files)
        copy["rates"] = sum((f["rate_count"] or 0) if f["stage"] == "loaded" else (f["rows"] or 0)
                            for f in files if f["stage"] in ("loaded", "saving"))
        copy["current"] = next((f for f in files if f["stage"] in ("downloading", "reading", "saving")), None)
        return copy

    def current(self) -> dict | None:
        """A snapshot of the running job, or None."""
        with self.lock:
            running = self.running
        return self.snapshot(running) if running else None


def make_router(connect, templates, no_data_exc, store: IndexStore, raw_dir: Path, index_dir: Path,
                download: bool) -> APIRouter:
    """store: the index databases and which is active; connect() opens the active one (or raises no_data_exc)."""
    router = APIRouter()
    jobs = Jobs()
    # Notices the next Load Data page shows once, then forgets: {"text", "href" (or None), "link"}.
    flashes: list[dict] = []
    flash_lock = threading.Lock()

    def flash(text: str, href: str | None = None, link: str = "") -> None:
        with flash_lock:
            flashes.append({"text": text, "href": href, "link": link})

    def take_flashes() -> list[dict]:
        with flash_lock:
            taken = flashes[:]
            flashes.clear()
        return taken

    def loaded_indexes() -> list[dict]:
        active = store.active()
        return [entry for path in store.dbs() if (entry := describe_db(path, raw_dir, path == active)) is not None]

    def read_for_page(location: str) -> dict:
        """The index's files (each with its preview) and schema problems for the page, or the reason it couldn't be
        read. "Loaded now" refers to this index's own database."""
        try:
            index, path = read_index(location, index_dir)
        except IndexReadError as exc:
            return {"index_error": str(exc), "files": [], "problems": []}
        db = store.db_for(path)
        rows, listed = source_rows(db) or {}, index_files(index)
        files = []
        for f in listed:
            preview = file_preview(listed, f["url"], raw_dir, rows.get(f["url"]))
            # What Load would fetch (#41.5): the host, and the size of the local copy (the only size known without
            # fetching anything).
            files.append(dict(f, on_disk=local_path_for(f["url"], raw_dir).exists(),
                              loaded=f["url"] in rows and rows[f["url"]]["error"] is None, preview=preview,
                              size=preview["local"]["size"] if preview["local"] else None,
                              host=urlsplit(f["url"]).hostname or f["url"]))
        return {"index": index, "index_path": str(path), "files": files, "index_error": None,
                "problems": validate_index(index), "target_db": db.name, "target_exists": db.is_file(),
                "target_loaded": sum(1 for f in files if f["loaded"]), "free_bytes": disk_free(db.parent),
                "min_free_bytes": MIN_FREE_BYTES, "big_file_count": BIG_FILE_COUNT, "big_bytes": BIG_BYTES}

    def in_list(location: str) -> Path | None:
        """location as a file directly in index_dir (the list), else None. Never a URL."""
        if not location or urlsplit(location).scheme:
            return None
        path = Path(location).expanduser()
        try:
            if path.resolve().parent == Path(index_dir).resolve() and path.is_file():
                return path
        except OSError:
            pass
        return None

    work_dir = store.fixed.parent if store.fixed is not None else store.parsed_dir

    def recover() -> None:
        """Clean up after loads killed part-way and say so once (#42.3). Only while no load of ours runs."""
        if jobs.current() is not None:
            return
        for found in recover_interrupted(work_dir):
            when = found["at"][:16].replace("T", " ")
            location = found["index_location"]
            href = f"/?index={quote(location, safe='')}#load" if location and in_list(location) else "/#load"
            flash(INTERRUPTED.format(index=found["index"], when=when), href, "Load again?")

    def gone_from_list(location: str) -> bool:
        """location names a file directly in index_dir that no longer exists (e.g. deleted in Finder)."""
        if not location or urlsplit(location).scheme:
            return False
        path = Path(location).expanduser()
        try:
            return path.resolve().parent == Path(index_dir).resolve() and not path.exists()
        except OSError:
            return False

    def load_context(request: Request, location: str = "") -> dict:
        recover()
        local, indexes = list_local_indexes(index_dir), loaded_indexes()
        link_indexes(local, indexes, store)
        indexes.sort(key=lambda e: not e["active"])  # the active index first (#41.3); stable, so newest next
        return {"location": location, "open_location": "", "files": [], "index_error": None, "problems": [],
                "error": None, "result": None, "open_error": None, "job": None, "running": jobs.current(),
                "local_indexes": local, "indexes": indexes,
                "remove_ask": None, "notice": None, "index_notice": None, "downloads_on": download,
                "progress_here": False, "flashes": take_flashes()}

    def page(request: Request, context: dict, status_code: int = 200):
        return templates.TemplateResponse(request, "load.html", context, status_code=status_code)

    def opened(path: Path) -> RedirectResponse:
        return RedirectResponse(f"/?index={quote(str(path), safe='')}", status_code=303)

    def open_failed(request: Request, location: str, message: str):
        context = load_context(request)
        context.update(open_location=location, open_error=message)
        return page(request, context, 400)

    @router.get("/", response_class=HTMLResponse)
    def home(request: Request, index: str = "", remove: str = ""):
        """The Load Data page. Shows one index from the list in step 2; reads nothing else: a URL or a path outside
        the list must be opened with a POST to /load/open, so a link can't make the app download or read files.
        remove=<db name> asks to confirm removing that index database."""
        if "billing_code" in request.query_params:  # links from before search moved off / keep working (via /search)
            return RedirectResponse(f"/search?{request.url.query}", status_code=307)
        if gone_from_list(index.strip()):  # said once, rather than an error on every reload (#42)
            flash(GONE_FROM_LIST.format(name=Path(index.strip()).name))
            return RedirectResponse("/", status_code=303)
        context = load_context(request, index.strip())
        if remove and store.find(remove) is not None:
            context["remove_ask"] = remove
        if context["location"]:
            if in_list(context["location"]) is None:
                context["index_error"] = NOT_LISTED.format(location=context["location"])
            else:
                context.update(read_for_page(context["location"]))
        return page(request, context)

    @router.get("/load")
    def load_page(request: Request):
        """The Load Data page moved to / (#37.1)."""
        return RedirectResponse("/" + (f"?{request.url.query}" if request.url.query else ""), status_code=307)

    def preview_for(index: str, url: str) -> tuple[dict | None, str | None]:
        """(the preview of url's file, None), or (None, why not): the index must be in the list and list url."""
        if (path := in_list(index.strip())) is None:
            return None, NOT_LISTED.format(location=index.strip() or "(no index given)")
        try:
            parsed, _ = read_index(str(path), index_dir)
        except IndexReadError as exc:
            return None, str(exc)
        files = index_files(parsed)
        if url not in {f["url"] for f in files}:
            return None, NOT_IN_INDEX.format(url=url or "(no file given)")
        return file_preview(files, url, raw_dir, (source_rows(store.db_for(path)) or {}).get(url)), None

    @router.get("/load/preview", response_class=HTMLResponse)
    def load_preview(request: Request, index: str = "", url: str = ""):
        """One file's preview as an HTML fragment (what step 2 shows under the file). Reads, never fetches."""
        preview, why = preview_for(index, url)
        if preview is None:
            return HTMLResponse(f'<div class="notice error" role="alert">{escape(why)}</div>', status_code=404)
        return templates.TemplateResponse(request, "_file_preview.html", {"p": preview})

    @router.get("/api/load/preview")
    def api_load_preview(index: str = "", url: str = ""):
        preview, why = preview_for(index, url)
        if preview is None:
            return JSONResponse({"detail": why}, status_code=404)
        return preview

    @router.post("/load/open", response_class=HTMLResponse)
    async def load_open(request: Request):
        """Open an index by https URL (downloaded into index_dir) or local path (copied into index_dir unless it is
        already there), then show it from the list."""
        if (source := foreign_origin(request)) is not None:
            return refused(source)
        location = ((await request.form()).get("location") or "").strip()
        if not location:
            return open_failed(request, location, "Give an https:// URL or a local path.")
        if (listed := in_list(location)) is not None:
            return opened(listed)
        try:
            _, path = await run_in_threadpool(read_index, location, index_dir)
        except IndexReadError as exc:
            return open_failed(request, location, str(exc))
        if Path(path).resolve().parent != Path(index_dir).resolve():  # a local file elsewhere: copy it into the list
            try:
                path = save_index_upload(Path(path).name, Path(path).read_bytes(), index_dir)
            except (ValueError, OSError) as exc:
                return open_failed(request, location, str(exc))
        return opened(path)

    @router.post("/load/upload", response_class=HTMLResponse)
    async def load_upload(request: Request, file: UploadFile):
        if (source := foreign_origin(request)) is not None:
            return refused(source)
        content = await file.read()
        try:
            # Check it is an index before saving, so a wrong file never joins the list.
            if (file.filename or "").lower().endswith(".json"):
                await run_in_threadpool(parse_index_bytes, ntpath.basename(file.filename), content)
            path = save_index_upload(file.filename, content, index_dir)
        except (ValueError, IndexReadError) as exc:
            return open_failed(request, "", str(exc))
        return opened(path)

    @router.post("/load", response_class=HTMLResponse)
    async def load_submit(request: Request):
        """Load the chosen files: builds the index's own database (store.db_for), backing up and replacing an existing
        one only if a file loaded, then makes it the active index (#37.3, #37.5)."""
        if (source := foreign_origin(request)) is not None:
            return refused(source)
        form = await request.form()
        location = (form.get("index") or "").strip()
        urls = form.getlist("url")
        # One radio group per file, named "source:<url>"; a file without one gets run_load's default.
        sources = {url: form[f"source:{url}"] for url in urls if f"source:{url}" in form}
        context = load_context(request, location)
        context.update(await run_in_threadpool(read_for_page, location))
        if context["index_error"] is None and context["problems"]:
            context["error"] = "This index doesn’t match the CMS schema, so nothing was loaded."
        elif context["index_error"] is None:
            if not urls:
                context["error"] = "Pick at least one file to load."
            elif any(source not in SOURCES for source in sources.values()):
                context["error"] = "Choose where each file comes from: local copy, download and keep, or the web."
            elif (free := disk_free(store.db_for(context["index_path"]).parent)) is not None \
                    and free < MIN_FREE_BYTES:
                context["error"] = LOW_DISK.format(free=do_filesizeformat(free), floor=do_filesizeformat(MIN_FREE_BYTES))
                return page(request, context, 507)
            else:
                index, index_path = context["index"], context["index_path"]
                db = store.db_for(index_path)
                names = {f["url"]: f["file"] for f in context["files"]}
                chosen = [{"url": url, "file": names.get(url, url), "source": sources.get(url)}
                          for url in dict.fromkeys(urls)]

                def work(report) -> dict:
                    result = run_load(index, urls, db, raw_dir, "replace", store.backup_dir, download, sources,
                                      index_location=index_path, progress=report)
                    result["db"] = db.name
                    if result["replaced"]:
                        store.set_active(db.name)
                    return result

                job, started = jobs.start(location, chosen, work)
                if not started:
                    context["error"] = "A load is already running, so this one didn’t start. Try again when it’s done."
                    context["running"] = jobs.snapshot(job["id"])
                    return page(request, context, 409)
                # Straight to the progress, in place of the form (#42.6).
                return RedirectResponse(f"/load/status/{job['id']}#load-progress", status_code=303)
        return page(request, context)

    @router.get("/load/status/{job_id}", response_class=HTMLResponse)
    def load_status_page(request: Request, job_id: str):
        job = jobs.snapshot(job_id)
        if job is None:
            context = load_context(request)
            context["open_error"] = "That load isn’t known (the app may have restarted since)."
            return page(request, context, 404)
        context = load_context(request, job["location"])
        context["job"] = job
        if job["state"] == "done":
            context["result"] = job["result"]
            context["error"] = job["error"]
            context["running"] = None
            context.update(read_for_page(job["location"]))  # with the new "loaded now" marks
        elif context["running"] and context["running"]["id"] == job_id:
            context["progress_here"] = True  # in place of the form it came from (#42.6)
        return page(request, context)

    @router.get("/load/progress/{job_id}", response_class=HTMLResponse)
    def load_progress_fragment(request: Request, job_id: str):
        """The running load's progress panel (what the page's script swaps in), or a marker once it's done."""
        job = jobs.snapshot(job_id)
        if job is None:
            return HTMLResponse('<div data-job="unknown"></div>', status_code=404)
        if job["state"] == "done":
            return HTMLResponse(f'<div data-job="done" data-status-url="/load/status/{job_id}"></div>')
        return templates.TemplateResponse(request, "_load_progress.html", {"job": job})

    @router.get("/api/load/progress")
    def api_load_progress():
        """The running load's progress (Jobs.snapshot, without its result), or {"running": false, "job": null}."""
        job = jobs.current()
        if job is None:
            return {"running": False, "job": None}
        return {"running": True, "job": {k: v for k, v in job.items() if k not in ("result", "started")}}

    def db_named(request: Request, name: str):
        """(the database called name, None), or (None, a 404 page saying there's no such database)."""
        if (db := store.find(name)) is not None:
            return db, None
        context = load_context(request)
        context["index_notice"] = {"error": True, "text": NO_SUCH_DB.format(name=name)}
        return None, page(request, context, 404)

    @router.post("/indexes/show", response_class=HTMLResponse)
    async def show_index(request: Request):
        """Make one index database the active one: every page reads it from now on."""
        if (source := foreign_origin(request)) is not None:
            return refused(source, "no index was switched", "/#indexes", "Open the Load Data page")
        name = str((await request.form()).get("name") or "")
        db, missing = db_named(request, name)
        if db is None:
            return missing
        store.set_active(name)
        return RedirectResponse("/#indexes", status_code=303)

    @router.post("/indexes/remove", response_class=HTMLResponse)
    async def remove_index(request: Request):
        """Remove one index database after confirm=yes (without it, ask): backed up to the backups folder first."""
        if (source := foreign_origin(request)) is not None:
            return refused(source, "no index was removed", "/#indexes", "Open the Load Data page")
        form = await request.form()
        name = str(form.get("name") or "")
        db, missing = db_named(request, name)
        if db is None:
            return missing
        context = load_context(request)
        if form.get("confirm") != "yes":
            context["remove_ask"] = name
            return page(request, context)
        if jobs.current() is not None:
            context["index_notice"] = {"error": True, "text": "A load is running, so nothing was removed. "
                                                              "Try again when it’s done."}
            return page(request, context, 409)
        backup = await run_in_threadpool(store.remove, name)
        context = load_context(request)
        context["index_notice"] = {"error": False, "text": f"Removed {name}. It was backed up to {backup} first."}
        return page(request, context)

    recover()  # on startup: a load the last run of the app was doing when it stopped

    @router.get("/api/health")
    def health():
        active = store.active()
        status = load_status(active)
        if status is None:
            return {"db_present": False, "db_path": str(active) if active else None, "active_index": None,
                    "detail": NO_DATA, "rate_count": 0, "source_files": []}
        body = {"db_present": True, "db_path": str(active), "active_index": active.name,
                "rate_count": status["rate_count"],
                "source_files": [{k: v for k, v in f.items() if k != "id"} for f in status["source_files"]]}
        if status["rate_count"] == 0:
            body["detail"] = NO_DATA
        body["loading"] = jobs.current() is not None
        return body

    return router
