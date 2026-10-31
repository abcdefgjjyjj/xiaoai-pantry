"""Create recoverable SQLite snapshots on the second NAS volume."""

import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


def backup_database(db_path, backup_dir):
    db_path = Path(db_path).resolve(strict=True)
    backup_dir = Path(backup_dir)
    today = date.today()
    target = backup_dir / f"pantry-{today.isoformat()}.sqlite3"
    fd, name = tempfile.mkstemp(prefix=".pantry-", suffix=".tmp", dir=backup_dir)
    os.close(fd)
    staged = Path(name)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(staged)) as destination:
                source.backup(destination, pages=64, sleep=0.1)
                if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("SQLite backup integrity check failed")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)

    for old in backup_dir.glob("pantry-????-??-??.sqlite3"):
        try:
            snapshot_day = date.fromisoformat(old.stem[len("pantry-"):])
        except ValueError:
            continue
        if snapshot_day <= today - timedelta(days=14):
            old.unlink()
    return target


def start_backup_worker(db_path):
    backup_dir = os.environ.get("PANTRY_BACKUP_DIR")
    if not backup_dir:
        return

    def run():
        while True:
            try:
                backup_database(db_path, backup_dir)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                logging.getLogger(__name__).error("Pantry database backup failed: %s", exc)
            time.sleep(15 * 60)

    threading.Thread(target=run, name="pantry-backup", daemon=True).start()
