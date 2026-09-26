#!/usr/bin/env python3
"""Unified Orchestrator & CLI Launcher for Outlook Automation Ecosystem.
Combines OutlookManage (API & WebUI), OutlookRegister (Autoreg),
Outlook-Oauth-GetToken, and real-time database synchronization.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANAGE_DIR = ROOT / "OutlookManage"
REGISTER_DIR = ROOT / "OutlookRegister"
GET_TOKEN_DIR = ROOT / "Outlook-Oauth-GetToken"


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def ensure_configs() -> None:
    """Ensure all configuration files exist from templates."""
    pairs = [
        (MANAGE_DIR / "config.example.json", MANAGE_DIR / "config.json"),
        (REGISTER_DIR / "config.example.json", REGISTER_DIR / "config.json"),
        (GET_TOKEN_DIR / "config.example.json", GET_TOKEN_DIR / "config.json"),
        (GET_TOKEN_DIR / "not_oauth2.example.txt", GET_TOKEN_DIR / "not_oauth2.txt"),
    ]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            import shutil
            shutil.copy(src, dst)
            print(f"[*] Initialized configuration: {dst.name}")

    (MANAGE_DIR / "data").mkdir(parents=True, exist_ok=True)
    (MANAGE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (REGISTER_DIR / "Results").mkdir(parents=True, exist_ok=True)


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the FastAPI backend server (WebUI, Mail API, OTP Reader, Account Manager)."""
    ensure_configs()
    print("\n===========================================================")
    print(f"  [+] Outlook Unified API & Webmail Server: http://{host}:{port}")
    print(f"  [+] API Docs (Swagger): http://{host}:{port}/docs")
    print(f"  [+] Mail API:           http://{host}:{port}/api/mail/inbox")
    print(f"  [+] OTP API:            http://{host}:{port}/api/mail/otp")
    print("===========================================================\n")
    
    # Run uvicorn
    import uvicorn
    sys.path.insert(0, str(MANAGE_DIR))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False, app_dir=str(MANAGE_DIR))


def run_autoreg() -> None:
    """Run Outlook auto-registration worker."""
    ensure_configs()
    main_script = REGISTER_DIR / "main.py"
    if not main_script.exists():
        print(f"[!] OutlookRegister not found at {main_script}")
        return
    print("[*] Starting OutlookRegister worker...")
    subprocess.run([sys.executable, str(main_script)], cwd=str(REGISTER_DIR))


def run_get_token() -> None:
    """Run standalone OAuth2 token retrieval for existing accounts."""
    ensure_configs()
    script = GET_TOKEN_DIR / "get_refresh_token.py"
    if not script.exists():
        print(f"[!] Outlook-Oauth-GetToken not found at {script}")
        return
    print("[*] Starting Outlook-Oauth-GetToken worker...")
    subprocess.run([sys.executable, str(script)], cwd=str(GET_TOKEN_DIR))


def start_results_watcher() -> None:
    """Continuously monitor Results/oauth2.txt and auto-import accounts into SQLite."""
    results_file = REGISTER_DIR / "Results" / "oauth2.txt"
    sys.path.insert(0, str(MANAGE_DIR))
    from backend.db import get_conn, retry_on_locked

    print("[*] Started background Results/oauth2.txt auto-sync watcher.")
    while True:
        try:
            if results_file.exists():
                text = results_file.read_text(encoding="utf-8", errors="ignore")
                lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
                if lines:
                    with get_conn() as conn:
                        cursor = conn.cursor()
                        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                        imported = 0
                        for line in lines:
                            parts = [p.strip() for p in line.split("----")]
                            if len(parts) < 4:
                                continue
                            email, pwd, cid = parts[:3]
                            rf_token = "----".join(parts[3:]).strip()
                            cursor.execute("SELECT id FROM accounts WHERE email = ?", (email,))
                            if cursor.fetchone() is None:
                                retry_on_locked(
                                    cursor.execute,
                                    """
                                    INSERT INTO accounts (
                                        email, password, client_id, refresh_token,
                                        status, health_status, health_severity,
                                        registered_at, registered_source,
                                        refresh_token_updated_at, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, 'normal', 'all', 'ok', ?, 'watcher', ?, ?, ?)
                                    """,
                                    (email, pwd, cid, rf_token, now_str, now_str, now_str, now_str),
                                )
                                imported += 1
                        if imported > 0:
                            conn.commit()
                            print(f"[+] Watcher auto-imported {imported} newly registered account(s) into database!")
        except Exception as exc:
            pass
        time.sleep(5)


def run_all(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run Web Server and auto-sync watcher in one unified daemon."""
    ensure_configs()
    watcher_thread = threading.Thread(target=start_results_watcher, daemon=True)
    watcher_thread.start()
    run_server(host=host, port=port)


def main():
    parser = argparse.ArgumentParser(description="Unified Outlook Automation & Mail Server")
    parser.add_argument("--serve", action="store_true", help="Start the Web API & Management server")
    parser.add_argument("--autoreg", action="store_true", help="Run Outlook auto-registration worker")
    parser.add_argument("--get-token", action="store_true", help="Run standalone OAuth2 token retrieval")
    parser.add_argument("--all", action="store_true", help="Start Web API server and background auto-sync watcher")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")

    args = parser.parse_args()

    if args.autoreg:
        run_autoreg()
    elif args.get_token:
        run_get_token()
    elif args.all or args.serve or len(sys.argv) == 1:
        run_all(host=args.host, port=args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
