"""Auto-Registration & Results Auto-Sync Service.
Controls OutlookRegister process, updates config, and provides live log streaming.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
REGISTER_DIR = ROOT.parent / "OutlookRegister"
RESULTS_FILE = REGISTER_DIR / "Results" / "oauth2.txt"
AUTOREG_LOG_FILE = ROOT / "logs" / "autoreg.log"


class AutoRegManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._is_running = False
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.log_lines: collections.deque[str] = collections.deque(maxlen=1000)
        self.stats = {
            "started_at": None,
            "registered_count": 0,
            "failed_count": 0,
            "last_error": None,
            "status": "idle",
        }

    def start(
        self,
        concurrent: int = 1,
        tasks: int = 5,
        email_suffix: str = "@outlook.com",
        headless: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._is_running and self._process and self._process.poll() is None:
                return {"success": False, "error": "Auto-registration worker is already running"}

            if not REGISTER_DIR.exists():
                return {"success": False, "error": f"OutlookRegister directory not found at {REGISTER_DIR}"}

            main_script = REGISTER_DIR / "main.py"
            if not main_script.exists():
                return {"success": False, "error": f"main.py not found in {REGISTER_DIR}"}

            # Update OutlookRegister/config.json with requested parameters
            config_file = REGISTER_DIR / "config.json"
            if not config_file.exists():
                example = REGISTER_DIR / "config.example.json"
                if example.exists():
                    import shutil
                    shutil.copy(example, config_file)

            try:
                cfg_data = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
                cfg_data["tasks"] = max(1, int(tasks or 5))
                cfg_data["concurrent_flows"] = max(1, int(concurrent or 1))
                cfg_data["email_suffix"] = email_suffix if email_suffix else "@outlook.com"
                cfg_data["headless"] = bool(headless)
                config_file.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [WARN] Failed to write config.json: {exc}")

            self._stop_event.clear()
            self.stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.stats["status"] = "running"
            self.stats["last_error"] = None
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [INIT] Starting auto-registration: tasks={tasks}, concurrent={concurrent}, suffix={email_suffix}, headless={headless}")

            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                self._process = subprocess.Popen(
                    [sys.executable, str(main_script)],
                    cwd=str(REGISTER_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                self._is_running = True

                # Start watcher thread for stdout & results
                self._sync_thread = threading.Thread(target=self._monitor_loop, daemon=True)
                self._sync_thread.start()

                return {
                    "success": True,
                    "pid": self._process.pid,
                    "status": "started",
                    "tasks": tasks,
                    "concurrent": concurrent,
                }
            except Exception as exc:
                self.stats["status"] = "error"
                self.stats["last_error"] = str(exc)
                self._is_running = False
                self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [ERROR] Process launch failed: {exc}")
                return {"success": False, "error": str(exc)}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running or not self._process:
                return {"success": True, "message": "Worker is not running"}

            self._stop_event.set()
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [STOP] Stopping auto-registration worker (PID {self._process.pid})...")
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            except Exception as exc:
                return {"success": False, "error": str(exc)}
            finally:
                self._is_running = False
                self.stats["status"] = "stopped"
                self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [STOP] Worker stopped.")

            return {"success": True, "message": "Worker stopped"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._process and self._process.poll() is not None:
                self._is_running = False
                if self.stats["status"] == "running":
                    self.stats["status"] = "finished"

            return {
                "running": self._is_running,
                "status": self.stats["status"],
                "started_at": self.stats["started_at"],
                "registered_count": self.stats["registered_count"],
                "last_error": self.stats["last_error"],
                "pid": self._process.pid if (self._process and self._is_running) else None,
            }

    def get_logs(self, limit: int = 150) -> list[str]:
        limit = max(1, min(limit, 500))
        lines = list(self.log_lines)
        return lines[-limit:]

    def _monitor_loop(self) -> None:
        """Read stdout in real-time, buffer lines, and sync results."""
        if not self._process or not self._process.stdout:
            return

        try:
            AUTOREG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            for line in iter(self._process.stdout.readline, ""):
                if not line:
                    break
                clean = line.strip()
                if clean:
                    entry = f"[{time.strftime('%H:%M:%S')}] {clean}"
                    self.log_lines.append(entry)
                    try:
                        with open(AUTOREG_LOG_FILE, "a", encoding="utf-8") as f:
                            f.write(entry + "\n")
                    except Exception:
                        pass
                self.sync_results()

            self._process.stdout.close()
        except Exception as exc:
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [LOG ERR] {exc}")
        finally:
            self._is_running = False
            self.stats["status"] = "finished"
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [FINISH] Auto-registration process completed.")
            self.sync_results()

    def sync_results(self) -> int:
        """Scan Results/oauth2.txt and insert any newly registered accounts into accounts.db."""
        if not RESULTS_FILE.exists():
            return 0

        from backend.db import get_conn, retry_on_locked

        imported_now = 0
        try:
            content = RESULTS_FILE.read_text(encoding="utf-8", errors="ignore")
            lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
            if not lines:
                return 0

            with get_conn() as conn:
                cursor = conn.cursor()
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")

                for line in lines:
                    parts = [p.strip() for p in line.split("----")]
                    if len(parts) < 4:
                        continue
                    email_addr, password, client_id = parts[:3]
                    refresh_token = "----".join(parts[3:]).strip()

                    cursor.execute("SELECT id FROM accounts WHERE email = ?", (email_addr,))
                    row = cursor.fetchone()
                    if row is None:
                        retry_on_locked(
                            cursor.execute,
                            """
                            INSERT INTO accounts (
                                email, password, client_id, refresh_token,
                                status, health_status, health_severity,
                                registered_at, registered_source,
                                refresh_token_updated_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'normal', 'all', 'ok', ?, 'autoreg', ?, ?, ?)
                            """,
                            (
                                email_addr,
                                password,
                                client_id,
                                refresh_token,
                                now_str,
                                now_str,
                                now_str,
                                now_str,
                            ),
                        )
                        imported_now += 1
                        self.stats["registered_count"] += 1
                        self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] [DB] Auto-imported new account: {email_addr}")

                conn.commit()
        except Exception as exc:
            self.stats["last_error"] = f"Sync error: {exc}"

        return imported_now


autoreg_mgr = AutoRegManager()
