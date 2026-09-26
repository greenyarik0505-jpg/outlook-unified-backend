import sqlite3
import time
from pathlib import Path

LOCKED_RETRIES = 4
LOCKED_BASE_DELAY = 0.5


def retry_on_locked(fn, *args, retries=LOCKED_RETRIES, base_delay=LOCKED_BASE_DELAY, **kwargs):
    """SQLITE_BUSY 指数退避重试；非锁错误立即抛出。"""
    last = None
    for attempt in range(max(1, retries)):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last = exc
            time.sleep(base_delay * (2 ** attempt))
    raise last

ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    client_id TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    status TEXT DEFAULT 'new',
    health_status TEXT DEFAULT '',
    health_severity TEXT DEFAULT '',
    ban_reason TEXT DEFAULT '',
    error_detail TEXT DEFAULT '',
    graph_status TEXT DEFAULT '',
    imap_status TEXT DEFAULT '',
    pop_status TEXT DEFAULT '',
    smtp_status TEXT DEFAULT '',
    registered_at TEXT DEFAULT '',
    registered_source TEXT DEFAULT '',
    last_alive_at TEXT DEFAULT '',
    last_refresh_at TEXT DEFAULT '',
    last_refresh_status TEXT DEFAULT '',
    last_refresh_error TEXT DEFAULT '',
    refresh_token_updated_at TEXT DEFAULT '',
    last_protocol_test_at TEXT DEFAULT '',
    remote_sync_status TEXT DEFAULT '',
    remote_sync_at TEXT DEFAULT '',
    remote_sync_error TEXT DEFAULT '',
    remote_last_refresh_at TEXT DEFAULT '',
    remote_last_refresh_status TEXT DEFAULT '',
    token_sync_status TEXT DEFAULT '',
    remote_id TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    email TEXT DEFAULT '',
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_account ON history(account_id, id DESC);
"""

# 总览统计缓存：避免每次全表扫 / 前端等全量列表
STATS_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS stats_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

EXPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'running',
    phase TEXT NOT NULL DEFAULT 'queued',
    requested_count INTEGER NOT NULL DEFAULT 0,
    domain TEXT NOT NULL DEFAULT 'all',
    min_registered_days INTEGER NOT NULL DEFAULT 0,
    retest INTEGER NOT NULL DEFAULT 1,
    concurrency INTEGER NOT NULL DEFAULT 1,
    total INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    file_parts INTEGER NOT NULL DEFAULT 1,
    export_outlook INTEGER NOT NULL DEFAULT 0,
    export_hotmail INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',
    downloaded_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS export_items (
    job_id TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    test_state TEXT NOT NULL DEFAULT 'pending',
    delete_state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    remote_status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_export_jobs_updated ON export_jobs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_export_items_job_test ON export_items(job_id, test_state, ordinal);
CREATE INDEX IF NOT EXISTS idx_export_items_job_delete ON export_items(job_id, delete_state, ordinal);
"""

BACKGROUND_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS background_jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_updated ON background_jobs(updated_at DESC);

CREATE TABLE IF NOT EXISTS runtime_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    heartbeat REAL NOT NULL
);
"""

EXPORT_JOB_MIGRATIONS = {
    "file_parts": "INTEGER NOT NULL DEFAULT 1",
    "checksum": "TEXT NOT NULL DEFAULT ''",
    "downloaded_at": "TEXT NOT NULL DEFAULT ''",
    "expires_at": "TEXT NOT NULL DEFAULT ''",
    "deleted_at": "TEXT NOT NULL DEFAULT ''",
}

ACCOUNTS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_accounts_health ON accounts(health_status, health_severity);
CREATE INDEX IF NOT EXISTS idx_accounts_graph ON accounts(graph_status);
CREATE INDEX IF NOT EXISTS idx_accounts_imap ON accounts(imap_status);
CREATE INDEX IF NOT EXISTS idx_accounts_pop ON accounts(pop_status);
CREATE INDEX IF NOT EXISTS idx_accounts_remote ON accounts(remote_sync_status);
CREATE INDEX IF NOT EXISTS idx_accounts_proto ON accounts(last_protocol_test_at);
"""

# 增量迁移：旧库缺失的列在此补齐（幂等）
MIGRATIONS = {
    "health_severity": "TEXT DEFAULT ''",
    "ban_reason": "TEXT DEFAULT ''",
    "error_detail": "TEXT DEFAULT ''",
    "remote_id": "TEXT DEFAULT ''",
    "remark": "TEXT DEFAULT ''",
    "recovery_status": "TEXT DEFAULT ''",
    "recovery_attempts": "INTEGER DEFAULT 0",
    "recovery_last_at": "TEXT DEFAULT ''",
    "recovery_last_reason": "TEXT DEFAULT ''",
    "recovery_last_temp_mail": "TEXT DEFAULT ''",
    "oauth_reauth_at": "TEXT DEFAULT ''",
    "oauth_reauth_status": "TEXT DEFAULT ''",
    "oauth_reauth_error": "TEXT DEFAULT ''",
    "registered_at": "TEXT DEFAULT ''",
    "registered_source": "TEXT DEFAULT ''",
    "last_alive_at": "TEXT DEFAULT ''",
    "refresh_token_updated_at": "TEXT DEFAULT ''",
    "remote_last_refresh_at": "TEXT DEFAULT ''",
    "remote_last_refresh_status": "TEXT DEFAULT ''",
    "token_sync_status": "TEXT DEFAULT ''",
}


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "accounts.db"


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    elif isinstance(db_path, str):
        db_path = Path(db_path)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(ACCOUNTS_SCHEMA)
        conn.executescript(HISTORY_SCHEMA)
        conn.executescript(STATS_CACHE_SCHEMA)
        conn.executescript(EXPORT_SCHEMA)
        conn.executescript(BACKGROUND_JOBS_SCHEMA)
        conn.executescript(ACCOUNTS_INDEXES)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        for column, decl in MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {decl}")
        export_existing = {row[1] for row in conn.execute("PRAGMA table_info(export_jobs)").fetchall()}
        for column, decl in EXPORT_JOB_MIGRATIONS.items():
            if column not in export_existing:
                try:
                    conn.execute(f"ALTER TABLE export_jobs ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        conn.commit()


def add_history(conn: sqlite3.Connection, account_id: int, email: str, action: str, status: str, detail: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO history (account_id, email, action, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, email, action, status, str(detail or "")[:2000], ts),
    )
