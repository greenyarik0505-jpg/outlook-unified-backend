"""
Outlook OAuth2 Token 批量获取工具

从 not_oauth2.txt 读取已注册但未授权的账号（每行：邮箱----密码），
逐个完成 OAuth2 授权，将 refresh_token 写入 oauth2.txt。
"""
import os
import time
import json
import random
from urllib.parse import quote, parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from patchright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REDIRECT_URI = "https://localhost"
SCOPE = "https://graph.microsoft.com/.default offline_access"
AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

# 验证邮箱页 → 改走密码登录
USE_PASSWORD_TEXTS = (
    '使用密码',
    '使用密码登录',
    'Use your password',
    'Use password instead',
    'Sign in with a password',
    'Password',
)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'concurrent': 1,
        'input_file': 'not_oauth2.txt',
        'output_file': 'oauth2.txt',
        'proxy': {'mode': 'single', 'type': 'http', 'host': '127.0.0.1', 'single_port': 7890}
    }


def load_accounts(path):
    if not os.path.exists(path):
        print(f"[Error] 未找到 {path}")
        return []
    accounts = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '----' in line:
                parts = line.split('----')
                if len(parts) >= 2:
                    accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts


def _extract_code_from_url(url):
    if not url or 'code=' not in url:
        return None
    # 回调多为 https://localhost/?code=...
    if 'localhost' not in url and '127.0.0.1' not in url:
        return None
    try:
        parsed = urlparse(url)
        code = parse_qs(parsed.query).get('code', [None])[0]
        if code:
            return code
        # 少数情况 code 在 fragment
        if parsed.fragment and 'code=' in parsed.fragment:
            return parse_qs(parsed.fragment).get('code', [None])[0]
    except Exception:
        pass
    return None


def _log(tag, t0, msg):
    print(f"{tag} {time.strftime('%H:%M:%S')} | +{time.time()-t0:.0f}s {msg}")


def _fail(tag, t0, reason, detail=""):
    """统一失败日志：必须带可读原因，便于排查。"""
    reason = (reason or "未知错误").strip()
    detail = (detail or "").strip()
    if detail:
        _log(tag, t0, f"[FAIL] 原因={reason} | {detail}")
    else:
        _log(tag, t0, f"[FAIL] 原因={reason}")
    return reason


def _compact_exc(exc):
    """Playwright 异常常带多行 Call log；日志只保留首行摘要。"""
    if exc is None:
        return ""
    text = str(exc).replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return type(exc).__name__
    kept = []
    for ln in lines:
        if ln.startswith("Call log:") or ln.startswith("- waiting") or ln.startswith("- attempting") \
                or ln.startswith("- element is") or ln.startswith("- scrolling") or ln.startswith("- done scrolling") \
                or ln.startswith("- retrying") or ln.startswith("- locator resolved") \
                or ln.startswith("2 ×") or ln.startswith("5 ×") or ln.startswith("waiting for"):
            continue
        # 丢弃超长 HTML 片段行
        if ln.startswith("<") and len(ln) > 80:
            continue
        kept.append(ln)
        if len(kept) >= 2:
            break
    summary = " | ".join(kept) if kept else lines[0]
    if len(summary) > 220:
        summary = summary[:217] + "..."
    return summary


class ProxyPicker:
    def __init__(self, cfg):
        self.type = cfg.get('type', 'http')
        self.host = cfg.get('host', '127.0.0.1')
        self.max_per = cfg.get('max_per_proxy', 20)
        mode = cfg.get('mode', 'single')
        if mode == 'single':
            self.ports = [cfg.get('single_port', 7890)]
        else:
            self.ports = list(range(cfg.get('port_start', 24000), cfg.get('port_end', 24100) + 1))
        self.usage = {}

    def pick(self):
        available = [p for p in self.ports if self.usage.get(p, 0) < self.max_per]
        if not available:
            available = list(self.ports)
            for p in available:
                self.usage[p] = 0
        port = random.choice(available)
        self.usage[port] = self.usage.get(port, 0) + 1
        return f"{self.type}://{self.host}:{port}"


def _visible(locator, timeout=800):
    try:
        return locator.count() > 0 and locator.first.is_visible(timeout=timeout)
    except Exception:
        return False


