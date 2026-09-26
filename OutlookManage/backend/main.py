import json
import hashlib
import os
import queue
import sys
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.db import add_history, get_conn, init_db, retry_on_locked
from backend.services import db_writer, diagnostics, export_store, jobs, locks
from backend.services.runtime_lease import RuntimeLease
from backend.services.abuse_recovery import recover_abuse_account
from backend.services.protocols import run_protocol_test
from backend.services.remote_pool import (
    AuthenticatedSessionPool,
    IMPORT_BATCH_SIZE,
    REMOTE_MAX_CONCURRENCY,
    build_session,
    chunked,
    delete_account_remote,
    find_account,
    get_account_detail,
    get_csrf,
    import_accounts,
    index_accounts_by_email,
    list_accounts as list_remote_accounts,
    login,
    update_remote_token,
)

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
LOG_PATH = ROOT / "logs" / "app.log"
CONFIG_PATH = ROOT / "config.json"
TEST_SCRIPT = ROOT / "test_protocols.py"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
HTTP_TIMEOUT = 30
SUSPECTED_RESTRICTED_REASON = "可取 token 但 Graph/IMAP/POP 均不可用（疑似受限）"
ABUSE_HINTS = (
    "service abuse",
    "abuse mode",
    "[abuse]",
    "账号被微软风控判定为滥用并封禁",
    "违反 microsoft 服务协议",
    "锁定了你的帐户",
)

PYTHON_EXE = sys.executable


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time_value(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.timestamp()
    except ValueError:
        return None


def token_time(row: dict[str, Any] | Any) -> str:
    if isinstance(row, dict):
        return str(row.get("refresh_token_updated_at") or row.get("last_refresh_at") or "").strip()
    return str(row["refresh_token_updated_at"] or row["last_refresh_at"] or "").strip()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        example_path = CONFIG_PATH.parent / "config.example.json"
        if example_path.exists():
            import shutil
            shutil.copy(example_path, CONFIG_PATH)
        else:
            return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()
_CONFIG_LOCK = threading.RLock()
_LOG_LOCK = threading.Lock()
_LOG_QUEUE: queue.Queue[tuple[Path, str]] = queue.Queue()
DB_PATH = ROOT / CONFIG["database"]["path"]
init_db(DB_PATH)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_config() -> dict[str, Any]:
    with _CONFIG_LOCK:
        return CONFIG


def proxy_url() -> str:
    cfg = get_config()
    return ((cfg.get("proxy") or {}).get("url") or "").strip()

def group_for_email(email: str):
    """按邮箱后缀返回目标远程分组 id；未映射且 skip_unmapped 时返回 None（跳过）。"""
    remote = get_config()["remote"]
    gmap = {str(k).lower(): int(v) for k, v in (remote.get("group_map") or {}).items()}
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain in gmap:
        return gmap[domain]
    if remote.get("skip_unmapped", True):
        return None
    return remote.get("default_group_id") or remote.get("group_id") or 1


def resync_target_group(email: str, current_gid):
    """resync 时的分组纠正目标：仅当账号当前位于批量分组(如 3/8)且与域名不符时返回正确分组；
    个人(1)/临时(2)/默认(9)等非批量分组一律返回 None=保留，绝不挪动用户手动归类的账号。"""
    remote = get_config()["remote"]
    gmap = {str(k).lower(): int(v) for k, v in (remote.get("group_map") or {}).items()}
    bulk = set(gmap.values())
    try:
        current_gid = int(current_gid)
    except (TypeError, ValueError):
        current_gid = None
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    domain_gid = gmap.get(domain)
    if domain_gid is not None and current_gid in bulk and current_gid != domain_gid:
        return domain_gid
    return None


def _log_writer() -> None:
    while True:
        first = _LOG_QUEUE.get()
        batch = [first]
        deadline = time.monotonic() + 0.01
        while len(batch) < 200 and time.monotonic() < deadline:
            try:
                batch.append(_LOG_QUEUE.get_nowait())
            except queue.Empty:
                time.sleep(0.001)
        grouped: dict[Path, list[str]] = {}
        for path, line in batch:
            grouped.setdefault(path, []).append(line)
        try:
            with _LOG_LOCK:
                for path, lines in grouped.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write("\n".join(lines) + "\n")
        finally:
            for _ in batch:
                _LOG_QUEUE.task_done()


threading.Thread(target=_log_writer, daemon=True, name="log-writer").start()


def flush_logs() -> None:
    _LOG_QUEUE.join()


def log_event(stage: str, message: str, level: str = "INFO") -> None:
    line = f"[{stage}][{level}] {datetime.now().strftime('%H:%M:%S')} | {message}"
    _LOG_QUEUE.put((Path(LOG_PATH), line))
    print(line)


def is_banned_row(row: dict[str, Any] | Any) -> bool:
    """统一封禁判定：health_status 或 health_severity 任一为 banned。"""
    if isinstance(row, dict):
        hs = str(row.get("health_status") or "").strip()
        sev = str(row.get("health_severity") or "").strip()
    else:
        hs = str(row["health_status"] or "").strip()
        sev = str(row["health_severity"] or "").strip()
    return hs == "banned" or sev == "banned"


SQL_NOT_BANNED = (
    "(COALESCE(health_status, '') != 'banned' AND COALESCE(health_severity, '') != 'banned')"
)


def account_line(row) -> str:
    return f"{row['email']}----{row['password']}----{row['client_id']}----{row['refresh_token']}"


def row_text(row: dict[str, Any] | Any, *keys: str) -> str:
    if isinstance(row, dict):
        return " ".join(str(row.get(key, "") or "") for key in keys).lower()
    return " ".join(str(row[key] or "") for key in keys).lower()


def is_abuse_candidate(row: dict[str, Any] | Any) -> bool:
    # 滥用封禁 = ABUSE 候选（与 is_banned_row 一致）
    return is_banned_row(row)


def is_normal_account(row: dict[str, Any] | Any) -> bool:
    getter = row.get if isinstance(row, dict) else row.__getitem__
    return any((getter(key) or "") == "ok" for key in ("graph_status", "imap_status", "pop_status"))


def is_untested_account(row: dict[str, Any] | Any) -> bool:
    getter = row.get if isinstance(row, dict) else row.__getitem__
    return not (getter("health_status") or "") and not (getter("last_protocol_test_at") or "") and not (getter("error_detail") or "")


def is_other_error_account(row: dict[str, Any] | Any) -> bool:
    getter = row.get if isinstance(row, dict) else row.__getitem__
    if is_normal_account(row) or is_banned_row(row) or is_untested_account(row):
        return False
    return (getter("health_status") or "") in ("other_error", "token_invalid") or (getter("status") or "") == "proto_error" or bool(getter("error_detail") or "")


def summarize_protocol_error(outcome: dict[str, Any]) -> str:
    parts = []
    for key in ("error", "stderr", "stdout"):
        value = str(outcome.get(key) or "").strip()
        if value:
            parts.append(value.replace("\n", " | "))
    text = " | ".join(parts)
    return text[:1000] if text else "协议测试失败"


def suspected_restricted(health: dict[str, Any]) -> bool:
    return (health.get("ban_reason") or "") == SUSPECTED_RESTRICTED_REASON


def to_other_error(health: dict[str, Any], reason: str) -> dict[str, Any]:
    patched = dict(health)
    patched["health_status"] = "other_error"
    patched["health_severity"] = "fail"
    patched["ban_reason"] = ""
    patched["error_detail"] = reason
    return patched


def is_alive_health(health: dict[str, Any]) -> bool:
    return (health.get("health_severity") or "") in ("ok", "warn")


def run_cancellable(items, workers: int, worker, is_cancelled) -> None:
    item_iter = iter(items)
    pending = set()

    def submit_next(pool: ThreadPoolExecutor) -> bool:
        if is_cancelled():
            return False
        try:
            item = next(item_iter)
        except StopIteration:
            return False
        def limited_worker():
            try:
                with jobs.worker_slot(is_cancelled):
                    if not is_cancelled():
                        worker(item)
            except jobs.TaskCancelled:
                return

        pending.add(pool.submit(limited_worker))
        return True

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        for _ in range(workers):
            submit_next(pool)
        while pending:
            if is_cancelled():
                for future in pending:
                    future.cancel()
                break
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                if future.cancelled():
                    continue
                future.result()
            if is_cancelled():
                for future in pending:
                    future.cancel()
                break
            for _ in range(len(done)):
                submit_next(pool)
    finally:
        try:
            pool.shutdown(wait=not is_cancelled(), cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=not is_cancelled())


# ---------------- Pydantic ----------------
class ImportPayload(BaseModel):
    text: str


class BatchPayload(BaseModel):
    ids: list[int] | None = None
    concurrency: int | None = None


MAX_CONCURRENCY = 100


def clamp_concurrency(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_CONCURRENCY, n))


def clamp_remote_concurrency(value, default: int = 8) -> int:
    return min(REMOTE_MAX_CONCURRENCY, clamp_concurrency(value, default))


class ConfigPayload(BaseModel):
    proxy_url: str | None = None
    remote_base_url: str | None = None
    remote_password: str | None = None
    group_map: dict[str, int] | None = None
    skip_unmapped: bool | None = None
    external_recipient: str | None = None
    default_concurrency: int | None = None
    export_concurrency: int | None = None


class EditPayload(BaseModel):
    password: str | None = None
    client_id: str | None = None
    refresh_token: str | None = None
    remark: str | None = None


class RecoveryBatchPayload(BaseModel):
    ids: list[int] | None = None
    concurrency: int | None = None
    include_over_limit: bool = False


_runtime_lease: RuntimeLease | None = None
_export_cleanup_stop = threading.Event()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    global _runtime_lease
    lease = RuntimeLease(DB_PATH)
    if not lease.acquire():
        raise RuntimeError("已有 Outlook Manage 后台实例正在运行")
    _runtime_lease = lease
    jobs.configure(DB_PATH)
    recover_interrupted_exports()
    _bootstrap_stats_cache()
    cleanup_expired_exports()
    _export_cleanup_stop.clear()
    threading.Thread(target=_export_cleanup_worker, daemon=True, name="export-cleanup").start()
    try:
        yield
    finally:
        invalidate_remote_cache()
        flush_logs()
        db_writer.flush()
        _export_cleanup_stop.set()
        lease.release()
        _runtime_lease = None


app = FastAPI(title="Outlook Manage WebUI", lifespan=app_lifespan)

from fastapi.middleware.cors import CORSMiddleware
from backend.routers.mail_router import router as mail_router, autoreg_router

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mail_router)
app.include_router(autoreg_router)

app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/scope.md")
def scope_doc():
    return FileResponse(ROOT / "scope.md")


# ---------------- 状态统计（DB 缓存，启动秒开） ----------------
_stats_lock = threading.Lock()
_stats_refresh_event = threading.Event()
_stats_thread_started = False
_stats_requested_delay = 0.0
_STATS_DEBOUNCE_SEC = 1.2


