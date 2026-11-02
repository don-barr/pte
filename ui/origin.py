"""Same-origin check for the app's POSTs (#33): a page on another site can't make the browser change anything here."""

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse


def foreign_origin(request: Request) -> str | None:
    """The other site a POST came from, or None when it came from this app (or names no origin at all, as a script or
    curl does). Browsers send Origin with every form POST; Referer is the fallback."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return None
    own = request.headers.get("host", "")
    parts = urlsplit(source)
    if source == "null" or parts.scheme not in ("http", "https") or parts.netloc != own:
        return source
    return None


def refused(source: str, nothing: str = "nothing was opened or loaded", href: str = "/",
            link: str = "Open the Load Data page") -> HTMLResponse:
    """403 for a POST from another site: says nothing happened and links to the page to do it from."""
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Refused</title>"
        '<p style="font-family: system-ui, sans-serif">Refused: this request came from another site '
        f"({_escape(source)}), not from this app’s own pages, so {nothing}. "
        f'<a href="{href}">{link}</a> and do it from there.</p>', status_code=403)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