def _is_usable_password_input(locator):
    """排除 moveOffScreen / aria-hidden 等浏览器预填假密码框（如 #i0118）。"""
    try:
        if locator.count() <= 0:
            return False
        el = locator.first
        if not el.is_visible(timeout=600):
            return False
        # 隐藏预填框特征：class 含 moveOffScreen / aria-hidden / 移出视口
        meta = el.evaluate(
            """(el) => ({
                hidden: !!(el.hidden || el.getAttribute('aria-hidden') === 'true'),
                tabIndex: el.tabIndex,
                cls: (el.className && el.className.toString) ? el.className.toString() : '',
                off: (el.classList && el.classList.contains('moveOffScreen')) || false,
                w: el.offsetWidth || 0,
                h: el.offsetHeight || 0,
            })"""
        ) or {}
        if meta.get("hidden") or meta.get("off"):
            return False
        cls = (meta.get("cls") or "").lower()
        if "moveoffscreen" in cls or "move-off-screen" in cls:
            return False
        if (meta.get("w") or 0) < 4 or (meta.get("h") or 0) < 4:
            return False
        # tabindex=-1 且极小，多半是预填桩
        if meta.get("tabIndex") == -1 and (meta.get("w") or 0) < 20:
            return False
        return True
    except Exception:
        return False


def _click_use_password_if_present(page, tag, t0):
    """
    「验证你的电子邮件」等页：优先点「使用密码」走密码登录，而不是辅助邮箱验证。
    HTML: <span role="button" class="fui-Link ...">使用密码</span>
    """
    # 文案 + role=button / link / 普通文本
    for text in USE_PASSWORD_TEXTS:
        selectors = [
            page.get_by_role("button", name=text),
            page.get_by_role("link", name=text),
            page.locator(f'span[role="button"]:has-text("{text}")'),
            page.locator(f'a:has-text("{text}")'),
            page.get_by_text(text, exact=True),
            page.get_by_text(text),
        ]
        for loc in selectors:
            try:
                if not _visible(loc, timeout=400):
                    continue
                loc.first.click(timeout=4000)
                page.wait_for_timeout(1200)
                _log(tag, t0, f"已点击「{text}」（改走密码登录）")
                return True
            except Exception:
                continue
    return False


