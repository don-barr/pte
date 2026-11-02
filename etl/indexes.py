"""One database per index (#37.5).

Loading an index builds data/parsed/<index file stem>.db holding only that index's chosen in-network files, with the
index itself recorded inside (etl.run.write_index_info). The app shows one index database at a time, the active one:
the name saved in data/parsed/active_index, or the newest database when nothing (or nothing that still exists) is
saved. IndexStore lists the databases, switches the active one, and removes one after backing it up.
migrate_rates_db turns the single rates.db from before #37 into its index's database without re-parsing anything.
"""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from etl.run import backup_db, read_index, write_index_info

ACTIVE_FILE = "active_index"


def db_name_for(index_path: str | os.PathLike) -> str:
    """The database file name for an index file: its name with .db instead of .json."""
    return Path(index_path).stem + ".db"


class IndexStore:
    """The index databases directly in parsed_dir (*.db), and which one is active.

    fixed: a single database used exactly as given (tests, or RATES_DB): it is the only database and always
    active, and loading any index rebuilds it."""

    def __init__(self, parsed_dir: str | os.PathLike, backup_dir: str | os.PathLike,
                 fixed: str | os.PathLike | None = None):
        self.parsed_dir = Path(parsed_dir)
        self.backup_dir = Path(backup_dir)
        self.fixed = Path(fixed) if fixed is not None else None

    def dbs(self) -> list[Path]:
        """Every index database, newest name first (index file names start with their date)."""
        if self.fixed is not None:
            return [self.fixed] if self.fixed.is_file() else []
        if not self.parsed_dir.is_dir():
            return []
        return sorted((p for p in self.parsed_dir.glob("*.db") if p.is_file()), key=lambda p: p.name, reverse=True)

    def find(self, name: str) -> Path | None:
        """The database called name (a bare file name from dbs()), or None."""
        return next((p for p in self.dbs() if p.name == name), None)

    def active(self) -> Path | None:
        """The database the app shows: the saved choice if it still exists, else the newest (last modified) one;
        None when there is none. A fixed database is always active, even before it exists."""
        if self.fixed is not None:
            return self.fixed
        try:
            saved = (self.parsed_dir / ACTIVE_FILE).read_text().strip()
        except OSError:
            saved = ""
        if saved and (chosen := self.find(saved)) is not None:
            return chosen
        dbs = self.dbs()
        return max(dbs, key=lambda p: p.stat().st_mtime) if dbs else None

    def set_active(self, name: str) -> Path:
        """Make the database called name the active one (saved, so it survives a restart). ValueError if there is
        no such database."""
        path = self.find(name)
        if path is None:
            raise ValueError(f"There’s no index database called “{name}”.")
        if self.fixed is None:
            fd, tmp = tempfile.mkstemp(dir=self.parsed_dir, prefix=ACTIVE_FILE + ".")
            with os.fdopen(fd, "w") as f:
                f.write(name + "\n")
            os.chmod(tmp, 0o644)  # mkstemp makes it owner-only; it's an ordinary setting
            os.replace(tmp, self.parsed_dir / ACTIVE_FILE)
        return path

    def db_for(self, index_path: str | os.PathLike) -> Path:
        """Where loading this index builds its database."""
        return self.fixed if self.fixed is not None else self.parsed_dir / db_name_for(index_path)

    def remove(self, name: str) -> Path:
        """Back up the database called name to backup_dir, then delete it; returns the backup. If it was the saved
        active one, the setting goes too (the newest remaining database becomes active). ValueError if there is no
        such database."""
        path = self.find(name)
        if path is None:
            raise ValueError(f"There’s no index database called “{name}”.")
        backup = backup_db(path, self.backup_dir)
        path.unlink()
        if self.fixed is None:
            setting = self.parsed_dir / ACTIVE_FILE
            try:
                if setting.read_text().strip() == name:
                    setting.unlink()
            except OSError:
                pass
        return backup


def migrate_rates_db(parsed_dir: str | os.PathLike, index_path: str | os.PathLike,
                     backup_dir: str | os.PathLike, old_name: str = "rates.db") -> dict:
    """One-off (#37.5): turn parsed_dir/rates.db, built before one database per index, into its index's database.

    Backs rates.db up to backup_dir, records index_path's index in it (its loaded_at kept as when its files were
    loaded), renames it to <index stem>.db and makes that active. Nothing is re-parsed. Raises FileNotFoundError
    when there is no rates.db and FileExistsError when the index already has a database (nothing changed).
    Returns {"backup", "db"}."""
    parsed_dir, index_path = Path(parsed_dir), Path(index_path)
    old = parsed_dir / old_name
    new = parsed_dir / db_name_for(index_path)
    if not old.is_file():
        raise FileNotFoundError(f"nothing to migrate: {old} doesn’t exist")
    if new.exists():
        raise FileExistsError(f"{new} already exists; not overwriting it")
    index, _ = read_index(str(index_path), index_path.parent)
    backup = backup_db(old, Path(backup_dir))
    with closing(sqlite3.connect(old)) as conn:
        write_index_info(conn, index, str(index_path))
        (loaded_at,) = conn.execute("SELECT MAX(loaded_at) FROM source_files").fetchone()
        if loaded_at:
            with conn:
                conn.execute("UPDATE index_info SET loaded_at = ?", (loaded_at,))
    os.replace(old, new)
    IndexStore(parsed_dir, backup_dir).set_active(new.name)
    return {"backup": str(backup), "db": str(new)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="One-off: turn data/parsed/rates.db into its index's database "
                                     "(<index file stem>.db), recording the index in it, and make it active.")
    parser.add_argument("--index", required=True, type=Path, help="the index file rates.db was loaded from")
    parser.add_argument("--parsed-dir", type=Path, default=Path("data/parsed"), help="default: %(default)s")
    parser.add_argument("--backup-dir", type=Path, default=Path("data/parsed/backups"), help="default: %(default)s")
    args = parser.parse_args()
    try:
        result = migrate_rates_db(args.parsed_dir, args.index, args.backup_dir)
    except (FileNotFoundError, FileExistsError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"backed up to {result['backup']}\nnow {result['db']} (active)")


if __name__ == "__main__":
    main()