def _compute_stats_summary(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN graph_status='ok' OR imap_status='ok' OR pop_status='ok' THEN 1 ELSE 0 END) AS normal,
          SUM(CASE WHEN health_status='banned' OR health_severity='banned' THEN 1 ELSE 0 END) AS banned,
          SUM(CASE WHEN COALESCE(health_status,'')='' AND COALESCE(last_protocol_test_at,'')=''
                   AND COALESCE(error_detail,'')='' THEN 1 ELSE 0 END) AS untested,
          SUM(CASE WHEN (health_status IN ('token_invalid','other_error') OR status='proto_error')
                   AND COALESCE(health_status,'')!='banned' AND COALESCE(health_severity,'')!='banned'
                   THEN 1 ELSE 0 END) AS other_error,
          SUM(CASE WHEN health_status='graph_only' THEN 1 ELSE 0 END) AS graph_only,
          SUM(CASE WHEN graph_status='ok' THEN 1 ELSE 0 END) AS graph,
          SUM(CASE WHEN imap_status='ok' OR pop_status='ok' THEN 1 ELSE 0 END) AS imap_pop,
          SUM(CASE WHEN COALESCE(last_refresh_at,'')!='' THEN 1 ELSE 0 END) AS refreshed,
          SUM(CASE WHEN remote_sync_status IN ('imported','exists','synced') THEN 1 ELSE 0 END) AS remote_ready,
          SUM(CASE WHEN (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                   AND remote_sync_status IN ('imported','exists','synced') THEN 1 ELSE 0 END) AS synced,
          SUM(CASE WHEN remote_sync_status='dirty' THEN 1 ELSE 0 END) AS remote_dirty,
          SUM(CASE WHEN (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                   AND COALESCE(remote_sync_status,'') NOT IN ('imported','exists','synced') THEN 1 ELSE 0 END) AS never_synced,
          SUM(CASE WHEN pop_status='disabled' THEN 1 ELSE 0 END) AS pop_disabled,
          SUM(CASE WHEN smtp_status='disabled' THEN 1 ELSE 0 END) AS smtp_disabled,
          SUM(CASE WHEN imap_status='disabled' THEN 1 ELSE 0 END) AS imap_disabled
        FROM accounts
        """
    ).fetchone()
    values = {key: int(row[key] or 0) for key in row.keys()}
    return {
        "total_accounts": values["total"],
        "healthy": values["normal"],
        **values,
    }


def rebuild_stats_cache() -> dict[str, Any]:
    """全表聚合一次并写入 stats_cache，供 /api/status 快速读取。"""
    ts = now_local()
    with get_conn(DB_PATH) as conn:
        summary = _compute_stats_summary(conn)
        summary["cached_at"] = ts
        conn.execute(
            """
            INSERT INTO stats_cache (id, payload, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (json.dumps(summary, ensure_ascii=False), ts),
        )
        conn.commit()
    return summary


def read_stats_cache() -> dict[str, Any] | None:
    try:
        with get_conn(DB_PATH) as conn:
            row = conn.execute("SELECT payload, updated_at FROM stats_cache WHERE id=1").fetchone()
        if not row:
            return None
        data = json.loads(row["payload"] or "{}")
        if not isinstance(data, dict) or not data:
            return None
        data.setdefault("cached_at", row["updated_at"] or "")
        return data
    except Exception:
        return None


def schedule_stats_refresh(delay: float | None = None) -> None:
    global _stats_thread_started, _stats_requested_delay
    wait_seconds = _STATS_DEBOUNCE_SEC if delay is None else max(0.0, float(delay))
    with _stats_lock:
        _stats_requested_delay = wait_seconds
        if not _stats_thread_started:
            threading.Thread(target=_stats_refresh_worker, daemon=True, name="stats-refresh").start()
            _stats_thread_started = True
    _stats_refresh_event.set()


def _stats_refresh_worker() -> None:
    while True:
        _stats_refresh_event.wait()
        _stats_refresh_event.clear()
        with _stats_lock:
            delay = _stats_requested_delay
        if delay:
            time.sleep(delay)
        if _stats_refresh_event.is_set():
            continue
        try:
            rebuild_stats_cache()
        except Exception as exc:  # noqa: BLE001
            try:
                log_event("STATS", f"统计缓存刷新失败：{exc}", "WARN")
            except Exception:
                pass


# 启动时预热统计缓存（空则同步算一次；已有则后台刷新）
def _bootstrap_stats_cache() -> None:
    cached = read_stats_cache()
    if cached is None:
        try:
            rebuild_stats_cache()
        except Exception:
            pass
    else:
        schedule_stats_refresh(0.05)


@app.get("/api/status")
def status(refresh: bool = False):
    """优先读 DB 缓存；无缓存或 refresh=1 时现算并回写。"""
    summary = None if refresh else read_stats_cache()
    from_cache = summary is not None
    if summary is None:
        summary = rebuild_stats_cache()
        from_cache = False
    return {
        "success": True,
        "summary": summary,
        "from_cache": from_cache,
        "server_time": now_local(),
    }


_LIST_PUBLIC_COLS = (
    "id,email,client_id,status,health_status,health_severity,ban_reason,error_detail,"
    "graph_status,imap_status,pop_status,smtp_status,registered_at,registered_source,"
    "last_alive_at,last_refresh_at,last_refresh_status,last_refresh_error,"
    "refresh_token_updated_at,last_protocol_test_at,remote_sync_status,remote_sync_at,"
    "remote_sync_error,remote_last_refresh_at,remote_last_refresh_status,token_sync_status,"
    "remote_id,remark,created_at,updated_at,recovery_status,recovery_attempts,"
    "recovery_last_at,recovery_last_reason,oauth_reauth_at,oauth_reauth_status"
)


_ACCOUNT_SORTS = {
    "id": "id",
    "email": "email COLLATE NOCASE",
    "health_status": "health_status COLLATE NOCASE",
    "registered_at": "registered_at",
    "last_protocol_test_at": "last_protocol_test_at",
    "remote_sync_status": "remote_sync_status COLLATE NOCASE",
}


def _account_filters(
    search: str = "", filter: str = "all", domain: str = "", alive_days: int = 7
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    search = str(search or "").strip().lower()
    if search:
        clauses.append("LOWER(email) LIKE ?")
        params.append(f"%{search}%")
    domain = str(domain or "").strip().lower()
    if domain == "__other__":
        clauses.append("LOWER(email) NOT LIKE '%@outlook.com' AND LOWER(email) NOT LIKE '%@hotmail.com'")
    elif domain:
        clauses.append("LOWER(email) LIKE ?")
        params.append(f"%@{domain}")
    filters = {
        "normal": "(graph_status='ok' OR imap_status='ok' OR pop_status='ok')",
        "synced": "(graph_status='ok' OR imap_status='ok' OR pop_status='ok') AND remote_sync_status IN ('imported','exists','synced')",
        "graph": "graph_status='ok'",
        "imap_pop": "(imap_status='ok' OR pop_status='ok')",
        "banned": "(health_status='banned' OR health_severity='banned')",
        "other_error": "((health_status IN ('token_invalid','other_error') OR status='proto_error' OR COALESCE(error_detail,'')!='') AND NOT (graph_status='ok' OR imap_status='ok' OR pop_status='ok') AND COALESCE(health_status,'')!='banned' AND COALESCE(health_severity,'')!='banned')",
        "untested": "(COALESCE(health_status,'')='' AND COALESCE(last_protocol_test_at,'')='' AND COALESCE(error_detail,'')='')",
        "never_synced": "((graph_status='ok' OR imap_status='ok' OR pop_status='ok') AND COALESCE(remote_sync_status,'') NOT IN ('imported','exists','synced'))",
    }
    if filter in filters:
        clauses.append(filters[filter])
    elif filter == "alive_over_threshold":
        clauses.append(
            "COALESCE(registered_at,'')!='' AND "
            "(julianday(CASE WHEN graph_status='ok' OR imap_status='ok' OR pop_status='ok' "
            "THEN 'now' ELSE NULLIF(last_alive_at,'') END) - julianday(registered_at)) > ?"
        )
        params.append(max(0, int(alive_days or 0)))
    return (" AND ".join(f"({clause})" for clause in clauses) or "1=1"), params


@app.get("/api/accounts")
def list_accounts(
    include_secrets: bool = False,
    page: int = 1,
    page_size: int = 50,
    search: str = "",
    filter: str = "all",
    domain: str = "",
    alive_days: int = 7,
    sort: str = "id",
    dir: str = "desc",
):
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 500))
    where, params = _account_filters(search, filter, domain, alive_days)
    sort_sql = _ACCOUNT_SORTS.get(sort, _ACCOUNT_SORTS["id"])
    direction = "ASC" if str(dir).lower() == "asc" else "DESC"
    with get_conn(DB_PATH) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM accounts WHERE {where}", params).fetchone()[0])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        columns = "*" if include_secrets else _LIST_PUBLIC_COLS
        rows = conn.execute(
            f"SELECT {columns} FROM accounts WHERE {where} ORDER BY {sort_sql} {direction}, id {direction} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "success": True,
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "secrets": bool(include_secrets),
    }


@app.get("/api/accounts/ids")
def list_account_ids(search: str = "", filter: str = "all", domain: str = "", alive_days: int = 7):
    where, params = _account_filters(search, filter, domain, alive_days)
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(f"SELECT id FROM accounts WHERE {where} ORDER BY id", params).fetchall()
    return {"success": True, "ids": [int(row[0]) for row in rows], "total": len(rows)}


@app.get("/api/logs")
def list_logs(limit: int = 300):
    flush_logs()
    if not LOG_PATH.exists():
        return {"success": True, "lines": []}
    limit = max(1, min(int(limit or 300), 2000))
    # 大日志避免整文件读入：倒序读尾部
    try:
        with _LOG_LOCK:
            with LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                block = 8192
                data = b""
                while size > 0 and data.count(b"\n") <= limit:
                    step = min(block, size)
                    size -= step
                    f.seek(size)
                    data = f.read(step) + data
                text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if lines and not text.endswith("\n") and size > 0:
            # 首行可能被截断
            lines = lines[1:]
        return {"success": True, "lines": lines[-limit:]}
    except Exception:
        with _LOG_LOCK:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        return {"success": True, "lines": lines[-limit:]}


@app.post("/api/logs/clear")
def clear_logs():
    flush_logs()
    if LOG_PATH.exists():
        with _LOG_LOCK:
            LOG_PATH.write_text("", encoding="utf-8")
    log_event("LOG", "日志已清空")
    return {"success": True}


# ---------------- 导出（持久化复测 → 文件落盘 → 删除已导出） ----------------
EXPORT_DIR = ROOT / "data" / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def _parse_account_dt(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _account_registered_dt(row: dict[str, Any] | Any) -> datetime | None:
    getter = row.get if isinstance(row, dict) else row.__getitem__
    return _parse_account_dt(getter("registered_at")) or _parse_account_dt(getter("created_at"))


def _email_domain(email: str) -> str:
    parts = str(email or "").strip().lower().rsplit("@", 1)
    return parts[1] if len(parts) == 2 else ""


def _domain_bucket(email: str) -> str:
    domain = _email_domain(email)
    return domain if domain in ("outlook.com", "hotmail.com") else ""


def split_export_quota(count: int, domain: str) -> dict[str, int]:
    n = max(0, int(count))
    domain = (domain or "all").strip().lower()
    if domain in ("outlook.com", "outlook"):
        return {"outlook.com": n, "hotmail.com": 0}
    if domain in ("hotmail.com", "hotmail"):
        return {"outlook.com": 0, "hotmail.com": n}
    return {"outlook.com": (n + 1) // 2, "hotmail.com": n // 2}


def is_export_alive(row: dict[str, Any] | Any) -> bool:
    return not is_banned_row(row) and is_normal_account(row)


def is_min_registered_days(row: dict[str, Any] | Any, min_days: int, now: datetime | None = None) -> bool:
    days = max(0, int(min_days))
    if days <= 0:
        return True
    registered = _account_registered_dt(row)
    if not registered:
        return False
    return ((now or datetime.now()) - registered).total_seconds() >= days * 86400


def list_export_eligible(
    domain: str,
    min_registered_days: int,
    *,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    domain = (domain or "all").strip().lower()
    with get_conn(DB_PATH) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM accounts").fetchall()]
    now = now or datetime.now()
    buckets: dict[str, list[dict[str, Any]]] = {"outlook.com": [], "hotmail.com": []}
    for row in rows:
        if not is_export_alive(row) or not is_min_registered_days(row, min_registered_days, now=now):
            continue
        bucket = _domain_bucket(row.get("email") or "")
        if domain in ("outlook.com", "outlook") and bucket != "outlook.com":
            continue
        if domain in ("hotmail.com", "hotmail") and bucket != "hotmail.com":
            continue
        if bucket in buckets:
            buckets[bucket].append(row)
    for key in buckets:
        buckets[key].sort(
            key=lambda row: (_account_registered_dt(row) or datetime.min, int(row.get("id") or 0)),
            reverse=True,
        )
    return buckets


def plan_export_selection(count: int, domain: str, min_registered_days: int) -> dict[str, Any]:
    quota = split_export_quota(count, domain)
    buckets = list_export_eligible(domain, min_registered_days)
    selected_outlook = buckets["outlook.com"][: quota["outlook.com"]]
    selected_hotmail = buckets["hotmail.com"][: quota["hotmail.com"]]
    selected = selected_outlook + selected_hotmail
    selected.sort(
        key=lambda row: (_account_registered_dt(row) or datetime.min, int(row.get("id") or 0)),
        reverse=True,
    )
    plan_total = quota["outlook.com"] + quota["hotmail.com"]
    exported = len(selected)
    candidates = buckets["outlook.com"] + buckets["hotmail.com"]
    candidates.sort(
        key=lambda row: (_account_registered_dt(row) or datetime.min, int(row.get("id") or 0)),
        reverse=True,
    )
    return {
        "quota": quota,
        "eligible_outlook": len(buckets["outlook.com"]),
        "eligible_hotmail": len(buckets["hotmail.com"]),
        "eligible_total": len(buckets["outlook.com"]) + len(buckets["hotmail.com"]),
        "plan_outlook": quota["outlook.com"],
        "plan_hotmail": quota["hotmail.com"],
        "plan_total": plan_total,
        "selected": selected,
        "candidates": candidates,
        "selected_outlook": len(selected_outlook),
        "selected_hotmail": len(selected_hotmail),
        "export_count": exported,
        "shortfall": max(0, plan_total - exported),
    }


class ExportPayload(BaseModel):
    count: int = 10
    file_parts: int = 1
    domain: str = "all"
    min_registered_days: int = 7
    retest: bool = True
    concurrency: int | None = None


def _export_id() -> str:
    return f"export-{time.time_ns()}"


def _export_update(job_id: str, **fields: Any) -> None:
    export_store.update_job(DB_PATH, job_id, updated_at=now_local(), **fields)


def _export_view(job_id: str) -> dict[str, Any] | None:
    job = export_store.get_job(DB_PATH, job_id)
    return export_store.job_view(DB_PATH, job) if job else None


def _export_set_cancelled(job_id: str, phase: str | None = None) -> None:
    summary = export_store.counts(DB_PATH, job_id)
    target_phase = phase or ("partial_ready" if summary["tested_ok"] else "cancelled")
    _export_update(
        job_id,
        state="cancelled",
        phase=target_phase,
        cancel_requested=1,
        finished_at=now_local(),
    )


def _sticky_cancel_check(is_cancelled):
    cancelled = threading.Event()

    def check() -> bool:
        if is_cancelled():
            cancelled.set()
        return cancelled.is_set()

    return check


def _export_test_item(job_id: str, item: dict[str, Any], is_cancelled) -> None:
    account_id = int(item["account_id"])
    if is_cancelled():
        return
    try:
        result = protocol_one(account_id, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        result = {"status": "fail", "reason": str(exc)}
    if result.get("status") == "skip" and is_cancelled():
        return

    try:
        fresh = dict(fetch_account(account_id))
    except Exception:
        fresh = None

    if fresh and is_banned_row(fresh):
        state = "abused"
        reason = fresh.get("ban_reason") or result.get("reason") or "ABUSE"
    elif fresh and is_export_alive(fresh):
        state = "tested_ok"
        reason = "复测存活"
    elif result.get("status") == "fail":
        state = "test_failed"
        reason = result.get("reason") or (fresh or {}).get("error_detail") or "协议测试异常"
    else:
        state = "not_alive"
        reason = result.get("reason") or "复测后不满足导出条件"

    export_store.update_item(
        DB_PATH,
        job_id,
        account_id,
        test_state=state,
        reason=str(reason)[:1000],
        updated_at=now_local(),
    )


def _run_export_tests(job_id: str, is_cancelled) -> bool:
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        return False
    if is_cancelled():
        _export_set_cancelled(job_id)
        return False
    _export_update(job_id, state="running", phase="retesting", cancel_requested=0, finished_at="")
    workers = clamp_concurrency(job.get("concurrency"), 8)
    quota = split_export_quota(int(job.get("requested_count") or 0), str(job.get("domain") or "all"))
    for domain, target in quota.items():
        while target > 0 and not is_cancelled():
            completed = [
                item for item in export_store.get_items(DB_PATH, job_id, test_state="tested_ok")
                if item.get("domain") == domain
            ]
            deficit = target - len(completed)
            if deficit <= 0:
                break
            pending = [
                item for item in export_store.get_items(DB_PATH, job_id, test_state="pending")
                if item.get("domain") == domain
            ]
            if not pending:
                break
            batch = pending[:deficit]
            run_cancellable(
                batch,
                min(workers, len(batch)),
                lambda item: _export_test_item(job_id, item, is_cancelled),
                is_cancelled,
            )
    if is_cancelled():
        _export_set_cancelled(job_id)
        return False
    export_store.mark_pending_unused(DB_PATH, job_id, now_local())
    return True


def _atomic_export_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def split_export_rows(rows: list[dict[str, Any]], file_parts: int) -> list[list[dict[str, Any]]]:
    if not rows:
        return []
    parts = min(max(1, int(file_parts or 1)), len(rows))
    size, extra = divmod(len(rows), parts)
    chunks: list[list[dict[str, Any]]] = []
    offset = 0
    for index in range(parts):
        end = offset + size + (1 if index < extra else 0)
        chunks.append(rows[offset:end])
        offset = end
    return chunks


def _atomic_export_zip(path: Path, entries: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w+b") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, content in entries:
                    archive.writestr(name, content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.is_file():
            tmp.unlink()


def _export_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_export_artifact(path: Path, expected_rows: int, expected_parts: int) -> None:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) != expected_parts or len({entry.filename for entry in files}) != expected_parts:
                raise RuntimeError("导出 ZIP 分片数量校验失败")
            actual_rows = sum(len(archive.read(entry).decode("utf-8").splitlines()) for entry in files)
    else:
        actual_rows = len(path.read_text(encoding="utf-8").splitlines())
    if actual_rows != expected_rows:
        raise RuntimeError(f"导出文件行数校验失败：预期 {expected_rows}，实际 {actual_rows}")


def _build_export_file(job_id: str, is_cancelled) -> list[dict[str, Any]] | None:
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        return None
    if is_cancelled():
        _export_set_cancelled(job_id)
        return None
    _export_update(job_id, state="running", phase="building", cancel_requested=0, finished_at="")
    final_rows: list[dict[str, Any]] = []
    final_items: list[dict[str, Any]] = []

    quota = split_export_quota(int(job.get("requested_count") or 0), str(job.get("domain") or "all"))
    chosen: list[dict[str, Any]] = []
    tested = export_store.get_items(DB_PATH, job_id, test_state="tested_ok")
    for domain, count in quota.items():
        chosen.extend([item for item in tested if item.get("domain") == domain][:count])
    chosen.sort(key=lambda item: int(item.get("ordinal") or 0))
    for item in chosen:
        if is_cancelled():
            _export_set_cancelled(job_id)
            return None
        account_id = int(item["account_id"])
        try:
            row = dict(fetch_account(account_id))
        except Exception:
            export_store.update_item(
                DB_PATH,
                job_id,
                account_id,
                test_state="not_alive",
                reason="账号记录已不存在",
                updated_at=now_local(),
            )
            continue
        if is_banned_row(row):
            export_store.update_item(
                DB_PATH,
                job_id,
                account_id,
                test_state="abused",
                reason=row.get("ban_reason") or "ABUSE",
                updated_at=now_local(),
            )
            continue
        if not is_export_alive(row):
            export_store.update_item(
                DB_PATH,
                job_id,
                account_id,
                test_state="not_alive",
                reason="生成文件前已不满足导出条件",
                updated_at=now_local(),
            )
            continue
        final_rows.append(row)
        final_items.append(item)

    if not final_rows:
        _export_update(
            job_id,
            state="failed",
            phase="failed",
            error="没有可导出的存活账号",
            finished_at=now_local(),
        )
        return None

    chunks = split_export_rows(final_rows, int(job.get("file_parts") or 1))
    file_parts = len(chunks)
    stem = Path(str(job.get("filename") or f"{job_id}.txt")).stem
    if file_parts == 1:
        filename = f"{stem}.txt"
        path = EXPORT_DIR / f"{job_id}.txt"
        content = "\n".join(account_line(row) for row in chunks[0]) + "\n"
        _atomic_export_write(path, content)
    else:
        filename = f"{stem}.zip"
        path = EXPORT_DIR / f"{job_id}.zip"
        width = max(3, len(str(file_parts)))
        entries = [
            (
                f"{stem}-{index:0{width}d}-of-{file_parts:0{width}d}.txt",
                "\n".join(account_line(row) for row in chunk) + "\n",
            )
            for index, chunk in enumerate(chunks, 1)
        ]
        _atomic_export_zip(path, entries)
    _validate_export_artifact(path, len(final_rows), file_parts)
    checksum = _export_checksum(path)
    other_path = EXPORT_DIR / f"{job_id}{'.zip' if path.suffix == '.txt' else '.txt'}"
    if other_path.is_file():
        other_path.unlink()
    expires_at = (datetime.now().astimezone() + timedelta(hours=72)).isoformat(timespec="seconds")
    for item in final_items:
        export_store.update_item(
            DB_PATH,
            job_id,
            int(item["account_id"]),
            delete_state="ready",
            updated_at=now_local(),
        )

    n_out = sum(1 for row in final_rows if _domain_bucket(row.get("email") or "") == "outlook.com")
    n_hot = sum(1 for row in final_rows if _domain_bucket(row.get("email") or "") == "hotmail.com")
    _export_update(
        job_id,
        state="running",
        phase="deleting",
        filename=filename,
        file_path=str(path),
        file_count=len(final_rows),
        file_parts=file_parts,
        export_outlook=n_out,
        export_hotmail=n_hot,
        checksum=checksum,
        expires_at=expires_at,
        downloaded_at="",
        deleted_at="",
        error="",
    )
    return final_items


def _delete_export_items(job_id: str, is_cancelled) -> None:
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        return
    items = export_store.get_items(
        DB_PATH,
        job_id,
        test_state="tested_ok",
        delete_states=("ready",),
    )
    workers = clamp_remote_concurrency(job.get("concurrency"), 8)
    remote_error = ""
    try:
        remote_context = _remote_session(pool=workers + 2, thread_safe=True)
    except Exception as exc:  # noqa: BLE001
        remote_context = (None, "", "", False, {})
        remote_error = str(exc)

    def delete_one(item: dict[str, Any]) -> None:
        if is_cancelled():
            return
        account_id = int(item["account_id"])
        email = str(item.get("email") or "")
        remote_status = str(item.get("remote_status") or "")
        if not remote_status.startswith("deleted"):
            session, base, csrf_token, csrf_disabled, _ = remote_context
            if session is None:
                remote_status = f"failed:{remote_error or '远程会话失败'}"[:1000]
            else:
                try:
                    result = delete_account_remote(session, base, csrf_token, csrf_disabled, email)
                    remote_ok = bool(result.get("success", False)) or result.get("http_status") in (200, 204, 404)
                    if remote_ok:
                        remote_status = "deleted_pending_local"
                    else:
                        remote_status = f"failed:{result.get('error') or result.get('http_status')}"[:1000]
                except Exception as exc:  # noqa: BLE001
                    remote_status = f"failed:{exc}"[:1000]

        if is_cancelled():
            export_store.update_item(
                DB_PATH,
                job_id,
                account_id,
                remote_status=remote_status,
                updated_at=now_local(),
            )
            return
        export_store.delete_local_account(DB_PATH, job_id, account_id, remote_status, now_local())

    try:
        run_cancellable(items, workers, delete_one, is_cancelled)
    finally:
        _close_remote_session(remote_context[0])
    if is_cancelled():
        _export_set_cancelled(job_id, "delete_interrupted")
        return

    summary = export_store.counts(DB_PATH, job_id)
    current = export_store.get_job(DB_PATH, job_id) or {}
    if summary["deleted"] < int(current.get("file_count") or 0):
        _export_update(
            job_id,
            state="failed",
            phase="delete_interrupted",
            error="部分账号删除尚未完成",
            finished_at=now_local(),
        )
        return

    _export_update(
        job_id,
        state="done",
        phase="done",
        cancel_requested=0,
        error="",
        finished_at=now_local(),
    )
    schedule_stats_refresh(0.2)
    view = _export_view(job_id) or {}
    log_event(
        "EXPORT",
        f"导出完成 file={current.get('filename', '')} count={current.get('file_count', 0)} "
        f"outlook={current.get('export_outlook', 0)} hotmail={current.get('export_hotmail', 0)} "
        f"remote_fail={view.get('export_remote_failed', 0)}",
    )


def _finalize_export(job_id: str, is_cancelled, *, resume_delete: bool = False) -> None:
    try:
        if resume_delete:
            _export_update(job_id, state="running", phase="deleting", cancel_requested=0, finished_at="", error="")
        else:
            built = _build_export_file(job_id, is_cancelled)
            if not built:
                return
        if is_cancelled():
            _export_set_cancelled(job_id, "delete_interrupted")
            return
        _delete_export_items(job_id, is_cancelled)
    except Exception as exc:  # noqa: BLE001
        current = export_store.get_job(DB_PATH, job_id) or {}
        tested_ok = export_store.counts(DB_PATH, job_id)["tested_ok"]
        phase = "delete_interrupted" if current.get("file_path") else ("partial_ready" if tested_ok else "failed")
        _export_update(
            job_id,
            state="failed",
            phase=phase,
            error=str(exc)[:1000],
            finished_at=now_local(),
        )
        log_event("EXPORT", f"导出任务异常 job={job_id} | {exc}", "FAIL")


def _export_runner(job_id: str):
    def runner(progress, is_cancelled):
        cancel_check = _sticky_cancel_check(is_cancelled)
        progress.set_total((export_store.get_job(DB_PATH, job_id) or {}).get("total", 0))
        try:
            job = export_store.get_job(DB_PATH, job_id)
            if not job:
                return
            if bool(job.get("retest")):
                if not _run_export_tests(job_id, cancel_check):
                    return
            else:
                for item in export_store.get_items(DB_PATH, job_id, test_state="pending"):
                    if cancel_check():
                        _export_set_cancelled(job_id)
                        return
                    export_store.update_item(
                        DB_PATH,
                        job_id,
                        int(item["account_id"]),
                        test_state="tested_ok",
                        reason="未复测，沿用当前存活状态",
                        updated_at=now_local(),
                    )
            if cancel_check():
                _export_set_cancelled(job_id)
                return
            _finalize_export(job_id, cancel_check)
        except Exception as exc:  # noqa: BLE001
            _export_update(
                job_id,
                state="failed",
                phase="failed",
                error=str(exc)[:1000],
                finished_at=now_local(),
            )
            log_event("EXPORT", f"导出任务异常 job={job_id} | {exc}", "FAIL")

    return runner


@app.post("/api/accounts/export/preview")
def export_preview(payload: ExportPayload):
    _validate_export_payload(payload)
    plan = plan_export_selection(payload.count, payload.domain, payload.min_registered_days)
    return {
        "success": True,
        "count": payload.count,
        "file_parts": payload.file_parts,
        "domain": payload.domain,
        "min_registered_days": payload.min_registered_days,
        "eligible_total": plan["eligible_total"],
        "eligible_outlook": plan["eligible_outlook"],
        "eligible_hotmail": plan["eligible_hotmail"],
        "plan_outlook": plan["plan_outlook"],
        "plan_hotmail": plan["plan_hotmail"],
        "plan_total": plan["plan_total"],
        "will_export": plan["export_count"],
        "shortfall": plan["shortfall"],
    }


def _validate_export_payload(payload: ExportPayload) -> None:
    if payload.count < 1:
        raise HTTPException(status_code=400, detail="数量必须 ≥ 1")
    if payload.min_registered_days < 0:
        raise HTTPException(status_code=400, detail="注册满天数不能为负")
    if payload.file_parts < 1 or payload.file_parts > 100:
        raise HTTPException(status_code=400, detail="文件个数必须在 1-100 之间")
    if payload.file_parts > payload.count:
        raise HTTPException(status_code=400, detail="文件个数不能超过导出数量")


@app.post("/api/accounts/export/run")
def export_run(payload: ExportPayload):
    _validate_export_payload(payload)
    workers = clamp_concurrency(payload.concurrency, 8)
    plan = plan_export_selection(payload.count, payload.domain, payload.min_registered_days)
    candidates = list(plan["candidates"] if payload.retest else plan["selected"])
    if not candidates:
        raise HTTPException(status_code=400, detail="没有符合条件的可导出账号")

    job_id = _export_id()
    ts = now_local()
    suffix = "zip" if payload.file_parts > 1 else "txt"
    filename = f"{time.strftime('%Y%m%d%H%M%S')}-Microsoft-mail.{suffix}"
    try:
        export_store.create_job(
            DB_PATH,
            {
                "id": job_id,
                "state": "running",
                "phase": "queued",
                "requested_count": payload.count,
                "domain": payload.domain,
                "min_registered_days": payload.min_registered_days,
                "retest": payload.retest,
                "concurrency": workers,
                "total": len(candidates),
                "filename": filename,
                "file_parts": payload.file_parts,
                "started_at": ts,
                "updated_at": ts,
            },
            [
                {
                    "account_id": int(row["id"]),
                    "email": row.get("email") or "",
                    "domain": _domain_bucket(row.get("email") or ""),
                    "ordinal": index,
                }
                for index, row in enumerate(candidates)
            ],
            require_no_unresolved=True,
        )
    except export_store.UnresolvedExportError as exc:
        raise HTTPException(status_code=409, detail=f"请先处理未结束的导出任务 {exc.job_id}") from exc
    jobs.submit_custom(
        "export",
        len(candidates),
        _export_runner(job_id),
        job_id=job_id,
        job_extra={"export_filename": filename, "export_phase": "queued"},
    )
    log_event(
        "EXPORT",
        f"导出任务启动 job={job_id} count={payload.count} files={payload.file_parts} domain={payload.domain} "
        f"min_days={payload.min_registered_days} retest={payload.retest} concurrency={workers}",
    )
    return {
        "success": True,
        "job_id": job_id,
        "filename": filename,
        "file_parts": payload.file_parts,
        "plan_total": plan["plan_total"],
        "eligible_total": plan["eligible_total"],
        "concurrency": workers,
    }


@app.post("/api/accounts/export/{job_id}/finalize")
def finalize_partial_export(job_id: str):
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    phase = str(job.get("phase") or "")
    if phase not in ("partial_ready", "delete_interrupted"):
        raise HTTPException(status_code=409, detail="当前任务状态不支持继续导出")
    if phase == "partial_ready" and export_store.counts(DB_PATH, job_id)["tested_ok"] <= 0:
        raise HTTPException(status_code=409, detail="没有已复测通过的账号")

    resume_delete = phase == "delete_interrupted"

    def runner(progress, is_cancelled):
        cancel_check = _sticky_cancel_check(is_cancelled)
        progress.set_total((export_store.get_job(DB_PATH, job_id) or {}).get("total", 0))
        _finalize_export(job_id, cancel_check, resume_delete=resume_delete)

    claimed = export_store.claim_job_phase(
        DB_PATH,
        job_id,
        (phase,),
        state="running",
        phase="deleting" if resume_delete else "building",
        cancel_requested=0,
        finished_at="",
        error="",
        updated_at=now_local(),
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="任务正在执行或状态已变化，请刷新后重试")
    jobs.submit_custom(
        "export",
        int(job.get("total") or 0),
        runner,
        job_id=job_id,
        job_extra={"export_filename": job.get("filename") or "", "export_phase": phase},
    )
    return {"success": True, "job_id": job_id, "resume_delete": resume_delete}


@app.post("/api/accounts/export/{job_id}/discard")
def discard_partial_export(job_id: str):
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if job.get("state") == "running":
        raise HTTPException(status_code=409, detail="请先终止当前任务")
    if job.get("phase") not in ("partial_ready", "cancelled"):
        raise HTTPException(status_code=409, detail="当前任务状态不支持放弃")
    claimed = export_store.claim_job_phase(
        DB_PATH,
        job_id,
        ("partial_ready", "cancelled"),
        state="cancelled",
        phase="discarded",
        cancel_requested=1,
        finished_at=now_local(),
        updated_at=now_local(),
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="任务正在执行或状态已变化，请刷新后重试")
    if not job.get("file_path"):
        for suffix in ("txt", "zip"):
            orphan = EXPORT_DIR / f"{job_id}.{suffix}"
            if orphan.is_file():
                orphan.unlink()
    return {"success": True}


@app.get("/api/accounts/export/download/{job_id}")
def export_download(job_id: str):
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if job.get("state") != "done":
        raise HTTPException(status_code=409, detail="导出删除流程尚未完成")
    path = Path(str(job.get("file_path") or ""))
    try:
        path.resolve().relative_to(EXPORT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="导出文件路径无效")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    expires = parse_time_value(job.get("expires_at"))
    if expires is not None and expires <= time.time():
        _delete_export_file(job_id, job)
        raise HTTPException(status_code=410, detail="导出文件已过期并清理")
    expected = str(job.get("checksum") or "")
    actual = _export_checksum(path)
    if expected and actual != expected:
        raise HTTPException(status_code=409, detail="导出文件校验失败")
    _export_update(job_id, downloaded_at=now_local())
    log_event("EXPORT", f"下载导出文件 {job.get('filename', '')} | job={job_id}")
    return FileResponse(
        path,
        media_type="application/zip" if path.suffix.lower() == ".zip" else "text/plain; charset=utf-8",
        filename=job.get("filename") or path.name,
        headers={
            "X-Count": str(int(job.get("file_count") or 0)),
            "X-File-Parts": str(max(1, int(job.get("file_parts") or 1))),
            "X-Export-Job": job_id,
        },
    )


def _delete_export_file(job_id: str, job: dict[str, Any] | None = None) -> bool:
    job = job or export_store.get_job(DB_PATH, job_id)
    if not job:
        return False
    raw_path = str(job.get("file_path") or "")
    if raw_path:
        path = Path(raw_path)
        try:
            path.resolve().relative_to(EXPORT_DIR.resolve())
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
    _export_update(job_id, file_path="", deleted_at=now_local())
    return True


def cleanup_expired_exports() -> int:
    cleaned = 0
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT * FROM export_jobs WHERE COALESCE(file_path,'')!='' AND COALESCE(deleted_at,'')=''"
        ).fetchall()
    now = time.time()
    for row in rows:
        job = dict(row)
        expires = parse_time_value(job.get("expires_at"))
        if expires is not None and expires <= now and _delete_export_file(job["id"], job):
            cleaned += 1
    return cleaned


def _export_cleanup_worker() -> None:
    while not _export_cleanup_stop.wait(3600):
        try:
            cleanup_expired_exports()
        except Exception as exc:  # noqa: BLE001
            log_event("EXPORT", f"过期文件清理失败：{exc}", "WARN")


@app.delete("/api/accounts/export/{job_id}/file")
def delete_export_file(job_id: str):
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if job.get("state") == "running":
        raise HTTPException(status_code=409, detail="导出任务仍在运行")
    return {"success": _delete_export_file(job_id, job)}


@app.post("/api/accounts/export/{job_id}/retry-remote-delete")
def retry_export_remote_delete(job_id: str):
    job = export_store.get_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    items = [
        item for item in export_store.get_items(DB_PATH, job_id)
        if str(item.get("remote_status") or "").startswith("failed:")
    ]
    if not items:
        raise HTTPException(status_code=400, detail="没有待重试的远程删除")
    workers = clamp_remote_concurrency(job.get("concurrency"), 8)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, _ = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))

        def retry(item):
            result = delete_account_remote(session, base, csrf_token, csrf_disabled, item["email"])
            if is_cancelled():
                return
            ok = bool(result.get("success")) or result.get("http_status") in (200, 204, 404)
            status = "deleted" if ok else f"failed:{result.get('error') or result.get('http_status')}"[:1000]
            export_store.update_item(DB_PATH, job_id, int(item["account_id"]), remote_status=status, updated_at=now_local())
            progress(status="ok" if ok else "fail", account_id=item["account_id"], email=item["email"], reason=status)

        run_cancellable(items, workers, retry, is_cancelled)

    retry_job_id = jobs.submit_custom("export-remote-retry", len(items), runner)
    return {"success": True, "job_id": retry_job_id, "total": len(items), "concurrency": workers}


def recover_interrupted_exports() -> None:
    for job in export_store.list_jobs(DB_PATH, 500):
        state = str(job.get("state") or "")
        phase = str(job.get("phase") or "")
        if state != "running" and phase != "cancelling":
            continue
        path = Path(str(job.get("file_path") or ""))
        if path.is_file() and phase in ("deleting", "delete_interrupted"):
            target_phase = "delete_interrupted"
        else:
            target_phase = "partial_ready" if export_store.counts(DB_PATH, job["id"])["tested_ok"] else "cancelled"
        export_store.update_job(
            DB_PATH,
            job["id"],
            state="cancelled",
            phase=target_phase,
            cancel_requested=1,
            updated_at=now_local(),
            finished_at=now_local(),
        )


@app.get("/api/accounts/{account_id}/detail")
def account_detail(account_id: int):
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")
        history = conn.execute(
            "SELECT action, status, detail, created_at FROM history WHERE account_id=? ORDER BY id DESC LIMIT 50",
            (account_id,),
        ).fetchall()
    return {
        "success": True,
        "account": dict(row),
        "history": [dict(h) for h in history],
        "locked": locks.is_locked(account_id),
    }


@app.get("/api/accounts/{account_id}/recovery-detail")
def recovery_detail(account_id: int):
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="账号不存在")
        history = conn.execute(
            """
            SELECT action, status, detail, created_at
            FROM history
            WHERE account_id=? AND action IN ('recover_abuse', 'bind_backup_email', 'oauth_reauth', 'captcha')
            ORDER BY id DESC LIMIT 50
            """,
            (account_id,),
        ).fetchall()
    return {
        "success": True,
        "account": dict(row),
        "history": [dict(h) for h in history],
        "locked": locks.is_locked(account_id),
    }


# ---------------- 导入 ----------------
def parse_lines(text: str) -> tuple[list[dict], list[dict]]:
    """返回 (valid_records, errors)。"""
    valid = []
    errors = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("----")]
        if len(parts) < 4 or not parts[0] or "@" not in parts[0]:
            errors.append({"line_no": line_no, "line": line[:80], "error": "格式错误（需 邮箱----密码----client_id----refresh_token）"})
            continue
        valid.append({
            "email": parts[0],
            "password": parts[1],
            "client_id": parts[2],
            "refresh_token": "----".join(parts[3:]).strip(),
        })
    return valid, errors


@app.post("/api/accounts/import-preview")
def import_preview(payload: ImportPayload):
    valid, errors = parse_lines(payload.text)
    with get_conn(DB_PATH) as conn:
        existing_emails = {r[0] for r in conn.execute("SELECT email FROM accounts").fetchall()}
    seen: set[str] = set()
    new_count = overwrite_count = dup_in_input = 0
    for rec in valid:
        email = rec["email"]
        if email in seen:
            dup_in_input += 1
            continue
        seen.add(email)
        if email in existing_emails:
            overwrite_count += 1
        else:
            new_count += 1
    return {
        "success": True,
        "total_lines": len(valid) + len(errors),
        "valid": len(valid),
        "new": new_count,
        "overwrite": overwrite_count,
        "dup_in_input": dup_in_input,
        "errors": errors[:50],
        "error_count": len(errors),
    }


def _do_import(text: str) -> dict[str, Any]:
    valid, errors = parse_lines(text)
    inserted = updated = 0
    ts = now_local()
    with get_conn(DB_PATH) as conn:
        for rec in valid:
            existing = conn.execute("SELECT id FROM accounts WHERE email=?", (rec["email"],)).fetchone()
            if existing:
                # 覆盖导入：更新密钥字段并重置缓存的健康/协议/远程状态
                conn.execute(
                    """
                    UPDATE accounts
                    SET password=?, client_id=?, refresh_token=?,
                        status='new', health_status='', health_severity='', ban_reason='', error_detail='',
                        graph_status='', imap_status='', pop_status='', smtp_status='',
                        last_protocol_test_at='',
                        remote_sync_status='dirty', refresh_token_updated_at=?,
                        token_sync_status='local_newer',
                        updated_at=?
                    WHERE email=?
                    """,
                    (rec["password"], rec["client_id"], rec["refresh_token"], ts, ts, rec["email"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO accounts (
                        email, password, client_id, refresh_token,
                        status, remote_sync_status, refresh_token_updated_at, token_sync_status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'new', 'dirty', ?, 'local_newer', ?, ?)
                    """,
                    (rec["email"], rec["password"], rec["client_id"], rec["refresh_token"], ts, ts, ts),
                )
                inserted += 1
        conn.commit()
    schedule_stats_refresh()
    log_event("IMPORT", f"导入完成 | 新增 {inserted} | 覆盖 {updated} | 错误 {len(errors)}")
    return {"success": True, "inserted": inserted, "updated": updated, "errors": errors[:50], "error_count": len(errors)}


@app.post("/api/accounts/import-text")
def import_accounts_text(payload: ImportPayload):
    return _do_import(payload.text)


@app.post("/api/accounts/import-file")
async def import_accounts_file(file: UploadFile = File(...)):
    raw = await file.read()
    return _do_import(raw.decode("utf-8", errors="ignore"))


def default_import_path() -> Path:
    """默认导入文件：优先 config.default_import_file，否则取注册项目产出的 oauth2.txt。"""
    cfg_path = (get_config().get("default_import_file") or "").strip()
    if cfg_path:
        p = Path(cfg_path)
        return p if p.is_absolute() else (ROOT / p)
    return ROOT.parent / "OutlookRegister" / "Results" / "oauth2.txt"


@app.get("/api/accounts/load-default-file")
def load_default_file():
    file_path = default_import_path()
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"默认导入文件不存在：{file_path}")
    text = file_path.read_text(encoding="utf-8")
    result = _do_import(text)
    # 导入后清空源文件：移除所有已成功解析的账号行，仅保留无法解析的异常行（防误删）
    remaining = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("----")]
        if len(parts) < 4 or not parts[0] or "@" not in parts[0]:
            remaining.append(raw)  # 异常行保留，便于排查
    file_path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
    result["source_cleared"] = True
    result["remaining_lines"] = len(remaining)
    log_event("IMPORT", f"已清空 {file_path.name}（移除已导入账号，保留 {len(remaining)} 个异常行）")
    return result


@app.put("/api/accounts/{account_id}")
def edit_account(account_id: int, payload: EditPayload):
    row = fetch_account(account_id)
    fields = []
    values: list[Any] = []
    for col in ("password", "client_id", "refresh_token", "remark"):
        val = getattr(payload, col)
        if val is not None:
            fields.append(f"{col}=?")
            values.append(val)
    if not fields:
        return {"success": True, "message": "无更新"}
    ts = now_local()
    if payload.refresh_token is not None and payload.refresh_token != row["refresh_token"]:
        fields.append("remote_sync_status=?")
        values.append("dirty")
        fields.append("refresh_token_updated_at=?")
        values.append(ts)
        fields.append("token_sync_status=?")
        values.append("local_newer")
    fields.append("updated_at=?")
    values.append(ts)
    values.append(account_id)
    with get_conn(DB_PATH) as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    return {"success": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, remote: bool = False):
    row = fetch_account(account_id)
    remote_result = None
    if remote:
        session = None
        try:
            session, base, csrf_token, csrf_disabled, _ = _remote_session()
            remote_result = delete_account_remote(session, base, csrf_token, csrf_disabled, row["email"])
        except Exception as exc:  # noqa: BLE001
            remote_result = {"success": False, "error": str(exc)}
        finally:
            _close_remote_session(session)
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.execute("DELETE FROM history WHERE account_id=?", (account_id,))
        conn.commit()
    schedule_stats_refresh()
    log_event("ACCOUNT", f"删除账号 {row['email']} | 远程={'是' if remote else '否'}", "WARN")
    return {"success": True, "remote": remote_result}


class DeletePayload(BaseModel):
    ids: list[int]
    remote: bool = False
    concurrency: int | None = None


@app.post("/api/accounts/batch/delete")
def batch_delete(payload: DeletePayload):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="未指定账号")
    with get_conn(DB_PATH) as conn:
        marks = ",".join("?" for _ in payload.ids)
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, email FROM accounts WHERE id IN ({marks})", payload.ids).fetchall()]
    if not rows:
        raise HTTPException(status_code=400, detail="账号不存在")

    if not payload.remote:
        # 仅本地删除：一次事务完成，立即返回
        with get_conn(DB_PATH) as conn:
            conn.execute(f"DELETE FROM accounts WHERE id IN ({marks})", payload.ids)
            conn.execute(f"DELETE FROM history WHERE account_id IN ({marks})", payload.ids)
            conn.commit()
        schedule_stats_refresh()
        log_event("ACCOUNT", f"批量删除 {len(rows)} 个账号（仅本地）", "WARN")
        return {"success": True, "deleted": len(rows), "remote": False}

    # 含远程删除：后台任务逐个删远程 + 本地
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, _ = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))

        def do_one(r):
            res = delete_account_remote(session, base, csrf_token, csrf_disabled, r["email"])
            if is_cancelled():
                return
            ok = bool(res.get("success", False)) or res.get("http_status") in (200, 204, 404)
            # 远程硬失败时保留本地，避免「本地没了远程还在」
            if ok:
                with get_conn(DB_PATH) as conn:
                    conn.execute("DELETE FROM accounts WHERE id=?", (r["id"],))
                    conn.execute("DELETE FROM history WHERE account_id=?", (r["id"],))
                    conn.commit()
                reason = "本地+远程已删除"
            else:
                reason = f"远程删除失败，本地已保留：{res.get('error') or res.get('http_status')}"
            progress(status="ok" if ok else "fail", account_id=r["id"], email=r["email"], reason=reason)

        run_cancellable(rows, workers, do_one, is_cancelled)
        schedule_stats_refresh()
        log_event("ACCOUNT", f"批量删除任务{'已终止' if is_cancelled() else '完成'}（本地+远程）", "WARN")

    job_id = jobs.submit_custom("delete", len(rows), runner)
    return {"success": True, "job_id": job_id, "total": len(rows), "remote": True}


@app.post("/api/accounts/{account_id}/remote-remove")
def remote_remove_one(account_id: int):
    """从远程移除该账号，本地保留（标记 remote_sync_status=removed）。"""
    row = fetch_account(account_id)
    auto_remote_delete(account_id, row["email"])
    return {"success": True}


@app.post("/api/accounts/batch/remote-remove")
def batch_remote_remove(payload: DeletePayload):
    """批量从远程移除，本地全部保留。"""
    if not payload.ids:
        raise HTTPException(status_code=400, detail="未指定账号")
    with get_conn(DB_PATH) as conn:
        marks = ",".join("?" for _ in payload.ids)
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, email FROM accounts WHERE id IN ({marks})", payload.ids).fetchall()]
    if not rows:
        raise HTTPException(status_code=400, detail="账号不存在")
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, _ = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))

        def do_one(r):
            res = delete_account_remote(session, base, csrf_token, csrf_disabled, r["email"])
            if is_cancelled():
                return
            ok = bool(res.get("success", False)) or res.get("http_status") in (200, 204, 404)
            ts = now_local()
            with get_conn(DB_PATH) as conn:
                conn.execute("UPDATE accounts SET remote_sync_status=?, remote_sync_at=?, remote_sync_error=?, updated_at=? WHERE id=?",
                             ("removed" if ok else "fail", ts, "" if ok else str(res.get("error") or res.get("http_status"))[:300], ts, r["id"]))
                add_history(conn, r["id"], r["email"], "remote-remove", "ok" if ok else "fail",
                            "已从远程移除(本地保留)" if ok else "远程移除失败", ts)
                conn.commit()
            progress(status="ok" if ok else "fail", account_id=r["id"], email=r["email"],
                     reason="已从远程移除(本地保留)" if ok else "远程移除失败")

        run_cancellable(rows, workers, do_one, is_cancelled)
        log_event("ACCOUNT", f"批量从远程移除任务{'已终止' if is_cancelled() else '完成'}（本地保留）", "WARN")

    job_id = jobs.submit_custom("remote-remove", len(rows), runner)
    return {"success": True, "job_id": job_id, "total": len(rows)}


def fetch_account(account_id: int):
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    return row


def write_account_result(
    callback: Callable[[Any], None],
    is_cancelled: Callable[[], bool] | None = None,
) -> bool:
    def write(conn):
        if is_cancelled and is_cancelled():
            return False
        callback(conn)
        return True

    return bool(db_writer.execute(DB_PATH, write))


# ---------------- 刷新 token ----------------
def refresh_with_graph(client_id: str, refresh_token: str, proxy: str | None):
    session = requests.Session()
    session.verify = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    attempts = []
    for label, scope in (("default", GRAPH_SCOPE), ("original", None)):
        data = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
        if scope:
            data["scope"] = scope
        resp = session.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        attempts.append({"label": label, "status_code": resp.status_code, "body": body})
        if resp.status_code == 200 and body.get("access_token"):
            return {"success": True, "access_token": body.get("access_token", ""),
                    "refresh_token": body.get("refresh_token", ""), "scope": body.get("scope", ""), "attempts": attempts}
    return {"success": False, "attempts": attempts, "error": attempts[-1]["body"] if attempts else "unknown"}


def refresh_one(account_id: int, task: jobs.TaskContext | None = None) -> dict[str, Any]:
    row = fetch_account(account_id)
    if task and task.is_cancelled():
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
    if not locks.try_acquire(account_id):
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "账号正在执行其他任务"}
    post_sync = False
    post_delete = False
    out: dict[str, Any] = {
        "status": "fail",
        "account_id": account_id,
        "email": row["email"],
        "reason": "未知错误",
    }
    try:
        # 远程同步/删除放到 release 之后，缩短账号锁占用
        result = refresh_with_graph(row["client_id"], row["refresh_token"], proxy_url())
        if task and task.is_cancelled():
            return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
        ts = now_local()
        if result["success"]:
            new_refresh = result.get("refresh_token") or row["refresh_token"]
            rotated = bool(result.get("refresh_token"))
            rotated_text = "更新 refresh_token 成功" if rotated else "更新 refresh_token 失败"

            def write_success(conn):
                conn.execute(
                    """UPDATE accounts SET refresh_token=?,
                       last_refresh_at=?, last_refresh_status='ok', last_refresh_error='',
                       refresh_token_updated_at=?, token_sync_status='local_newer',
                       last_alive_at=?,
                       updated_at=? WHERE id=?""",
                    (new_refresh, ts, ts, ts, ts, account_id),
                )
                add_history(conn, account_id, row["email"], "refresh", "ok", rotated_text, ts)

            if not write_account_result(write_success, task.is_cancelled if task else None):
                return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
            schedule_stats_refresh()
            log_event("REFRESH", f"{row['email']} 刷新成功 | {rotated_text}")
            post_sync = True
            out = {
                "status": "ok",
                "account_id": account_id,
                "email": row["email"],
                "rotated": rotated,
                "reason": rotated_text,
                "scope": result.get("scope", ""),
                "attempts": result["attempts"],
            }
        else:
            analysis = diagnostics.analyze_error(result.get("error", ""))
            reason = f"[{analysis['code']}] {analysis['reason']}" if analysis["code"] else analysis["reason"]
            is_banned = analysis["severity"] == "banned"
            new_health = "banned" if is_banned else ("token_invalid" if analysis["severity"] == "fail" else "")
            new_sev = "banned" if is_banned else (analysis["severity"] or row["health_severity"] or "fail")

            def write_failure(conn):
                conn.execute(
                    """UPDATE accounts SET status='refresh_fail', health_status=?, health_severity=?,
                       last_refresh_at=?, last_refresh_status='fail', last_refresh_error=?,
                       ban_reason=?, updated_at=? WHERE id=?""",
                    (
                        new_health or row["health_status"],
                        new_sev,
                        ts,
                        reason[:1000],
                        reason if is_banned else "",
                        ts,
                        account_id,
                    ),
                )
                add_history(conn, account_id, row["email"], "refresh", "fail", reason, ts)

            if not write_account_result(write_failure, task.is_cancelled if task else None):
                return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
            schedule_stats_refresh()
            log_event("REFRESH", f"{row['email']} 刷新失败 | {reason}", "FAIL")
            post_delete = is_banned
            out = {
                "status": "fail",
                "account_id": account_id,
                "email": row["email"],
                "reason": reason,
                "severity": analysis["severity"],
                "attempts": result.get("attempts", []),
            }
    except Exception as exc:  # noqa: BLE001
        out = {
            "status": "fail",
            "account_id": account_id,
            "email": row["email"],
            "reason": f"刷新异常：{exc}",
        }
        log_event("REFRESH", f"{row['email']} 刷新异常 | {exc}", "FAIL")
    finally:
        locks.release(account_id)
    if post_sync and not (task and task.is_cancelled()):
        auto_remote_sync(account_id)
    if post_delete and not (task and task.is_cancelled()):
        auto_remote_delete(account_id, row["email"])
    return out


@app.post("/api/accounts/{account_id}/refresh")
def refresh_account(account_id: int):
    res = refresh_one(account_id)
    if res["status"] == "ok":
        return {"success": True, **res}
    return JSONResponse({"success": False, **res}, status_code=400 if res["status"] == "fail" else 409)


# ---------------- 协议测试 ----------------
def protocol_one(
    account_id: int,
    job_id: str | None = None,
    task: jobs.TaskContext | None = None,
) -> dict[str, Any]:
    """单账号协议测试。

    批量任务会传入 job_id：走可杀子进程；取消后不再写库，直接 skip。
    """
    if task is not None:
        job_id = task.job_id
    row = fetch_account(account_id)
    if job_id and jobs.is_cancelled(job_id):
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
    if not locks.try_acquire(account_id):
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "账号正在执行其他任务"}
    post_delete = False
    out: dict[str, Any] = {
        "status": "fail",
        "account_id": account_id,
        "email": row["email"],
        "reason": "未知错误",
    }
    try:
        if job_id and jobs.is_cancelled(job_id):
            out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
            return out
        log_event("PROTO", f"开始测试 {row['email']}")
        final_outcome = None
        health = None
        attempts_used = 0
        proto_cfg = (get_config().get("protocol_test") or {})
        ext_rcpt = str(proto_cfg.get("external_recipient") or "")
        for attempt in range(1, 3):
            if job_id and jobs.is_cancelled(job_id):
                out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                return out
            attempts_used = attempt
            # 有 job_id 时默认子进程（可被 cancel 立刻 kill）；单条点测仍同进程
            final_outcome = run_protocol_test(
                PYTHON_EXE,
                TEST_SCRIPT,
                ROOT,
                account_line(row),
                proxy_url=proxy_url(),
                external_recipient=ext_rcpt,
                protocol_cfg=proto_cfg,
                job_id=job_id,
                use_subprocess=bool(job_id) if job_id else False,
            )
            if job_id and jobs.is_cancelled(job_id):
                out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                return out
            if final_outcome.get("aborted") and job_id and jobs.is_cancelled(job_id):
                out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                return out
            if not final_outcome["success"] and "health" not in final_outcome:
                if attempt == 1 and not (job_id and jobs.is_cancelled(job_id)):
                    log_event("PROTO", f"{row['email']} 脚本异常，准备重试 | {summarize_protocol_error(final_outcome)}", "WARN")
                    time.sleep(1)
                    continue
                if job_id and jobs.is_cancelled(job_id):
                    out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                    return out
                ts = now_local()
                reason = summarize_protocol_error(final_outcome)
                def write_failure(conn):
                    conn.execute(
                        """
                        UPDATE accounts
                        SET status='proto_error', health_status='other_error', health_severity='fail',
                            ban_reason='', error_detail=?, graph_status='', imap_status='', pop_status='', smtp_status='',
                            last_protocol_test_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (reason, ts, ts, account_id),
                    )
                    add_history(conn, account_id, row["email"], "protocol", "fail", reason, ts)
                if not write_account_result(write_failure, lambda: bool(job_id and jobs.is_cancelled(job_id))):
                    return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                schedule_stats_refresh()
                log_event("PROTO", f"{row['email']} 测试失败 | {reason}", "FAIL")
                out = {
                    "status": "fail",
                    "account_id": account_id,
                    "email": row["email"],
                    "health": "other_error",
                    "severity": "fail",
                    "reason": reason,
                    "stderr": final_outcome.get("stderr", ""),
                }
                return out
            health = final_outcome["health"]
            if suspected_restricted(health) and attempt == 1:
                if job_id and jobs.is_cancelled(job_id):
                    out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
                    return out
                log_event("PROTO", f"{row['email']} 命中疑似受限，准备重试", "WARN")
                time.sleep(1)
                continue
            if suspected_restricted(health):
                health = to_other_error(health, "二次测试仍为疑似受限，已归入其他错误")
            break

        if job_id and jobs.is_cancelled(job_id):
            out = {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
            return out

        ts = now_local()
        registration = ((final_outcome or {}).get("result") or {}).get("registration") or {}
        registration_value = str(registration.get("registered_at") or "").strip()
        registration_source_value = str(registration.get("registered_source") or "").strip()
        existing_registered_at = str(row["registered_at"] or "").strip()
        existing_registered_source = str(row["registered_source"] or "").strip()
        created_at_fallback = str(row["created_at"] or "").strip()
        registered_at = registration_value or existing_registered_at or created_at_fallback or ""
        if registration_value:
            registered_source = registration_source_value or existing_registered_source or ""
        elif existing_registered_at:
            registered_source = existing_registered_source or ""
        elif created_at_fallback:
            registered_source = "db_created_at"
        else:
            registered_source = ""
        last_alive_at = ts if is_alive_health(health) else str(row["last_alive_at"] or "")
        def write_result(conn):
            conn.execute(
                """UPDATE accounts SET status=?, health_status=?, health_severity=?, ban_reason=?, error_detail=?,
                   graph_status=?, imap_status=?, pop_status=?, smtp_status=?,
                   registered_at=?, registered_source=?, last_alive_at=?,
                   last_protocol_test_at=?, updated_at=? WHERE id=?""",
                (health["health_status"], health["health_status"], health["health_severity"],
                 health["ban_reason"], health["error_detail"],
                 health["graph_status"], health["imap_status"], health["pop_status"], health["smtp_status"],
                 registered_at, registered_source, last_alive_at,
                 ts, ts, account_id),
            )
            hist_status = "ok" if health["health_severity"] in ("ok", "warn") else health["health_severity"]
            add_history(conn, account_id, row["email"], "protocol", hist_status,
                        health["ban_reason"] or health["error_detail"] or health["health_status"], ts)
        if not write_account_result(write_result, lambda: bool(job_id and jobs.is_cancelled(job_id))):
            return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
        schedule_stats_refresh()
        log_event(
            "PROTO",
            f"{row['email']} 测试完成 | health={health['health_status']} severity={health['health_severity']} | attempts={attempts_used}",
        )
        post_delete = health["health_status"] == "banned" or health.get("health_severity") == "banned"
        status = "ok" if health["health_severity"] in ("ok", "warn") else "fail"
        out = {
            "status": status,
            "account_id": account_id,
            "email": row["email"],
            "health": health["health_status"],
            "severity": health["health_severity"],
            "reason": health["ban_reason"] or health["error_detail"],
        }
    finally:
        locks.release(account_id)
    if post_delete and not (job_id and jobs.is_cancelled(job_id)):
        auto_remote_delete(account_id, row["email"])
    return out


@app.post("/api/accounts/{account_id}/protocol-test")
def protocol_test(account_id: int):
    res = protocol_one(account_id)
    if res["status"] == "skip":
        return JSONResponse({"success": False, **res}, status_code=409)
    return {"success": res["status"] == "ok", **res}


def recover_abuse_one(account_id: int, task: jobs.TaskContext | None = None) -> dict[str, Any]:
    row = fetch_account(account_id)
    row_dict = dict(row)
    if task and task.is_cancelled():
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
    if not locks.try_acquire(account_id):
        return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "账号正在执行其他任务"}
    try:
        log_event("RECOVER", f"开始恢复 account_id={account_id} email={row['email']}")
        cfg = get_config()
        recovery_cfg = cfg.get("recovery") or {}
        if not recovery_cfg.get("enabled", True):
            return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "恢复功能未启用"}
        if not is_abuse_candidate(row_dict):
            analysis = diagnostics.analyze_recovery_reason("not_abuse")
            return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": analysis["reason"]}

        def hook(stage: str, message: str, level: str = "INFO") -> None:
            log_event(f"RECOVER:{row['email']}", f"{stage} | {message}", level)

        result = recover_abuse_account(row_dict, cfg, proxy_url=proxy_url(), log_hook=hook)
        if task and task.is_cancelled():
            return {"status": "skip", "account_id": account_id, "email": row["email"], "reason": "任务已取消"}
        ts = now_local()
        with get_conn(DB_PATH) as conn:
            if result["success"]:
                new_refresh = result["refresh_token"]
                conn.execute(
                    """
                    UPDATE accounts
                    SET refresh_token=?, status='new', health_status='', health_severity='',
                        ban_reason='', error_detail='',
                        last_protocol_test_at='',
                        refresh_token_updated_at=?, token_sync_status='local_newer',
                        last_alive_at=?,
                        recovery_status='recovered', recovery_last_at=?, recovery_last_reason=?, recovery_last_temp_mail=?,
                        oauth_reauth_at=?, oauth_reauth_status='ok', oauth_reauth_error='',
                        remote_sync_status='dirty', updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_refresh,
                        ts,
                        ts,
                        ts,
                        result["reason"],
                        result.get("temp_mail", ""),
                        ts,
                        ts,
                        account_id,
                    ),
                )
                add_history(conn, account_id, row["email"], "recover_abuse", "ok", result["reason"], ts)
                if result.get("temp_mail"):
                    add_history(conn, account_id, row["email"], "bind_backup_email", "ok", result["temp_mail"], ts)
                add_history(conn, account_id, row["email"], "oauth_reauth", "ok", "refresh_token 已更新", ts)
                conn.commit()
                log_event("RECOVER", f"恢复成功 account_id={account_id} email={row['email']} debug_log={result.get('debug_log', '')}", "INFO")
                out = {
                    "status": "ok",
                    "success": True,
                    "account_id": account_id,
                    "email": row["email"],
                    "reason": result["reason"],
                    "temp_mail": result.get("temp_mail", ""),
                    "debug_log": result.get("debug_log", ""),
                }
            else:
                analysis = diagnostics.analyze_recovery_reason(result.get("reason_code", ""), result.get("reason", ""))
                # 不再累计失败次数，也不区分「不可恢复」——失败统一记为 failed
                status_value = "failed"
                oauth_status = "fail" if result.get("reason_code") == "reauth_failed" else ""
                oauth_at = ts if oauth_status else ""
                oauth_error = analysis["reason"][:1000] if oauth_status else ""
                conn.execute(
                    """
                    UPDATE accounts
                    SET recovery_status=?, recovery_last_at=?, recovery_last_reason=?, recovery_last_temp_mail=?,
                        oauth_reauth_at=?, oauth_reauth_status=?, oauth_reauth_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        status_value,
                        ts,
                        analysis["reason"][:1000],
                        result.get("temp_mail", ""),
                        oauth_at,
                        oauth_status,
                        oauth_error,
                        ts,
                        account_id,
                    ),
                )
                hist_action = "captcha" if result.get("reason_code") == "captcha_failed" else "recover_abuse"
                add_history(conn, account_id, row["email"], hist_action, "fail", analysis["reason"], ts)
                if result.get("temp_mail"):
                    add_history(conn, account_id, row["email"], "bind_backup_email", "ok", result["temp_mail"], ts)
                if result.get("reason_code") == "reauth_failed":
                    add_history(conn, account_id, row["email"], "oauth_reauth", "fail", analysis["reason"], ts)
                conn.commit()
                log_event(
                    "RECOVER",
                    f"恢复失败 account_id={account_id} email={row['email']} code={result.get('reason_code', '')} "
                    f"reason={analysis['reason']} debug_log={result.get('debug_log', '')}",
                    "FAIL",
                )
                out = {
                    "status": "fail",
                    "success": False,
                    "account_id": account_id,
                    "email": row["email"],
                    "reason": analysis["reason"],
                    "severity": analysis["severity"],
                    "temp_mail": result.get("temp_mail", ""),
                    "debug_log": result.get("debug_log", ""),
                }
    except Exception as exc:  # noqa: BLE001
        out = {
            "status": "fail",
            "success": False,
            "account_id": account_id,
            "email": row["email"],
            "reason": f"恢复异常：{exc}",
        }
        log_event("RECOVER", f"恢复异常 account_id={account_id} email={row['email']} | {exc}", "FAIL")
    finally:
        locks.release(account_id)
    return out


@app.post("/api/accounts/batch/recover-abuse")
def batch_recover_abuse(payload: RecoveryBatchPayload):
    with get_conn(DB_PATH) as conn:
        if payload.ids:
            marks = ",".join("?" for _ in payload.ids)
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM accounts WHERE id IN ({marks}) ORDER BY id", payload.ids).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()]
    rows = [r for r in rows if is_abuse_candidate(r)]
    ids = [int(r["id"]) for r in rows]
    if not ids:
        raise HTTPException(status_code=400, detail="没有可恢复的滥用封禁账号")
    workers = clamp_concurrency(payload.concurrency, 1)
    job_id = jobs.submit("recover-abuse", ids, recover_abuse_one, max_workers=workers)
    log_event("BATCH", f"批量恢复滥用封禁 {len(ids)} 个账号 | 并发={workers} | job={job_id}")
    return {"success": True, "job_id": job_id, "total": len(ids), "concurrency": workers}


@app.post("/api/accounts/{account_id}/recover-abuse")
def recover_abuse_api(account_id: int):
    res = recover_abuse_one(account_id)
    if res["status"] == "ok":
        return {"success": True, **res}
    return JSONResponse({"success": False, **res}, status_code=400 if res["status"] == "fail" else 409)


# ---------------- 批量任务 ----------------
def _ids_or_all(payload: BatchPayload, where_all: str) -> list[int]:
    """指定 ids 时也会叠加 where_all 过滤（修复原先勾选封禁号仍入队的问题）。"""
    with get_conn(DB_PATH) as conn:
        if payload.ids:
            marks = ",".join("?" for _ in payload.ids)
            rows = conn.execute(
                f"SELECT id FROM accounts WHERE id IN ({marks}) AND ({where_all}) ORDER BY id",
                list(payload.ids),
            ).fetchall()
            return [r[0] for r in rows]
        rows = conn.execute(f"SELECT id FROM accounts WHERE {where_all} ORDER BY id").fetchall()
        return [r[0] for r in rows]


@app.post("/api/accounts/batch/refresh")
def batch_refresh(payload: BatchPayload):
    ids = _ids_or_all(payload, SQL_NOT_BANNED)
    if not ids:
        raise HTTPException(status_code=400, detail="没有可处理的账号")
    workers = clamp_concurrency(payload.concurrency, 8)
    job_id = jobs.submit("refresh", ids, refresh_one, max_workers=workers)
    log_event("BATCH", f"批量刷新 {len(ids)} 个账号 | 并发={workers} | job={job_id}")
    return {"success": True, "job_id": job_id, "total": len(ids), "concurrency": workers}


@app.post("/api/accounts/batch/protocol")
def batch_protocol(payload: BatchPayload):
    ids = _ids_or_all(payload, SQL_NOT_BANNED)
    if not ids:
        raise HTTPException(status_code=400, detail="没有可处理的账号")
    workers = clamp_concurrency(payload.concurrency, 4)
    job_id = jobs.submit("protocol", ids, protocol_one, max_workers=workers)
    log_event("BATCH", f"批量协议测试 {len(ids)} 个账号 | 并发={workers} | job={job_id}")
    return {"success": True, "job_id": job_id, "total": len(ids), "concurrency": workers}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _export_view(job_id) or jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 大批量时只回传最近 100 条明细，避免轮询响应过大
    if len(job.get("items", [])) > 100:
        job = {**job, "items": job["items"][-100:], "items_truncated": True}
    return {"success": True, "job": job}


@app.get("/api/jobs")
def list_jobs():
    export_views = [export_store.job_view(DB_PATH, job) for job in export_store.list_jobs(DB_PATH, 20)]
    export_ids = {job["id"] for job in export_views}
    combined = export_views + [job for job in jobs.list_jobs() if job["id"] not in export_ids]
    combined.sort(key=lambda job: job.get("started_at") or "", reverse=True)
    return {"success": True, "jobs": combined[:20]}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    export_job = export_store.get_job(DB_PATH, job_id)
    if export_job and export_job.get("state") == "running":
        _export_update(job_id, cancel_requested=1, finished_at="")
    ok = jobs.cancel(job_id)
    if export_job and export_job.get("state") == "running":
        ok = True
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已结束")
    log_event("JOB", f"任务 {job_id} 已取消", "WARN")
    return {"success": True}


# ---------------- 远程导入 / 同步 ----------------
def _remote_session(pool: int = 10, thread_safe: bool = False):
    remote = get_config()["remote"]
    base = remote["base_url"].rstrip("/")
    session = build_session(proxy_url(), pool_maxsize=pool)
    try:
        ok, detail = login(session, base, remote["password"])
        if not ok:
            raise RuntimeError(f"远程登录失败: {detail}")
        csrf_token, csrf_disabled = get_csrf(session, base)
        if thread_safe:
            session = AuthenticatedSessionPool(session, proxy_url(), pool_maxsize=max(2, pool))
        return session, base, csrf_token, csrf_disabled, remote
    except Exception:
        _close_remote_session(session)
        raise


def _close_remote_session(session) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


# 共享远程会话缓存：必须 thread_safe Session，避免多线程共用 requests.Session 踩踏
_remote_cache: dict[str, Any] = {}
_remote_cache_lock = threading.Lock()


def get_cached_remote(ttl: int = 600):
    with _remote_cache_lock:
        if _remote_cache.get("bundle") and (time.time() - _remote_cache.get("ts", 0)) < ttl:
            return _remote_cache["bundle"]
        old_bundle = _remote_cache.pop("bundle", None)
        if old_bundle:
            _close_remote_session(old_bundle[0])
        bundle = _remote_session(pool=20, thread_safe=True)
        _remote_cache["bundle"] = bundle
        _remote_cache["ts"] = time.time()
        return bundle


def invalidate_remote_cache() -> None:
    with _remote_cache_lock:
        bundle = _remote_cache.pop("bundle", None)
        _remote_cache.clear()
    if bundle:
        _close_remote_session(bundle[0])


def auto_remote_delete(account_id: int, email: str) -> None:
    """账号被判封禁时：从远程删除（本地保留），标记 remote_sync_status=removed。最佳努力。"""
    ts = now_local()
    try:
        session, base, csrf_token, csrf_disabled, _ = get_cached_remote()
        result = delete_account_remote(session, base, csrf_token, csrf_disabled, email)
        ok = bool(result.get("success", False)) or result.get("http_status") in (200, 204, 404)
        status = "removed" if ok else "fail"
        err = "" if ok else str(result.get("error") or result.get("message") or result.get("http_status") or "远程删除失败")[:500]
        log_event("REMOTE", f"{email} 封禁 → {'已从远程删除（本地保留）' if ok else err}", "INFO" if ok else "WARN")
    except Exception as exc:  # noqa: BLE001
        status, err = "fail", f"远程删除失败：{exc}"[:500]
        log_event("REMOTE", f"{email} 远程删除失败：{exc}", "WARN")
    with get_conn(DB_PATH) as conn:
        conn.execute("UPDATE accounts SET remote_sync_status=?, remote_sync_at=?, remote_sync_error=?, updated_at=? WHERE id=?",
                     (status, ts, err, ts, account_id))
        conn.commit()


def auto_remote_sync(account_id: int) -> None:
    """刷新成功后：把账号全部信息(含密码)同步到远程（本地为准 upsert）。最佳努力。"""
    row = fetch_account(account_id)
    try:
        session, base, csrf_token, csrf_disabled, remote = get_cached_remote()
        retry_on_locked(
            _remote_upsert_one,
            session, base, csrf_token, csrf_disabled, remote, dict(row), lambda **k: None, "auto-sync",
        )
    except Exception as exc:  # noqa: BLE001
        ts = now_local()
        error = f"自动同步失败：{exc}"[:500]
        try:
            log_event("REMOTE", f"{row['email']} {error}", "WARN")
        except Exception:
            pass

        def _write_fail(conn):
            conn.execute(
                "UPDATE accounts SET remote_sync_status='fail', remote_sync_error=?, updated_at=? WHERE id=?",
                (error, ts, account_id),
            )

        try:
            retry_on_locked(db_writer.execute, DB_PATH, _write_fail)
        except Exception as exc2:  # noqa: BLE001
            try:
                log_event("REMOTE", f"{row['email']} 自动同步失败，写本地记录也失败：{exc2}", "WARN")
            except Exception:
                pass


def count_pending_upload_untested(conn, ids: list[int] | None = None) -> int:
    clauses = [
        "COALESCE(remote_sync_status, '') NOT IN ('synced','imported','exists')",
        "(last_protocol_test_at IS NULL OR last_protocol_test_at = '')",
        "COALESCE(health_status, '') = ''",
    ]
    params: list[Any] = []
    if ids:
        marks = ",".join("?" for _ in ids)
        clauses.append(f"id IN ({marks})")
        params.extend(ids)
    return conn.execute(f"SELECT COUNT(*) FROM accounts WHERE {' AND '.join(clauses)}", params).fetchone()[0]


@app.post("/api/accounts/batch/test-untested")
def batch_test_untested(payload: BatchPayload):
    """一键测试所有未测试的账号（last_protocol_test_at 为空且非封禁）。"""
    ids = _ids_or_all(
        payload,
        f"(last_protocol_test_at IS NULL OR last_protocol_test_at = '') AND {SQL_NOT_BANNED}",
    )
    if not ids:
        raise HTTPException(status_code=400, detail="没有未测试的账号")
    workers = clamp_concurrency(payload.concurrency, 4)
    job_id = jobs.submit("protocol", ids, protocol_one, max_workers=workers)
    log_event("BATCH", f"一键测试未测试：启动 {len(ids)} 个账号，并发 {workers}")
    return {"success": True, "job_id": job_id, "total": len(ids), "concurrency": workers}


@app.post("/api/accounts/batch/test-missing-registration")
def batch_test_missing_registration(payload: BatchPayload):
    """只测试尚未获取注册时间的账号。"""
    with get_conn(DB_PATH) as conn:
        if payload.ids:
            marks = ",".join("?" for _ in payload.ids)
            rows = conn.execute(
                f"""
                SELECT id FROM accounts
                WHERE id IN ({marks})
                  AND COALESCE(registered_at, '') = ''
                  AND {SQL_NOT_BANNED}
                ORDER BY id
                """,
                payload.ids,
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT id FROM accounts
                WHERE COALESCE(registered_at, '') = ''
                  AND {SQL_NOT_BANNED}
                ORDER BY id
                """
            ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        raise HTTPException(status_code=400, detail="没有未获取注册时间的账号")
    workers = clamp_concurrency(payload.concurrency, 4)
    job_id = jobs.submit("protocol", ids, protocol_one, max_workers=workers)
    log_event("BATCH", f"补测注册时间：启动 {len(ids)} 个账号，并发 {workers}")
    return {"success": True, "job_id": job_id, "total": len(ids), "concurrency": workers}


@app.post("/api/accounts/batch/sync-never-synced")
def batch_sync_never_synced(payload: BatchPayload):
    """一键同步所有正常且尚未确认同步到远程的账号。"""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT * FROM accounts
            WHERE (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
              AND COALESCE(remote_sync_status, '') NOT IN ('synced','imported','exists')
            ORDER BY id
            """
        ).fetchall()
        if not rows:
            pending_untested = count_pending_upload_untested(conn)
            if pending_untested:
                raise HTTPException(status_code=400, detail=f"存在 {pending_untested} 个未测试账号，请先完成协议测试后再上传")
    if not rows:
        raise HTTPException(status_code=400, detail="没有需要上传的账号")
    rows = [dict(r) for r in rows]
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, remote = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))
        try:
            remote_items = list_remote_accounts(session, base)
            if is_cancelled():
                return
            session._email_index = index_accounts_by_email(remote_items)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log_event("BATCH", f"预拉远程账号列表失败，回退逐号搜索：{exc}", "WARN")
            session._email_index = None  # type: ignore[attr-defined]
        log_event("BATCH", f"一键同步未上传：开始同步 {len(rows)} 个账号到远程，并发 {workers}")

        def do_one(r):
            _remote_upsert_safe(session, base, csrf_token, csrf_disabled, remote, r, progress, "sync-never-synced", is_cancelled)

        run_cancellable(rows, workers, do_one, is_cancelled)
        log_event("BATCH", f"一键同步未上传：{'已终止' if is_cancelled() else '完成'}", "WARN" if is_cancelled() else "OK")

    job_id = jobs.submit_custom("remote-import", len(rows), runner)
    return {"success": True, "job_id": job_id, "total": len(rows), "concurrency": workers}