def _fill_email(page, email, tag, t0):
    """邮箱页：#i0116 或 Fluent 输入框。"""
    for other_text in ('使用其他帐户', 'Use another account'):
        try:
            btn = page.get_by_text(other_text)
            if _visible(btn, timeout=500):
                btn.first.click(timeout=3000)
                page.wait_for_timeout(1500)
                _log(tag, t0, f"已点击「{other_text}」")
                break
        except Exception:
            pass

    # 经典 #i0116
    try:
        if _visible(page.locator("#i0116"), timeout=2000):
            page.eval_on_selector(
                "#i0116",
                """(el, value) => {
                    el.focus();
                    el.setAttribute('autocomplete', 'off');
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                email,
            )
            page.wait_for_timeout(300)
            try:
                page.locator("#idSIButton9").click(timeout=4000)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            _log(tag, t0, "邮箱已提交 (#i0116)")
            return True
    except Exception:
        pass

    # Fluent / 通用 email 框
    for sel in (
        'input[type="email"]',
        'input[name="loginfmt"]',
        'input[autocomplete="username"]',
    ):
        try:
            loc = page.locator(sel)
            if not _visible(loc, timeout=800):
                continue
            loc.first.fill(email, timeout=5000)
            page.wait_for_timeout(300)
            try:
                page.locator('[data-testid="primaryButton"]').click(timeout=4000)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            _log(tag, t0, f"邮箱已提交 ({sel})")
            return True
        except Exception:
            continue
    return False


def _find_password_input(page):
    """找真正可交互的密码框；跳过 #i0118 moveOffScreen 预填桩。"""
    # Fluent 优先
    for sel in ('#passwordEntry', 'input[type="password"]:not(.moveOffScreen)', 'input[name="passwd"]'):
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(min(n, 4)):
                item = loc.nth(i)
                if _is_usable_password_input(item):
                    return item
        except Exception:
            continue
    # 经典框：仅当不是预填桩时才用 #i0118
    try:
        loc = page.locator("#i0118")
        if _is_usable_password_input(loc):
            return loc.first
    except Exception:
        pass
    for name in ("密码", "Password"):
        try:
            loc = page.get_by_role("textbox", name=name)
            if _is_usable_password_input(loc):
                return loc.first
        except Exception:
            continue
    return None


def _fill_password_and_submit(page, password, tag, t0):
    """
    密码页：#passwordEntry（Fluent）或可见的 #i0118。
    提交：data-testid=primaryButton「下一步」或 Enter。
    """
    pwd = _find_password_input(page)
    if pwd is None:
        return False

    try:
        # 预填桩会被 footer 挡住 click；优先 fill，失败再 force click
        try:
            pwd.fill(password, timeout=8000)
        except Exception:
            pwd.click(timeout=3000, force=True)
            page.wait_for_timeout(150)
            pwd.fill(password, timeout=8000)
        page.wait_for_timeout(300)
    except Exception as e:
        _log(tag, t0, f"填密码失败: {_compact_exc(e)}")
        return False

    # 优先 primaryButton「下一步」
    submitted = False
    for clicker in (
        lambda: page.locator('[data-testid="primaryButton"]').first.click(timeout=4000),
        lambda: page.get_by_role("button", name="下一步").first.click(timeout=4000),
        lambda: page.get_by_role("button", name="Next").first.click(timeout=4000),
        lambda: page.locator("#idSIButton9").click(timeout=4000),
        lambda: page.keyboard.press("Enter"),
    ):
        try:
            clicker()
            submitted = True
            break
        except Exception:
            continue
    if not submitted:
        return False
    page.wait_for_timeout(1500)
    _log(tag, t0, "密码已提交")
    return True


def _click_consent_if_present(page, tag, t0):
    for sel in (
        '[data-testid="appConsentPrimaryButton"]',
        '[data-testid="primaryButton"]',
    ):
        try:
            loc = page.locator(sel)
            if not _visible(loc, timeout=600):
                continue
            # 同意页文案常见「接受」「是」；避免在密码页误点 primary
            text_blob = ""
            try:
                text_blob = (page.inner_text("body", timeout=1500) or "")[:800]
            except Exception:
                pass
            if sel == '[data-testid="primaryButton"]':
                if not any(k in text_blob for k in ('权限', '同意', 'consent', 'Accept', '接受', '应用')):
                    continue
            loc.first.click(timeout=5000)
            _log(tag, t0, f"已点击授权同意 ({sel})")
            page.wait_for_timeout(800)
            return True
        except Exception:
            continue
    for name in ('接受', '是', 'Accept', 'Yes', '允许', 'Allow'):
        try:
            btn = page.get_by_role("button", name=name)
            if _visible(btn, timeout=400):
                btn.first.click(timeout=4000)
                _log(tag, t0, f"已点击授权「{name}」")
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


def _click_kmsi_no_if_present(page, tag, t0):
    """保持登录状态？→ 点「否」"""
    try:
        loc = page.locator('[data-testid="secondaryButton"]')
        if _visible(loc, timeout=500):
            text_blob = ""
            try:
                text_blob = (page.inner_text("body", timeout=1000) or "")[:500]
            except Exception:
                pass
            if any(k in text_blob for k in ('保持登录', '保持登录状态', 'Stay signed in', 'KMSI')):
                loc.first.click(timeout=3000)
                _log(tag, t0, "已点击保持登录「否」")
                page.wait_for_timeout(600)
                return True
    except Exception:
        pass
    for name in ('否', 'No'):
        try:
            btn = page.get_by_role("button", name=name)
            if _visible(btn, timeout=300):
                text_blob = ""
                try:
                    text_blob = (page.inner_text("body", timeout=800) or "")[:400]
                except Exception:
                    pass
                if any(k in text_blob for k in ('保持登录', 'Stay signed in')):
                    btn.first.click(timeout=3000)
                    _log(tag, t0, f"已点击「{name}」")
                    page.wait_for_timeout(600)
                    return True
        except Exception:
            continue
    return False


def _page_has_password(page):
    return _find_password_input(page) is not None


def _page_looks_like_proof_email(page):
    """验证你的电子邮件（需确认辅助邮箱 / 发送验证码）。"""
    try:
        blob = (page.inner_text("body", timeout=1200) or "")
    except Exception:
        return False
    keys = (
        '验证你的电子邮件',
        'Verify your email',
        '发送验证码',
        'Send code',
        '已收到代码',
        'proof-confirmation',
    )
    if any(k in blob for k in keys):
        return True
    if _visible(page.locator("#proof-confirmation-email-input"), timeout=300):
        return True
    return False


def _page_title_text(page):
    for sel in ('[data-testid="title"]', "h1", '[role="heading"]'):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                t = (loc.first.inner_text(timeout=600) or "").strip()
                if t:
                    return t[:120]
        except Exception:
            continue
    return ""


def _page_is_account_locked(page):
    """
    密码后可能出现「帐户已锁定」→ 直接失败，不继续。
    <h1 data-testid="title">帐户已锁定</h1>
    """
    try:
        title = _page_title_text(page)
        if any(k in title for k in ('帐户已锁定', '账户已锁定', 'Account locked')):
            return True
        if 'locked' in title.lower() and 'account' in title.lower():
            return True
    except Exception:
        pass
    try:
        blob = (page.inner_text("body", timeout=800) or "")[:800]
        if '帐户已锁定' in blob or '账户已锁定' in blob:
            return True
        if '锁定了你的帐户' in blob or '锁定了您的帐户' in blob:
            return True
    except Exception:
        pass
    return False


def _diagnose_fail_reason(page, email_done, password_done):
    """拿不到 code 时根据当前页面推断失败原因（单行）。"""
    title = ""
    blob = ""
    url = ""
    try:
        url = (page.url or "")[:160]
    except Exception:
        pass
    try:
        title = _page_title_text(page)
    except Exception:
        pass
    try:
        blob = (page.inner_text("body", timeout=1000) or "")[:900]
    except Exception:
        pass

    if _page_is_account_locked(page):
        return "帐户已锁定", title or "检测到帐户已锁定页面"

    checks = (
        (("密码不正确", "password is incorrect", "密码错误", "That password is incorrect"), "密码错误"),
        (("找不到此帐户", "找不到该用户", "We couldn't find", "account doesn't exist", "不存在"), "帐户不存在"),
        (("太多", "too many", "暂时无法", "稍后再试", "try again later"), "请求过于频繁/暂时无法登录"),
        (("人机验证", "验证码", "captcha", "robot", "arkose", "funcaptcha"), "需要人机验证"),
        (("身份验证", "两步", "two-step", "authenticator", "安全代码"), "需要额外身份验证"),
        (("让我们来保护你的帐户", "暂时跳过", "EmailAddress"), "卡在保护帐户页(未能跳过)"),
        (("验证你的电子邮件", "发送验证码", "使用密码"), "卡在验证电子邮件页"),
        (("权限", "同意", "consent"), "卡在授权同意页"),
        (("保持登录", "Stay signed in"), "卡在保持登录页"),
    )
    low_blob = blob.lower()
    for keys, reason in checks:
        for k in keys:
            if k.lower() in low_blob or k in title:
                detail = title or k
                return reason, detail

    if password_done:
        return "登录超时未拿到code", f"密码已提交但未到回调 title={title or '?'} url={url or '?'}"
    if email_done:
        return "登录超时未拿到code", f"邮箱已提交但未完成后续 title={title or '?'} url={url or '?'}"
    return "登录超时未拿到code", f"未能完成登录 title={title or '?'} url={url or '?'}"


def _page_looks_like_protect_account(page):
    """让我们来保护你的帐户（绑辅助邮箱）；可 #iShowSkip 暂时跳过。"""
    if _visible(page.locator("#iShowSkip"), timeout=300):
        return True
    if _visible(page.locator("#EmailAddress"), timeout=300):
        return True
    try:
        blob = (page.inner_text("body", timeout=800) or "")[:800]
    except Exception:
        return False
    return any(
        k in blob
        for k in (
            '让我们来保护你的帐户',
            '让我们来保护你的账户',
            '保护你的帐户',
            '暂时跳过',
            'Help us protect your account',
            'Add a security info',
        )
    )


def _click_protect_skip_if_present(page, tag, t0):
    """
    密码后的保护帐户页：点「暂时跳过(N 天后必须输入)」。
    <a id="iShowSkip" ...>暂时跳过(7 天后必须输入)</a>
    """
    try:
        skip = page.locator("#iShowSkip")
        if _visible(skip, timeout=800):
            skip.first.click(timeout=4000)
            page.wait_for_timeout(1200)
            _log(tag, t0, "已点击 #iShowSkip 暂时跳过（保护帐户）")
            return True
    except Exception as e:
        _log(tag, t0, f"点击 #iShowSkip 失败: {_compact_exc(e)}")
    # 文案兜底（天数可能是 6/7）
    for text in (
        '暂时跳过',
        'Skip for now',
        '暂时跳过(7 天后必须输入)',
        '暂时跳过(6 天后必须输入)',
    ):
        try:
            loc = page.get_by_text(text)
            if _visible(loc, timeout=400):
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                _log(tag, t0, f"已点击「{text}」（保护帐户跳过）")
                return True
        except Exception:
            continue
    return False


def _try_capture_code(page, captured_code):
    """多路取 code；一旦有立刻返回。"""
    if captured_code[0]:
        return captured_code[0]
    candidates = []
    try:
        candidates.append(page.url)
    except Exception:
        pass
    try:
        candidates.append(page.evaluate("window.location.href"))
    except Exception:
        pass
    for url in candidates:
        code = _extract_code_from_url(url)
        if code:
            captured_code[0] = code
            return code
    return None


def process_single_account(email, password, proxy_url, idx, total):
    """处理单个账号的 OAuth2 授权，返回 (email, success, token_or_error)"""
    t0 = time.time()
    tag = f"[{idx}/{total}]"
    print(f"{tag} {time.strftime('%H:%M:%S')} | 开始: {email}")

    p = sync_playwright().start()
    browser = None
    page = None
    try:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--lang=zh-CN",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-features=WebAuthenticationConditionalUI",
                "--disable-save-password-bubble",
                "--disable-password-manager-reauthentication",
            ],
            proxy={"server": proxy_url},
        )
        page = browser.new_page()
    except Exception as e:
        reason = _fail(tag, t0, "浏览器启动失败", _compact_exc(e))
        try:
            p.stop()
        except Exception:
            pass
        return email, False, reason

    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
        'sso_reload': 'true',
    }
    auth_url = f"{AUTHORIZE_URL}?{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"
    captured_code = [None]

    def on_request(request):
        code = _extract_code_from_url(request.url)
        if code and not captured_code[0]:
            captured_code[0] = code

    def on_frame_navigated(frame):
        code = _extract_code_from_url(frame.url)
        if code and not captured_code[0]:
            captured_code[0] = code

    page.on('request', on_request)
    page.on('framenavigated', on_frame_navigated)

    try:
        page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
        _log(tag, t0, "进入 auth 页面")

        email_done = False
        password_done = False
        # 总登录+授权窗口（秒）；拿到 code 立即跳出
        deadline = time.time() + 120
        last_action = 0.0

        while time.time() < deadline:
            # ★ 一旦 URL/监听器拿到 code，立刻结束登录循环（不再干等 2 分钟）
            code = _try_capture_code(page, captured_code)
            if code:
                _log(tag, t0, "捕获到 code（立即结束登录阶段）")
                break

            # 密码后可能出现「帐户已锁定」→ 直接失败
            if _page_is_account_locked(page):
                title = _page_title_text(page) or "帐户已锁定"
                reason = _fail(tag, t0, "帐户已锁定", f"页面标题={title}")
                return email, False, reason

            now = time.time()
            # 节流：同一动作不要刷太快
            do_act = (now - last_action) >= 0.6

            if do_act:
                # 1) 验证邮箱页 → 点「使用密码」
                if _page_looks_like_proof_email(page) or _visible(
                    page.locator('span[role="button"]:has-text("使用密码")'), timeout=200
                ):
                    if _click_use_password_if_present(page, tag, t0):
                        last_action = time.time()
                        password_done = False  # 即将出现密码页
                        continue

                # 2) 密码页
                if _page_has_password(page):
                    if _fill_password_and_submit(page, password, tag, t0):
                        password_done = True
                        last_action = time.time()
                        continue

                # 3) 邮箱页
                if not email_done:
                    if _fill_email(page, email, tag, t0):
                        email_done = True
                        last_action = time.time()
                        continue
                    if _click_use_password_if_present(page, tag, t0):
                        last_action = time.time()
                        continue

                # 4) 密码后：保护帐户 → #iShowSkip 暂时跳过 → 同意 / code
                if password_done or _page_looks_like_protect_account(page):
                    if _click_protect_skip_if_present(page, tag, t0):
                        last_action = time.time()
                        continue

                # 5) 密码后回到验证页：再点使用密码
                if password_done and _click_use_password_if_present(page, tag, t0):
                    password_done = False
                    last_action = time.time()
                    continue

                # 6) 保持登录 → 否
                if _click_kmsi_no_if_present(page, tag, t0):
                    last_action = time.time()
                    continue

                # 7) 授权同意
                if _click_consent_if_present(page, tag, t0):
                    last_action = time.time()
                    continue

            page.wait_for_timeout(200)

        code = _try_capture_code(page, captured_code)
        if not code:
            # 结束前再判一次锁定
            if _page_is_account_locked(page):
                title = _page_title_text(page) or "帐户已锁定"
                reason = _fail(tag, t0, "帐户已锁定", f"页面标题={title}")
                return email, False, reason
            reason, detail = _diagnose_fail_reason(page, email_done, password_done)
            reason = _fail(tag, t0, reason, detail)
            return email, False, reason

        _log(tag, t0, "开始换 token")
        proxies = None
        if proxy_url:
            proxies = {'http': proxy_url, 'https': proxy_url}
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    'client_id': CLIENT_ID,
                    'code': code,
                    'redirect_uri': REDIRECT_URI,
                    'grant_type': 'authorization_code',
                    'scope': SCOPE,
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                proxies=proxies,
                timeout=30,
            )
            data = response.json()
        except Exception as e:
            reason = _fail(tag, t0, "token网络请求失败", _compact_exc(e))
            return email, False, reason

        if 'refresh_token' not in data:
            err = data.get('error') or data.get('error_description') or str(data)[:120]
            reason = _fail(tag, t0, "token交换失败", f"error={err}")
            return email, False, reason

        refresh_token = data['refresh_token']
        _log(tag, t0, "[OK] token获取成功!")
        return email, True, refresh_token

    except Exception as e:
        reason = _fail(tag, t0, "运行异常", _compact_exc(e))
        return email, False, reason

    finally:
        try:
            if page is not None:
                page.remove_listener('request', on_request)
                page.remove_listener('framenavigated', on_frame_navigated)
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass


