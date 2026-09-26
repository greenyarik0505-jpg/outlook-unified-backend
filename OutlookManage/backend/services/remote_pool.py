import threading

import requests
import urllib3
from urllib3.util.retry import Retry

urllib3.disable_warnings()
HTTP_TIMEOUT = 30
IMPORT_BATCH_SIZE = 50
REMOTE_MAX_CONCURRENCY = 12


class ThreadSafeSession:
    """Serialize requests on a shared Session (requests.Session is not thread-safe)."""

    def __init__(self, session: requests.Session):
        self._session = session
        self._lock = threading.RLock()
        self._closed = False

    @property
    def raw(self) -> requests.Session:
        return self._session

    def request(self, method, url, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("远程会话已关闭")
            return self._session.request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self):
        with self._lock:
            if not self._closed:
                self._session.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


def _adapter(pool_maxsize: int) -> requests.adapters.HTTPAdapter:
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST", "PUT", "DELETE")),
        respect_retry_after_header=True,
    )
    return requests.adapters.HTTPAdapter(
        pool_connections=max(1, pool_maxsize),
        pool_maxsize=max(1, pool_maxsize),
        max_retries=retry,
    )


def build_session(proxy_url: str | None, pool_maxsize: int = 10, thread_safe: bool = False):
    session = requests.Session()
    session.verify = False
    adapter = _adapter(pool_maxsize)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    if thread_safe:
        return ThreadSafeSession(session)
    return session


class AuthenticatedSessionPool:
    """One authenticated template with an independent Session per worker thread."""

    def __init__(self, template: requests.Session, proxy_url: str | None = None, pool_maxsize: int = 4):
        self._template = template
        self._proxy_url = proxy_url
        self._pool_maxsize = pool_maxsize
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()
        self._closed = False

    def session(self) -> requests.Session:
        with self._lock:
            if self._closed:
                raise RuntimeError("远程会话池已关闭")
        current = getattr(self._local, "session", None)
        if current is not None:
            return current
        current = build_session(self._proxy_url, self._pool_maxsize)
        current.headers.update(self._template.headers)
        current.cookies.update(self._template.cookies)
        with self._lock:
            if self._closed:
                current.close()
                raise RuntimeError("远程会话池已关闭")
            self._sessions.append(current)
        self._local.session = current
        return current

    @property
    def raw(self) -> requests.Session:
        return self.session()

    def request(self, method, url, **kwargs):
        return self.session().request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions, self._sessions = self._sessions, []
        for session in [self._template, *sessions]:
            session.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


def login(session, base_url: str, password: str):
    r = session.post(f"{base_url}/login", data={"password": password}, timeout=HTTP_TIMEOUT, verify=False)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        data = {}
    if data and not data.get("success", True):
        return False, data.get("message", str(data))
    return True, "ok"


def get_csrf(session, base_url: str):
    r = session.get(f"{base_url}/api/csrf-token", timeout=HTTP_TIMEOUT, verify=False)
    data = r.json()
    return data.get("csrf_token"), data.get("csrf_disabled", False)


def check_exists(session, base_url: str, email: str) -> bool:
    return find_account(session, base_url, email) is not None


def find_account(session, base_url: str, email: str):
    """搜索远程账号，命中则返回该 item（含 id），否则 None。"""
    r = session.get(
        f"{base_url}/api/accounts/search",
        params={"q": email},
        timeout=HTTP_TIMEOUT,
        verify=False,
    )
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    items = data.get("items", []) or data.get("accounts", [])
    for item in items:
        if str(item.get("email", "")).lower() == email.lower():
            return item
    return None


def get_account_detail(session, base_url: str, account_id):
    response = session.get(
        f"{base_url}/api/accounts/{account_id}",
        timeout=HTTP_TIMEOUT,
        verify=False,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("account") or data


def list_accounts(session, base_url: str, limit: int = 500):
    items = []
    offset = 0
    previous_marker = None
    page_size = max(1, min(int(limit or 500), 1000))
    while True:
        response = session.get(
            f"{base_url}/api/accounts",
            params={"limit": page_size, "offset": offset},
            timeout=HTTP_TIMEOUT,
            verify=False,
        )
        response.raise_for_status()
        data = response.json()
        page = data.get("accounts", []) or data.get("items", [])
        marker = tuple(str(item.get("id") or item.get("email") or "") for item in page)
        if page and marker == previous_marker:
            break
        previous_marker = marker
        items.extend(page)
        total = data.get("total")
        if not page or len(page) < page_size or (total is not None and len(items) >= int(total)):
            break
        offset += len(page)
    return items


def index_accounts_by_email(items) -> dict:
    """Build email(lower)->item map for bulk remote sync."""
    out = {}
    for item in items or []:
        email = str(item.get("email") or "").strip().lower()
        if email:
            out[email] = item
    return out


def update_remote_token(session, base_url, csrf_token, csrf_disabled, item, client_id, refresh_token, group_id=None, password=None):
    """更新远程已存在账号（PUT /api/accounts/<id>），以本地为准推送全部信息。"""
    headers = {"Content-Type": "application/json"}
    if not csrf_disabled and csrf_token:
        headers["X-CSRFToken"] = csrf_token
    body = {
        "email": item.get("email"),
        "client_id": client_id,
        "refresh_token": refresh_token,
        "account_type": "outlook",
        "provider": "outlook",
        "group_id": group_id if group_id is not None else item.get("group_id", 1),
        "sort_order": item.get("sort_order", 0),
        "remark": item.get("remark", "") or "",
        "status": item.get("status", "active"),
        "forward_enabled": bool(item.get("forward_enabled", False)),
    }
    if password is not None:
        body["password"] = password
    response = session.put(
        f"{base_url}/api/accounts/{item.get('id')}",
        json=body,
        headers=headers,
        timeout=HTTP_TIMEOUT,
        verify=False,
    )
    try:
        data = response.json()
        data.setdefault("http_status", response.status_code)
        return data
    except Exception:
        return {"success": False, "message": response.text[:500], "http_status": response.status_code}


def import_accounts(session, base_url, csrf_token, csrf_disabled, account_lines, group_id, provider, fmt):
    body = {
        "account_string": "\n".join(account_lines),
        "group_id": group_id,
        "account_format": fmt,
        "provider": provider,
    }
    headers = {"Content-Type": "application/json"}
    if not csrf_disabled and csrf_token:
        headers["X-CSRFToken"] = csrf_token
    response = session.post(
        f"{base_url}/api/accounts",
        json=body,
        headers=headers,
        timeout=max(HTTP_TIMEOUT, len(account_lines) * 2),
        verify=False,
    )
    try:
        data = response.json()
        data.setdefault("http_status", response.status_code)
        return data
    except Exception:
        return {"success": False, "message": response.text[:500], "http_status": response.status_code}


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def delete_account_remote(session, base_url, csrf_token, csrf_disabled, email):
    """按邮箱删除远程账号（DELETE /api/accounts/email/<email>）。"""
    from urllib.parse import quote
    headers = {}
    if not csrf_disabled and csrf_token:
        headers["X-CSRFToken"] = csrf_token
    resp = session.delete(
        f"{base_url}/api/accounts/email/{quote(email)}",
        headers=headers,
        timeout=HTTP_TIMEOUT,
        verify=False,
    )
    try:
        data = resp.json()
        data.setdefault("http_status", resp.status_code)
        return data
    except Exception:
        return {"success": resp.status_code in (200, 204), "http_status": resp.status_code}