@app.post("/api/remote/import")
def remote_import(payload: BatchPayload):
    with get_conn(DB_PATH) as conn:
        if payload.ids:
            pending_untested = count_pending_upload_untested(conn, payload.ids)
            if pending_untested:
                raise HTTPException(status_code=400, detail=f"所选账号中有 {pending_untested} 个未测试，请先完成协议测试后再上传")
            marks = ",".join("?" for _ in payload.ids)
            rows = conn.execute(
                f"""
                SELECT * FROM accounts
                WHERE id IN ({marks})
                  AND (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                ORDER BY id
                """,
                payload.ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM accounts
                WHERE (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                  AND COALESCE(remote_sync_status, '') NOT IN ('synced','imported','exists')
                ORDER BY id
                """
            ).fetchall()
            if not rows:
                pending_untested = count_pending_upload_untested(conn)
                if pending_untested:
                    raise HTTPException(status_code=400, detail=f"存在 {pending_untested} 个未测试账号，请先完成协议测试后再上传")
    if not rows:
        raise HTTPException(status_code=400, detail="没有需要上传的账号")
    rows = [dict(r) for r in rows]
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, remote = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))
        try:
            remote_items = list_remote_accounts(session, base)
            if is_cancelled():
                return
            session._email_index = index_accounts_by_email(remote_items)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log_event("REMOTE", f"预拉远程账号列表失败，回退逐号搜索：{exc}", "WARN")
            session._email_index = None  # type: ignore[attr-defined]

        def do_one(r):
            _remote_upsert_safe(session, base, csrf_token, csrf_disabled, remote, r, progress, "remote-import", is_cancelled)

        run_cancellable(rows, workers, do_one, is_cancelled)
        log_event("REMOTE", f"远程导入(本地为准 upsert){'已终止' if is_cancelled() else '完成'} | 并发={workers}")

    job_id = jobs.submit_custom("remote-import", len(rows), runner)
    return {"success": True, "job_id": job_id, "total": len(rows), "concurrency": workers}


def remote_refresh_time(item: dict[str, Any]) -> str:
    status = str(item.get("last_refresh_status") or "").lower()
    if status not in ("success", "ok"):
        return ""
    return str(item.get("last_refresh_at") or "").strip()


def compare_token_times(local_value: str, remote_value: str) -> str:
    local_ts = parse_time_value(local_value)
    remote_ts = parse_time_value(remote_value)
    if local_ts is not None and remote_ts is not None:
        if remote_ts > local_ts:
            return "remote_newer"
        if local_ts > remote_ts:
            return "local_newer"
        return "same_time"
    if remote_ts is not None:
        return "remote_newer"
    if local_ts is not None:
        return "local_newer"
    return "unknown"


def mark_remote_conflict(conn, r, item, ts: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE accounts
        SET remote_sync_status='conflict',
            remote_id=?,
            remote_sync_at=?,
            remote_sync_error=?,
            remote_last_refresh_at=?,
            remote_last_refresh_status=?,
            token_sync_status='conflict',
            updated_at=?
        WHERE id=?
        """,
        (
            str(item.get("id") or ""),
            ts,
            reason[:500],
            str(item.get("last_refresh_at") or ""),
            str(item.get("last_refresh_status") or ""),
            ts,
            r["id"],
        ),
    )


def pull_remote_token_to_local(conn, r, item, detail, ts: str) -> None:
    conn.execute(
        """
        UPDATE accounts
        SET refresh_token=?,
            client_id=?,
            last_refresh_at=?,
            last_refresh_status=?,
            last_refresh_error=?,
            refresh_token_updated_at=?,
            remote_id=?,
            remote_sync_status='synced',
            remote_sync_at=?,
            remote_sync_error='',
            remote_last_refresh_at=?,
            remote_last_refresh_status=?,
            token_sync_status='remote_newer',
            updated_at=?
        WHERE id=?
        """,
        (
            str(detail.get("refresh_token") or ""),
            str(detail.get("client_id") or item.get("client_id") or r["client_id"]),
            str(item.get("last_refresh_at") or ""),
            str(item.get("last_refresh_status") or ""),
            str(item.get("last_refresh_error") or "")[:1000],
            remote_refresh_time(item) or ts,
            str(item.get("id") or ""),
            ts,
            str(item.get("last_refresh_at") or ""),
            str(item.get("last_refresh_status") or ""),
            ts,
            r["id"],
        ),
    )


def _remote_upsert_one(
    session, base, csrf_token, csrf_disabled, remote, r, progress, action, is_cancelled=lambda: False
):
    """单账号同步到远程：远程已存在 → 用本地 token 覆盖(PUT)并纠正分组；不存在 → 按域名分组新增。本地为准。
    滥用封禁账号一律跳过，不推送到远程。
    远程 HTTP 阶段不持有本地写事务；所有本地写入合并为一次短事务，progress 在 commit 之后上报，
    避免多 worker 长时间占用 SQLite 写锁导致 database is locked。"""
    ts = now_local()
    aid = r["id"]
    email = r["email"]
    if is_cancelled():
        return

    def commit_writes(writes, status, reason):
        if not writes:
            return

        def run():
            with get_conn(DB_PATH) as conn:
                for write in writes:
                    write(conn)
                conn.commit()

        retry_on_locked(run)
        try:
            progress(status=status, account_id=aid, email=email, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    def fail_write(err):
        def write(conn):
            conn.execute(
                "UPDATE accounts SET remote_sync_status='fail', remote_sync_at=?, remote_sync_error=?, updated_at=? WHERE id=?",
                (ts, str(err)[:500], ts, aid),
            )
            add_history(conn, aid, email, action, "fail", str(err), ts)

        return write

    def skip_write(reason):
        def write(conn):
            conn.execute(
                "UPDATE accounts SET remote_sync_status='skipped', remote_sync_at=?, remote_sync_error=?, updated_at=? WHERE id=?",
                (ts, str(reason)[:500], ts, aid),
            )
            add_history(conn, aid, email, action, "skip", str(reason), ts)

        return write

    def conflict_write(item, reason):
        def write(conn):
            mark_remote_conflict(conn, r, item, ts, reason)
            add_history(conn, aid, email, action, "fail", reason, ts)

        return write

    if is_banned_row(r):
        commit_writes(
            [lambda conn: add_history(conn, aid, email, action, "skip", "滥用封禁，跳过同步", ts)],
            "skip", "滥用封禁，跳过同步",
        )
        return
    # 优先用预拉的 email 索引，减少每号 search
    email_index = getattr(session, "_email_index", None)
    if isinstance(email_index, dict):
        item = email_index.get(str(email or "").lower())
    else:
        item = find_account(session, base, email)
    if is_cancelled():
        return
    if not item:
        gid = group_for_email(email)
        if gid is None:
            domain = email.rsplit("@", 1)[-1]
            reason = f"域名 {domain} 不在同步范围"
            commit_writes([skip_write(reason)], "skip", reason)
            return
        result = import_accounts(session, base, csrf_token, csrf_disabled, [account_line(r)],
                                 gid, remote["provider"], remote["account_format"])
        if is_cancelled():
            return
        added = int(result.get("added_count", 0) or 0)
        skipped = int(result.get("skipped_count", 0) or 0)
        if added > 0:
            def write_added(conn):
                conn.execute("UPDATE accounts SET remote_sync_status='synced', remote_sync_at=?, remote_sync_error='', updated_at=? WHERE id=?",
                             (ts, ts, aid))
                add_history(conn, aid, email, action, "ok", f"远程不存在，新增导入(分组{gid})", ts)
            commit_writes([write_added], "ok", f"新增导入(分组{gid})")
            return
        if skipped > 0:
            existing = None
            if isinstance(email_index, dict):
                existing = email_index.get(str(email or "").lower())
            if existing is None:
                existing = find_account(session, base, email)
            if is_cancelled():
                return
            if existing:
                def write_calibrated(conn):
                    conn.execute(
                        """
                        UPDATE accounts
                        SET remote_sync_status='synced', remote_id=?, remote_sync_at=?,
                            remote_sync_error='', updated_at=?
                        WHERE id=?
                        """,
                        (str(existing.get("id") or ""), ts, ts, aid),
                    )
                    add_history(conn, aid, email, action, "ok", "远程已存在，状态已校准", ts)
                commit_writes([write_calibrated], "ok", "远程已存在，状态已校准")
            else:
                err = result.get("error") or result.get("message") or "远程报告重复，但搜索未命中"
                commit_writes([fail_write(err)], "fail", str(err))
            return
        err = result.get("error") or result.get("message") or f"导入失败 HTTP {result.get('http_status', '')}".strip()
        commit_writes([fail_write(err)], "fail", str(err))
        return
    # 远程已存在：按 refresh_token 更新时间决定同步方向，避免用旧 token 覆盖新 token
    detail = get_account_detail(session, base, item.get("id"))
    if is_cancelled():
        return
    remote_token = str(detail.get("refresh_token") or "")
    remote_client_id = str(detail.get("client_id") or item.get("client_id") or "")
    remote_time = remote_refresh_time(item)
    local_time = token_time(r)
    if remote_token and remote_token == str(r["refresh_token"] or "") and (not remote_client_id or remote_client_id == str(r["client_id"] or "")):
        def write_token_synced(conn):
            conn.execute(
                """
                UPDATE accounts
                SET remote_sync_status='synced', remote_id=?, remote_sync_at=?, remote_sync_error='',
                    last_refresh_at=?, last_refresh_status=?, last_refresh_error=?,
                    refresh_token_updated_at=CASE WHEN ? != '' THEN ? ELSE refresh_token_updated_at END,
                    remote_last_refresh_at=?, remote_last_refresh_status=?, token_sync_status='synced',
                    updated_at=?
                WHERE id=?
                """,
                (
                    str(item.get("id")),
                    ts,
                    str(item.get("last_refresh_at") or ""),
                    str(item.get("last_refresh_status") or ""),
                    str(item.get("last_refresh_error") or "")[:1000],
                    remote_time,
                    remote_time,
                    str(item.get("last_refresh_at") or ""),
                    str(item.get("last_refresh_status") or ""),
                    ts,
                    aid,
                ),
            )
            add_history(conn, aid, email, action, "ok", "本地与远程 token 一致，状态已校准", ts)
        commit_writes([write_token_synced], "ok", "token 已同步")
        return
    direction = compare_token_times(local_time, remote_time)
    if direction == "remote_newer":
        if not remote_token:
            reason = "远程较新但详情未返回 refresh_token"
            commit_writes([conflict_write(item, reason)], "fail", reason)
            return
        moved = resync_target_group(email, item.get("group_id"))
        if moved is not None:
            update_remote_token(session, base, csrf_token, csrf_disabled,
                                item, remote_client_id or r["client_id"], remote_token, group_id=moved, password=r.get("password"))
            if is_cancelled():
                return

        def write_pulled(conn):
            pull_remote_token_to_local(conn, r, item, detail, ts)
            add_history(conn, aid, email, action, "ok", "远程 token 更新较晚，已拉回本地", ts)
        commit_writes([write_pulled], "ok", "远程 token 较新，已拉回本地")
        return
    if direction in ("unknown", "same_time"):
        reason = "本地与远程 token 不一致，且无法可靠判断更新时间"
        commit_writes([conflict_write(item, reason)], "fail", reason)
        return
    # 本地较新 → 推送本地 token，并纠正放错的分组
    moved = resync_target_group(email, item.get("group_id"))
    result = update_remote_token(session, base, csrf_token, csrf_disabled,
                                 item, r["client_id"], r["refresh_token"], group_id=moved, password=r.get("password"))
    if is_cancelled():
        return
    if result.get("success") and int(result.get("http_status", 200) or 200) < 400:
        def write_pushed(conn):
            conn.execute(
                """
                UPDATE accounts
                SET remote_sync_status='synced', remote_id=?, remote_sync_at=?, remote_sync_error='',
                    remote_last_refresh_at=?, remote_last_refresh_status=?, token_sync_status='local_newer',
                    updated_at=?
                WHERE id=?
                """,
                (str(item.get("id")), ts, str(item.get("last_refresh_at") or ""), str(item.get("last_refresh_status") or ""), ts, aid),
            )
            add_history(conn, aid, email, action, "ok",
                        "本地 token 更新较晚，已推送远程" + (f"，分组纠正→{moved}" if moved is not None else ""), ts)
        commit_writes([write_pushed], "ok", "本地 token 较新，已推送远程")
    else:
        err = result.get("error") or result.get("message") or "更新失败"
        commit_writes([fail_write(err)], "fail", str(err))


def _remote_upsert_safe(
    session, base, csrf_token, csrf_disabled, remote, row, progress, action, is_cancelled=lambda: False
):
    try:
        retry_on_locked(
            _remote_upsert_one,
            session, base, csrf_token, csrf_disabled, remote, row, progress, action, is_cancelled,
        )
    except Exception as exc:  # noqa: BLE001
        if is_cancelled():
            return
        ts = now_local()
        error = f"远程同步异常：{exc}"[:500]

        def _write_fail(conn):
            conn.execute(
                """
                UPDATE accounts
                SET remote_sync_status='fail', remote_sync_at=?, remote_sync_error=?, updated_at=?
                WHERE id=?
                """,
                (ts, error, ts, row["id"]),
            )
            add_history(conn, row["id"], row["email"], action, "fail", error, ts)

        try:
            retry_on_locked(db_writer.execute, DB_PATH, _write_fail)
        except Exception as exc2:  # noqa: BLE001
            try:
                log_event("REMOTE", f"{row['email']} 同步失败，写本地记录也失败：{exc2}", "WARN")
            except Exception:
                pass
            return
        try:
            progress(status="fail", account_id=row["id"], email=row["email"], reason=error)
        except Exception:  # noqa: BLE001
            pass


@app.post("/api/remote/resync")
def remote_resync(payload: BatchPayload):
    """把本地刷新后(dirty/指定)账号的 token 推送到远程（本地为准 upsert）。"""
    with get_conn(DB_PATH) as conn:
        if payload.ids:
            marks = ",".join("?" for _ in payload.ids)
            rows = conn.execute(
                f"""
                SELECT * FROM accounts
                WHERE id IN ({marks})
                  AND (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                ORDER BY id
                """,
                payload.ids,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM accounts
                WHERE remote_sync_status='dirty'
                  AND (graph_status='ok' OR imap_status='ok' OR pop_status='ok')
                ORDER BY id
                """
            ).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail="没有待同步账号")
    rows = [dict(r) for r in rows]
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, remote = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))
        try:
            remote_items = list_remote_accounts(session, base)
            if is_cancelled():
                return
            session._email_index = index_accounts_by_email(remote_items)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log_event("REMOTE", f"预拉远程账号列表失败，回退逐号搜索：{exc}", "WARN")
            session._email_index = None  # type: ignore[attr-defined]

        def do_one(r):
            _remote_upsert_safe(session, base, csrf_token, csrf_disabled, remote, r, progress, "remote-resync", is_cancelled)

        run_cancellable(rows, workers, do_one, is_cancelled)
        log_event("REMOTE", f"远程 token 同步{'已终止' if is_cancelled() else '完成'} | 并发={workers}")

    job_id = jobs.submit_custom("remote-resync", len(rows), runner)
    return {"success": True, "job_id": job_id, "total": len(rows), "concurrency": workers}


@app.post("/api/remote/reconcile")
def remote_reconcile(payload: BatchPayload):
    with get_conn(DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()]
    normal_rows = [r for r in rows if is_normal_account(r)]
    banned_rows = [r for r in rows if is_banned_row(r)]
    workers = clamp_remote_concurrency(payload.concurrency, 4)

    def runner(progress, is_cancelled):
        session, base, csrf_token, csrf_disabled, remote = _remote_session(pool=workers + 2, thread_safe=True)
        progress.add_cleanup(lambda: _close_remote_session(session))
        remote_items = list_remote_accounts(session, base)
        if is_cancelled():
            return
        remote_by_email = {str(item.get("email", "")).lower(): item for item in remote_items}
        local_by_email = {row["email"].lower(): row for row in rows}
        bulk_group_ids = {int(value) for value in (remote.get("group_map") or {}).values()}
        stale_remote = [
            item for item in remote_items
            if int(item.get("group_id") or 0) in bulk_group_ids
            and str(item.get("email", "")).lower() not in local_by_email
        ]
        progress.set_total(len(normal_rows) + len(banned_rows) + len(stale_remote))
        sync_rows = []
        ts = now_local()

        with get_conn(DB_PATH) as conn:
            for row in normal_rows:
                if is_cancelled():
                    conn.rollback()
                    return
                item = remote_by_email.get(row["email"].lower())
                if item and row.get("remote_sync_status") != "dirty":
                    conn.execute(
                        """
                        UPDATE accounts
                        SET remote_sync_status='synced', remote_id=?, remote_sync_at=?,
                            remote_sync_error='', updated_at=?
                        WHERE id=?
                        """,
                        (str(item.get("id") or ""), ts, ts, row["id"]),
                    )
                    progress(status="ok", account_id=row["id"], email=row["email"], reason="远程已存在，状态已校准")
                else:
                    sync_rows.append(row)
            for row in banned_rows:
                if is_cancelled():
                    conn.rollback()
                    return
                if row["email"].lower() not in remote_by_email:
                    conn.execute(
                        """
                        UPDATE accounts
                        SET remote_sync_status='removed', remote_sync_at=?,
                            remote_sync_error='', updated_at=?
                        WHERE id=?
                        """,
                        (ts, ts, row["id"]),
                    )
                    progress(status="ok", account_id=row["id"], email=row["email"], reason="封禁账号远程已不存在")
            conn.commit()

        def sync_one(row):
            _remote_upsert_safe(session, base, csrf_token, csrf_disabled, remote, row, progress, "remote-reconcile", is_cancelled)

        run_cancellable(sync_rows, workers, sync_one, is_cancelled)
        if is_cancelled():
            log_event("REMOTE", "远程校准已终止：停止上传后续账号", "WARN")
            return

        banned_remote = [row for row in banned_rows if row["email"].lower() in remote_by_email]

        def remove_banned(row):
            try:
                result = delete_account_remote(session, base, csrf_token, csrf_disabled, row["email"])
                ok = bool(result.get("success", False)) or result.get("http_status") in (200, 204, 404)
                error = "" if ok else str(result.get("error") or result.get("message") or result.get("http_status") or "远程删除失败")[:500]
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = f"远程删除异常：{exc}"[:500]
            if is_cancelled():
                return
            now = now_local()
            with get_conn(DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE accounts
                    SET remote_sync_status=?, remote_sync_at=?, remote_sync_error=?, updated_at=?
                    WHERE id=?
                    """,
                    ("removed" if ok else "fail", now, error, now, row["id"]),
                )
                add_history(
                    conn,
                    row["id"],
                    row["email"],
                    "remote-reconcile",
                    "ok" if ok else "fail",
                    "封禁账号已从远程移除" if ok else error,
                    now,
                )
                conn.commit()
            progress(
                status="ok" if ok else "fail",
                account_id=row["id"],
                email=row["email"],
                reason="封禁账号已从远程移除" if ok else error,
            )

        run_cancellable(banned_remote, workers, remove_banned, is_cancelled)
        if is_cancelled():
            log_event("REMOTE", "远程校准已终止：停止清理远程孤立账号", "WARN")
            return

        def remove_stale(item):
            email = str(item.get("email") or "")
            try:
                result = delete_account_remote(session, base, csrf_token, csrf_disabled, email)
                ok = bool(result.get("success", False)) or result.get("http_status") in (200, 204, 404)
                error = "" if ok else str(result.get("error") or result.get("message") or result.get("http_status") or "远程删除失败")[:500]
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = f"远程删除异常：{exc}"[:500]
            if is_cancelled():
                return
            progress(
                status="ok" if ok else "fail",
                email=email,
                reason="已清理远程批量分组孤立账号" if ok else error,
            )

        run_cancellable(stale_remote, workers, remove_stale, is_cancelled)
        log_event("REMOTE", f"远程校准{'已终止' if is_cancelled() else '完成'}", "WARN" if is_cancelled() else "OK")

    total = len(normal_rows) + len(banned_rows)
    job_id = jobs.submit_custom("remote-reconcile", total, runner)
    return {
        "success": True,
        "job_id": job_id,
        "total": total,
        "normal": len(normal_rows),
        "banned": len(banned_rows),
        "concurrency": workers,
    }


# ---------------- 配置 ----------------
@app.get("/api/config")
def api_get_config():
    cfg = get_config()
    remote = cfg["remote"]
    ui = cfg.get("ui") or {}
    return {
        "success": True,
        "config": {
            "proxy_url": (cfg.get("proxy") or {}).get("url", ""),
            "remote_base_url": remote["base_url"],
            "remote_password": remote["password"],
            "group_map": remote.get("group_map") or {},
            "skip_unmapped": remote.get("skip_unmapped", True),
            "external_recipient": (cfg.get("protocol_test") or {}).get("external_recipient", ""),
            "default_concurrency": ui.get("default_concurrency", 100),
            "export_concurrency": ui.get("export_concurrency", 8),
        },
    }


@app.put("/api/config")
def put_config(payload: ConfigPayload):
    global CONFIG
    with _CONFIG_LOCK:
        cfg = load_config()
        if payload.proxy_url is not None:
            cfg.setdefault("proxy", {})["url"] = payload.proxy_url.strip()
        if payload.remote_base_url is not None:
            cfg["remote"]["base_url"] = payload.remote_base_url.strip()
        if payload.remote_password is not None:
            cfg["remote"]["password"] = payload.remote_password
        if payload.group_map is not None:
            cfg["remote"]["group_map"] = {
                str(k).strip().lower(): int(v)
                for k, v in payload.group_map.items()
                if str(k).strip()
            }
        if payload.skip_unmapped is not None:
            cfg["remote"]["skip_unmapped"] = payload.skip_unmapped
        if payload.external_recipient is not None:
            cfg.setdefault("protocol_test", {})["external_recipient"] = payload.external_recipient.strip()
        if payload.default_concurrency is not None:
            cfg.setdefault("ui", {})["default_concurrency"] = max(1, min(100, int(payload.default_concurrency)))
        if payload.export_concurrency is not None:
            cfg.setdefault("ui", {})["export_concurrency"] = max(1, min(100, int(payload.export_concurrency)))
        save_config(cfg)
        CONFIG = cfg
        invalidate_remote_cache()
    log_event("CONFIG", "配置已更新")
    return {"success": True}