def main():
    cfg = load_config()
    input_path = os.path.join(BASE_DIR, cfg.get('input_file', 'not_oauth2.txt'))
    output_path = os.path.join(BASE_DIR, cfg.get('output_file', 'oauth2.txt'))
    accounts = load_accounts(input_path)

    if not accounts:
        print("没有待处理的账号。请在 not_oauth2.txt 中添加（格式：邮箱----密码）")
        return

    total = len(accounts)
    concurrent = min(cfg.get('concurrent', 1), total)
    picker = ProxyPicker(cfg['proxy'])

    print(f"共 {total} 个账号，{concurrent} 并发")
    print()

    succeeded = []
    failed = []
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=concurrent) as executor:
        futures = {}
        for i, (email, password) in enumerate(accounts, 1):
            proxy = picker.pick()
            future = executor.submit(process_single_account, email, password, proxy, i, total)
            futures[future] = (email, i)

        for future in as_completed(futures):
            email, success, result = future.result()
            if success:
                succeeded.append((email, result))
                with open(output_path, 'a', encoding='utf-8') as f:
                    pwd = next((p for e, p in accounts if e == email), '')
                    f.write(f"{email}----{pwd}----{CLIENT_ID}----{result}\n")
                print(f"[结果] OK  {email}")
            else:
                failed.append((email, result))
                print(f"[结果] FAIL {email} | 原因={result}")
            done = len(succeeded) + len(failed)
            print(
                f"[进度] {done}/{total} 成功{len(succeeded)} 失败{len(failed)} | "
                f"耗时 {((time.time()-t_start)/60):.1f}min"
            )

    print(f"\n=== 完成 ===")
    print(f"成功: {len(succeeded)}/{total}")
    print(f"失败: {len(failed)}/{total}")
    print(f"耗时: {(time.time()-t_start)/60:.1f}min")
    if failed:
        print("--- 失败明细 ---")
        for email, reason in failed:
            print(f"  {email} | 原因={reason}")
        # 按原因归类计数
        from collections import Counter
        c = Counter((r or "未知") for _, r in failed)
        print("--- 失败原因统计 ---")
        for reason, n in c.most_common():
            print(f"  {n}x {reason}")


if __name__ == "__main__":
    main()
