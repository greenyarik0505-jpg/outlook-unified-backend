from __future__ import annotations

import atexit
import os
import threading
import time
import uuid
from pathlib import Path

from backend.db import get_conn


class RuntimeLease:
    def __init__(self, db_path: Path, name: str = "background-worker", ttl: float = 30.0):
        self.db_path = Path(db_path)
        self.name = name
        self.ttl = ttl
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._stop = threading.Event()

    def acquire(self) -> bool:
        now = time.time()
        with get_conn(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner,heartbeat FROM runtime_leases WHERE name=?", (self.name,)).fetchone()
            if row and row["owner"] != self.owner:
                old_pid_str = str(row["owner"]).split("-")[0]
                is_alive = False
                try:
                    old_pid = int(old_pid_str)
                    import psutil
                    is_alive = psutil.pid_exists(old_pid)
                except Exception:
                    is_alive = now - float(row["heartbeat"] or 0) < self.ttl
                if is_alive and (now - float(row["heartbeat"] or 0) < self.ttl):
                    conn.rollback()
                    return False
            conn.execute(
                "INSERT INTO runtime_leases(name,owner,heartbeat) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,heartbeat=excluded.heartbeat",
                (self.name, self.owner, now),
            )
            conn.commit()
        threading.Thread(target=self._heartbeat, daemon=True, name="runtime-lease").start()
        atexit.register(self.release)
        return True

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.ttl / 3):
            with get_conn(self.db_path) as conn:
                conn.execute(
                    "UPDATE runtime_leases SET heartbeat=? WHERE name=? AND owner=?",
                    (time.time(), self.name, self.owner),
                )
                conn.commit()

    def release(self) -> None:
        self._stop.set()
        try:
            with get_conn(self.db_path) as conn:
                conn.execute("DELETE FROM runtime_leases WHERE name=? AND owner=?", (self.name, self.owner))
                conn.commit()
        except Exception:
            pass
