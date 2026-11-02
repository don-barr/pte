"""Three layers, each doing one job (README: Separation Of Responsibilities): the ETL writes the database, the API
answers questions about it with SQL, the UI shows the answers. The UI runs no SQL, the API never builds pages or
imports the UI, pages don't import each other, and the ETL never imports the app. Checked on the source itself, so a
shortcut can't slip back in."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["ui/procedure_page.py", "ui/provider_page.py", "ui/load_page.py", "ui/docs_pages.py"]
UI = sorted(f"ui/{p.name}" for p in (ROOT / "ui").glob("*.py"))
API = sorted(f"api/{p.name}" for p in (ROOT / "api").glob("*.py"))
WEB = ("fastapi", "starlette", "jinja2")


def tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"), path)


def imported(path: str) -> set[str]:
    names = set()
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def database_calls(path: str) -> list[str]:
    """conn.execute(...), .executemany(...), .executescript(...) and sqlite3.connect(...) calls, by line."""
    found = []
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            owner = node.func.value
            if name in ("execute", "executemany", "executescript") or (
                    name == "connect" and isinstance(owner, ast.Name) and owner.id == "sqlite3"):
                found.append(f"{path}:{node.lineno} .{name}()")
    return found


class LayerTest(unittest.TestCase):
    def test_the_ui_runs_no_sql_and_opens_no_database(self):
        for path in UI:
            with self.subTest(path=path):
                self.assertEqual(database_calls(path), [])

    def test_the_api_knows_nothing_about_the_web_or_the_ui(self):
        ui = {p[:-3].replace("/", ".") for p in UI}
        for path in API:
            with self.subTest(path=path):
                self.assertEqual(sorted(m for m in imported(path) if m.split(".")[0] in WEB or m in ui), [])

    def test_pages_do_not_import_each_other(self):
        modules = {p[:-3].replace("/", ".") for p in PAGES}
        for path in PAGES:
            with self.subTest(path=path):
                self.assertEqual(sorted(imported(path) & modules), [])

    def test_the_etl_never_imports_the_app(self):
        for path in sorted((ROOT / "etl").glob("*.py")):
            with self.subTest(path=path.name):
                self.assertEqual(sorted(m for m in imported(f"etl/{path.name}") if m.split(".")[0] in ("api", "ui", "app")), [])

    def test_every_layer_is_its_own_folder(self):
        self.assertTrue(PAGES and set(PAGES) <= set(UI))
        self.assertIn("ui/labels.py", UI)
        self.assertNotIn("api/meanings.py", API)
        self.assertTrue((ROOT / "app.py").is_file())


if __name__ == "__main__":
    unittest.main()
