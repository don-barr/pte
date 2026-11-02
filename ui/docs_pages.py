"""Docs (#30): the project's Markdown docs rendered inside the app.

Only a fixed allow-list of files is served (never a path from the request). Each file is read on every request,
since the docs change while we work. Raw HTML in the Markdown is shown as text, never run; HTML comments are
dropped. Relative links between the docs are rewritten to their routes here, and images under docs/ are served
from /docs/assets/<name>.
"""

import posixpath
import re
from pathlib import Path
from urllib.parse import urlsplit

import markdown
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from markdown.treeprocessors import Treeprocessor

REPO_ROOT = Path(__file__).resolve().parent.parent

# slug -> (path under the repo root, title). The first is what /docs shows.
DOCS = {
    "readme": ("README.md", "README"),
    "modifiers": ("docs/modifiers.md", "Billing Code Modifiers"),
}
# What each doc holds, in a line, on the switcher at the top of every doc.
BLURBS = {
    "readme": "Getting started, design decisions, features and what the data showed",
    "modifiers": "The modifiers in the data, what each one means and how it changes the price",
}
PATH_TO_SLUG = {path: slug for slug, (path, _) in DOCS.items()}

ASSETS_DIR = "docs"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".svg": "image/svg+xml", ".webp": "image/webp"}
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_SCHEMES = {"http", "https", "mailto"}
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class _DropComments(Preprocessor):
    """Remove HTML comments. Runs after fenced code is stashed, so comments shown in code blocks survive."""

    def run(self, lines):
        return COMMENT.sub("", "\n".join(lines)).split("\n")


class _RewriteLinks(Treeprocessor):
    """Point links to other docs at their routes and images under docs/ at /docs/assets; drop unsafe URLs."""

    def __init__(self, md, doc_path: str):
        super().__init__(md)
        self.doc_dir = posixpath.dirname(doc_path)

    def run(self, root):
        for el in root.iter("a"):
            href = self.rewrite_link(el.get("href", ""))
            if href is None:
                el.attrib.pop("href", None)
            else:
                el.set("href", href)
        for el in root.iter("img"):
            src = self.rewrite_image(el.get("src", ""))
            if src is None:
                el.attrib.pop("src", None)
            else:
                el.set("src", src)

    def resolve(self, path: str) -> str | None:
        """A relative path in this doc → a path from the repo root, or None if it leaves the repo."""
        joined = posixpath.normpath(posixpath.join(self.doc_dir, path))
        return None if joined.startswith("..") or joined.startswith("/") else joined

    def rewrite_link(self, href: str) -> str | None:
        parts = urlsplit(href.strip())
        if parts.scheme or parts.netloc:
            return href if parts.scheme.lower() in SAFE_SCHEMES else None
        if not parts.path:
            return href  # "#anchor" within this page
        target = self.resolve(parts.path)
        if target in PATH_TO_SLUG:
            return f"/docs/{PATH_TO_SLUG[target]}" + (f"#{parts.fragment}" if parts.fragment else "")
        if target is not None and posixpath.dirname(target) == ASSETS_DIR and asset_type(posixpath.basename(target)):
            return f"/docs/assets/{posixpath.basename(target)}"
        return None  # a repo file we don't serve

    def rewrite_image(self, src: str) -> str | None:
        parts = urlsplit(src.strip())
        if parts.scheme or parts.netloc:
            return src if parts.scheme.lower() in {"http", "https"} else None
        target = self.resolve(parts.path)
        if target is not None and posixpath.dirname(target) == ASSETS_DIR and asset_type(posixpath.basename(target)):
            return f"/docs/assets/{posixpath.basename(target)}"
        return None


class _SafeDocs(Extension):
    """Escape raw HTML (block and inline), drop comments, rewrite links for the doc at doc_path."""

    def __init__(self, doc_path: str):
        super().__init__()
        self.doc_path = doc_path

    def extendMarkdown(self, md):
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        # fenced_code_block runs at 25; this at 20, so code blocks are already stashed.
        md.preprocessors.register(_DropComments(md), "drop_comments", 20)
        md.treeprocessors.register(_RewriteLinks(md, self.doc_path), "rewrite_links", 1)


def asset_type(name: str) -> str | None:
    """The media type of an image name under docs/ we serve, or None."""
    if not ASSET_NAME.match(name):
        return None
    return IMAGE_TYPES.get(Path(name).suffix.lower())


def count_headings(tokens: list) -> int:
    return sum(1 + count_headings(t["children"]) for t in tokens)


def render(text: str, doc_path: str) -> tuple[str, str, list]:
    """Markdown → (body HTML, table-of-contents HTML, toc tokens)."""
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "toc", _SafeDocs(doc_path)],
        extension_configs={"toc": {"permalink": "#", "permalink_title": "Link to this section", "toc_depth": "2-3"}},
        output_format="html",
    )
    body = md.convert(text)
    return body, md.toc, md.toc_tokens


def make_router(templates, root: Path = REPO_ROOT) -> APIRouter:
    """Routes: /docs (the README), /docs/index, /docs/{slug}, /docs/assets/{name}. root: where the docs live."""
    root = Path(root)
    router = APIRouter()
    def listing() -> list[dict]:
        return [{"slug": slug, "title": title, "path": path, "exists": (root / path).is_file(),
                 "blurb": BLURBS.get(slug, "")}
                for slug, (path, title) in DOCS.items()]

    def show(request: Request, slug: str) -> HTMLResponse:
        if slug not in DOCS:
            return templates.TemplateResponse(
                request, "docs_index.html", {"docs": listing(), "missing_slug": slug}, status_code=404)
        path, title = DOCS[slug]
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            text = None
        context = {"docs": listing(), "slug": slug, "title": title, "path": path,
                   "body": None, "toc": "", "has_toc": False}
        if text is not None:
            body, toc, tokens = render(text, path)
            context.update(body=body, toc=toc, has_toc=count_headings(tokens) > 0)
        return templates.TemplateResponse(request, "doc.html", context)

    @router.get("/docs", response_class=HTMLResponse)
    def docs_home(request: Request):
        return show(request, "readme")

    @router.get("/docs/", include_in_schema=False)
    def docs_slash():
        return RedirectResponse("/docs", status_code=307)

    @router.get("/docs/index", response_class=HTMLResponse)
    def docs_index(request: Request):
        return templates.TemplateResponse(request, "docs_index.html", {"docs": listing(), "missing_slug": None})

    @router.get("/docs/assets/{name}")
    def docs_asset(request: Request, name: str):
        media_type = asset_type(name)
        file = root / ASSETS_DIR / name
        if media_type is None or not file.is_file():
            return templates.TemplateResponse(
                request, "docs_index.html", {"docs": listing(), "missing_slug": name}, status_code=404)
        return FileResponse(file, media_type=media_type)

    @router.get("/docs/{slug}", response_class=HTMLResponse)
    def docs_page(request: Request, slug: str):
        return show(request, slug)

    return router
