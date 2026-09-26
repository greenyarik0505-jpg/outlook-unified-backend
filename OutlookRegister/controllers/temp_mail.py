"""CF Temp Mail 客户端：每任务独立地址 + JWT，避免多线程验证码串号。"""
import random
import re
import string
import threading
import time

import requests

# 公开发行：无内置密钥/域名，须由 config.json 的 temp_mail 段填写
DEFAULT_BASE = ""
DEFAULT_DOMAIN = ""
DEFAULT_ADMIN = ""
DEFAULT_PREFIX = "orx"

# 优先带「验证码/安全代码」上下文的数字，避免误匹配邮箱本地部分里的数字
_LABELED_CODE_RES = [
    re.compile(r"(?:安全代码|验证码|security\s*code|verification\s*code)[^\d]{0,48}(\d{4,8})", re.I),
    re.compile(r"(?:输入|enter)[^\d]{0,20}(?:代码|code)[^\d]{0,20}(\d{4,8})", re.I),
    re.compile(r"(?:code|代码)\s*[：:]\s*(\d{4,8})", re.I),
]


class TempMailClient:
    """线程安全：创建地址与收信均用本实例自己的 address/jwt，不共享全局邮箱。"""

    def __init__(
        self,
        base_url=DEFAULT_BASE,
        admin_password=DEFAULT_ADMIN,
        domain=DEFAULT_DOMAIN,
        name_prefix=DEFAULT_PREFIX,
        enable_prefix=False,
        timeout=30,
    ):
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.admin_password = admin_password or DEFAULT_ADMIN
        self.domain = domain or DEFAULT_DOMAIN
        self.name_prefix = name_prefix or DEFAULT_PREFIX
        self.enable_prefix = bool(enable_prefix)
        self.timeout = timeout
        self._lock = threading.Lock()
        self.address = None
        self.jwt = None
        self.address_id = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "OutlookRegister/1.0"})

    def _unique_name(self):
        # orx + mmddHHMMSS + 线程低位 + 随机，降低多线程碰撞
        ts = time.strftime("%m%d%H%M%S")
        tid = abs(threading.get_ident()) % 10000
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"{self.name_prefix}{ts}{tid:04d}{rnd}"

    def create_address(self, name=None, domain=None):
        """POST /admin/new_address → 本实例独有 address + jwt。"""
        name = name or self._unique_name()
        domain = domain or self.domain
        url = f"{self.base_url}/admin/new_address"
        headers = {
            "Content-Type": "application/json",
            "x-admin-auth": self.admin_password,
        }
        payload = {
            "enablePrefix": self.enable_prefix,
            "name": name,
            "domain": domain,
        }
        resp = self._session.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        with self._lock:
            self.address = data.get("address") or f"{name}@{domain}"
            self.jwt = data.get("jwt")
            self.address_id = data.get("address_id")
        if not self.jwt:
            raise RuntimeError(f"temp_mail create missing jwt: {data}")
        return self.address, self.jwt

    def list_mails(self, limit=20, offset=0):
        """仅用本实例 jwt 拉信，不会读到其它任务邮箱。"""
        if not self.jwt:
            raise RuntimeError("temp_mail: create_address first")
        url = f"{self.base_url}/api/mails"
        headers = {"Authorization": f"Bearer {self.jwt}"}
        resp = self._session.get(
            url,
            params={"limit": limit, "offset": offset},
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # API 可能用 results 或 data
        if isinstance(data, dict):
            return data.get("results") or data.get("data") or []
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def extract_code_from_text(text, exclude_substrings=None):
        """从邮件正文解析验证码。exclude_substrings：排除邮箱地址等中的数字片段。"""
        if not text:
            return None
        text = str(text)
        exclude = [str(x) for x in (exclude_substrings or []) if x]

        def _ok(code):
            if not code or not code.isdigit():
                return False
            # 勿把临时邮箱本地名里的连续数字当成验证码（日志曾误提 072468）
            for ex in exclude:
                if code in ex.replace("@", ""):
                    return False
            return True

        for rx in _LABELED_CODE_RES:
            m = rx.search(text)
            if m and _ok(m.group(1)):
                return m.group(1)
        # 兜底：独立 6 位（再 4-8 位），仍排除邮箱数字
        for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text):
            if _ok(m.group(1)):
                return m.group(1)
        for m in re.finditer(r"(?<!\d)(\d{4,8})(?!\d)", text):
            if _ok(m.group(1)):
                return m.group(1)
        return None

    def _mail_blob(self, mail):
        if not isinstance(mail, dict):
            return str(mail)
        parts = []
        for k in (
            "subject", "text", "content", "raw", "html", "message",
            "source", "intro", "body", "preview",
        ):
            v = mail.get(k)
            if v:
                parts.append(str(v))
        # 嵌套
        for k in ("mail", "data", "payload"):
            v = mail.get(k)
            if isinstance(v, dict):
                parts.append(self._mail_blob(v))
        return "\n".join(parts)

    def wait_for_code(self, timeout_sec=120, poll_sec=3, after_ts=None, log=None):
        """轮询本邮箱直到解析出验证码。after_ts: 只认该时间之后的信（unix）。"""
        deadline = time.time() + timeout_sec
        seen = set()
        while time.time() < deadline:
            try:
                mails = self.list_mails(limit=15, offset=0)
            except Exception as exc:
                if log:
                    log("temp_mail", f"list_mails 失败: {exc}", "WARN")
                time.sleep(poll_sec)
                continue
            for mail in mails or []:
                mid = None
                if isinstance(mail, dict):
                    mid = mail.get("id") or mail.get("mail_id") or mail.get("message_id")
                    # 时间过滤（字段名因版本而异）
                    if after_ts:
                        for tk in ("created_at", "createdAt", "time", "date", "timestamp"):
                            tv = mail.get(tk)
                            if tv is None:
                                continue
                            try:
                                if isinstance(tv, (int, float)):
                                    ts = float(tv)
                                    if ts > 1e12:
                                        ts /= 1000.0
                                else:
                                    # 跳过无法解析的字符串时间，不因格式误杀
                                    ts = None
                                if ts is not None and ts + 2 < after_ts:
                                    continue
                            except Exception:
                                pass
                            break
                key = mid if mid is not None else id(mail)
                if key in seen:
                    continue
                seen.add(key)
                blob = self._mail_blob(mail)
                code = self.extract_code_from_text(
                    blob,
                    exclude_substrings=[self.address, (self.address or "").split("@")[0]],
                )
                if code:
                    if log:
                        log("temp_mail", f"解析到验证码 code={code} addr={self.address}", "OK")
                    return code
            time.sleep(poll_sec)
        if log:
            log("temp_mail", f"等待验证码超时 addr={self.address}", "FAIL")
        return None


def client_from_config(cfg):
    """从 config['temp_mail'] 构建客户端。未配置时 base/admin/domain 为空。"""
    cfg = cfg or {}
    return TempMailClient(
        base_url=(cfg.get("base_url") or "").strip(),
        admin_password=(cfg.get("admin_password") or "").strip(),
        domain=(cfg.get("domain") or "").strip(),
        name_prefix=(cfg.get("name_prefix") or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX,
        enable_prefix=bool(cfg.get("enable_prefix", False)),
        timeout=int(cfg.get("timeout", 30)),
    )


if __name__ == "__main__":
    print(smoke_test())
