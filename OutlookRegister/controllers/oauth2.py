import time
from urllib.parse import parse_qs, quote, urlparse

import requests

# === OAuth2 常量 ===
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REDIRECT_URI = "https://localhost"
SCOPE = "https://graph.microsoft.com/.default offline_access"
AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

CONSENT_SELECTOR = '[data-testid="appConsentPrimaryButton"]'
EMAIL_SELECTOR = "#i0116"
EMAIL_NEXT_SELECTOR = "#idSIButton9"
PRIMARY_SELECTOR = '[data-testid="primaryButton"],input[data-testid="primaryButton"],input[type="submit"]'
# 个人帐户 HRD 选择（勿点工作/学校帐户）
MSA_TILE_SELECTOR = "#msaTile"
MSA_TILE_TITLE_SELECTOR = "#msaTileTitle"
PASSWORD_BYPASS_TEXTS = [
    "使用密码",
    "使用密码登录",
    "Use password instead",
    "Use your password",
    "Sign in with a password",
]
PASSWORD_WRONG_TEXTS = [
    "此密码不是你的 Microsoft 帐户的正确密码",
    "This password is incorrect",
    "你的帐户或密码不正确",
    "帐户或密码不正确",
    "账户或密码不正确",
    "Your account or password is incorrect",
    "incorrect account or password",
]
PASSWORD_BLOCKED_TEXTS = [
    "密码登录不可用",
    "请尝试其他方法",
    "Password login is not available",
    "Try another way",
    "Try a different way",
    "Sign-in method isn't available",
]
ACCOUNT_TYPE_HINT_TEXTS = [
    "哪种类型的帐户",
    "哪种类型的账户",
    "which type of account",
    "Work or school account",
    "工作或学校帐户",
    "工作或学校账户",
    "个人帐户",
    "个人账户",
    "Personal account",
]
AUTH_NAV_TIMEOUT_MS = 45000
AUTH_ENTRY_TIMEOUT_MS = 45000


def build_auth_url(prefer_sso=True):
    """构造授权 URL。

    prefer_sso=True（默认，COOKIE 路径）：
      - 不加 sso_reload，尽量用注册会话静默登录直接到 consent
    prefer_sso=False（NEW 冷启动）：
      - 可加 prompt=login 强制账密（一般仍不建议；默认也不加）
    """
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': SCOPE,
    }
    # 历史问题：sso_reload=true 会强制打断 cookie SSO，COOKIE 路径几乎必掉 #i0116
    if not prefer_sso:
        params['sso_reload'] = 'true'
    return f"{AUTHORIZE_URL}?{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"


def _extract_code_from_url(url):
    if 'localhost' not in url or 'code=' not in url:
        return None
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    return query_params.get('code', [None])[0]


def _wait_for_code_capture(page, captured_code, timeout_ms=180000, poll_ms=250):
    if captured_code[0]:
        return captured_code[0]
    code = _extract_code_from_url(page.url)
    if code:
        captured_code[0] = code
        return code
    try:
        js_url = page.evaluate('window.location.href')
        code = _extract_code_from_url(js_url)
        if code:
            captured_code[0] = code
            return code
    except Exception:
        pass
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if captured_code[0]:
            return captured_code[0]
        code = _extract_code_from_url(page.url)
        if code:
            captured_code[0] = code
            return code
        try:
            js_url = page.evaluate('window.location.href')
            code = _extract_code_from_url(js_url)
            if code:
                captured_code[0] = code
                return code
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    return None


def _compact_exc(exc, max_len=180):
    """压缩 Playwright 异常，去掉多行 Call log，保持单行日志。"""
    text = str(exc) if exc is not None else ""
    if not text:
        return ""
    # 只保留第一行语义（如 Locator.click: Timeout 5000ms exceeded.）
    first = text.strip().splitlines()[0].strip()
    # 去掉 Call log 及之后整段
    for marker in ("Call log:", "\nCall log"):
        idx = text.find(marker)
        if idx >= 0:
            first = text[:idx].strip().splitlines()[0].strip()
            break
    if len(first) > max_len:
        first = first[: max_len - 3] + "..."
    return first


def _wait_for_auth_state_or_code(page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, poll_ms=500, ignore_states=None):
    ignore_states = set(ignore_states or ())
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if captured_code and _wait_for_code_capture(page, captured_code, timeout_ms=0):
            return 'code'
        state = _current_auth_entry_state(page)
        if state != 'unknown' and state not in ignore_states:
            return state
        page.wait_for_timeout(poll_ms)
    if captured_code and _wait_for_code_capture(page, captured_code, timeout_ms=0):
        return 'code'
    state = _current_auth_entry_state(page)
    if state != 'unknown' and state not in ignore_states:
        return state
    return 'unknown'


def _locator_visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _text_exists(page, text):
    try:
        return page.get_by_text(text).count() > 0
    except Exception:
        return False


def _password_input(page):
    """密码框：中英 accessible name + 常见 id。"""
    for name in ("密码", "Password", "password"):
        try:
            loc = page.get_by_role("textbox", name=name)
            if _locator_visible(loc):
                return loc
        except Exception:
            pass
    for sel in ("#passwordEntry", "#i0118", 'input[type="password"]'):
        loc = page.locator(sel)
        if _locator_visible(loc):
            return loc
    return page.get_by_role("textbox", name="密码")


def _is_account_type_page(page):
    """个人/工作帐户选择页（HRD splitter）。"""
    if _locator_visible(page.locator(MSA_TILE_SELECTOR)):
        return True
    if _locator_visible(page.locator(MSA_TILE_TITLE_SELECTOR)):
        return True
    # 文案兜底：同时出现个人 + 工作/学校 更稳
    has_personal = (
        _text_exists(page, "个人帐户")
        or _text_exists(page, "个人账户")
        or _text_exists(page, "Personal account")
    )
    has_work = (
        _text_exists(page, "工作或学校帐户")
        or _text_exists(page, "工作或学校账户")
        or _text_exists(page, "Work or school account")
    )
    if has_personal and has_work:
        return True
    for t in ACCOUNT_TYPE_HINT_TEXTS:
        if "哪种类型" in t or "which type" in t.lower():
            if _text_exists(page, t):
                return True
    return False


def _is_protect_account_page(page):
    """「让我们来保护你的帐户」备用邮箱页。"""
    try:
        from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
        return is_protect_account_page(page) or is_ott_code_page(page)
    except Exception:
        if _locator_visible(page.locator("#EmailAddress")):
            return True
        if _locator_visible(page.locator("#iOttText")):
            return True
        return _text_exists(page, "保护你的帐户") or _text_exists(page, "保护您的帐户")


def _is_proof_verify_page(page):
    """冷登录：验证已绑定辅助邮箱 / 6 格验证码（不含仅 KMSI）。"""
    try:
        from controllers.recovery_bind import is_proof_confirm_page, is_code_entry_page
        return is_proof_confirm_page(page) or is_code_entry_page(page)
    except Exception:
        if _locator_visible(page.locator("#proof-confirmation-email-input")):
            return True
        if _locator_visible(page.locator("#codeEntry-0")):
            return True
        return _text_exists(page, "验证你的电子邮件") or _text_exists(page, "输入你的代码")


def _is_kmsi_only_page(page):
    try:
        from controllers.recovery_bind import is_kmsi_page, is_proof_confirm_page, is_code_entry_page
        return is_kmsi_page(page) and not is_proof_confirm_page(page) and not is_code_entry_page(page)
    except Exception:
        return _text_exists(page, "保持登录") or _text_exists(page, "Stay signed in")


def _current_auth_entry_state(page):
    """登录页状态机（可见 DOM 锚点，固定优先级）。

    consent > account_type > protect_account > proof_verify > kmsi > login_email > login_password > unknown
    """
    if _locator_visible(page.locator(CONSENT_SELECTOR)):
        return 'consent'
    if _is_account_type_page(page):
        return 'account_type'
    if _is_protect_account_page(page):
        return 'protect_account'
    if _is_proof_verify_page(page):
        return 'proof_verify'
    if _is_kmsi_only_page(page):
        return 'kmsi'
    if _locator_visible(page.locator(EMAIL_SELECTOR)):
        return 'login_email'
    if _locator_visible(_password_input(page)):
        return 'login_password'
    return 'unknown'


def _handle_kmsi(page, log):
    """保持登录状态？→ 点「否」secondaryButton。"""
    try:
        from controllers.recovery_bind import is_kmsi_page, _click_kmsi_no
        if is_kmsi_page(page):
            if _click_kmsi_no(page, log=log):
                log('kmsi', '已点保持登录「否」', 'OK')
            else:
                log('kmsi', '点击「否」失败', 'WARN')
            page.wait_for_timeout(800)
    except Exception as exc:
        log('kmsi', f'处理异常: {_compact_exc(exc)}', 'WARN')
    return _current_auth_entry_state(page)


def _handle_proof_verify(page, log, temp_mail_cfg=None, recovery_session=None, failure_hook=None):
    """OAuth 冷登录：验证已绑定辅助邮箱（发码 → #codeEntry-0..5 自动提交 → KMSI 否）。"""
    # 仅 KMSI 时不需要 jwt
    if _is_kmsi_only_page(page):
        return _handle_kmsi(page, log)

    log('proof_verify', '检测到「验证电子邮件/输入代码」页', 'WARN')
    session = recovery_session
    if not session or not session.get('address') or not session.get('jwt'):
        log('proof_verify', '无注册阶段保存的辅助邮箱 jwt，无法接码', 'FAIL')
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        return _current_auth_entry_state(page)
    try:
        from controllers.recovery_bind import verify_bound_email_on_login
        ok = verify_bound_email_on_login(
            page, session, temp_mail_cfg or {}, log=log,
        )
    except Exception as exc:
        log('proof_verify', f'验证异常: {_compact_exc(exc)}', 'FAIL')
        ok = False
    if ok:
        log('proof_verify', '辅助邮箱验证流程完成', 'OK')
    else:
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        log('proof_verify', '辅助邮箱验证失败', 'FAIL')
    page.wait_for_timeout(500)
    return _current_auth_entry_state(page)


def _handle_protect_account(page, log, temp_mail_cfg=None, failure_hook=None, already_bound=False):
    """OAuth 中的保护帐户页（兜底，非 100% 出现）。

    主路径应在「注册成功 → mail/0 前」完成绑定；此处仅当注册时跳过/失败后才常见。
    already_bound=True：注册阶段已绑成功，优先点跳过离开，避免重复绑定。
    """
    if already_bound:
        log('protect_account', '注册阶段已绑定过，OAuth 侧优先离开此页', 'INFO')
        try:
            if _locator_visible(page.locator('#iShowSkip')):
                page.locator('#iShowSkip').first.click(timeout=4000)
                page.wait_for_timeout(800)
        except Exception:
            pass
        return _current_auth_entry_state(page)

    log('protect_account', 'OAuth 出现保护帐户页（概率事件），尝试绑定', 'WARN')
    cfg = temp_mail_cfg or {}
    ok = False
    if cfg.get('enabled', True):
        try:
            from controllers.recovery_bind import bind_recovery_email
            result = bind_recovery_email(page, cfg, log=log)
            if isinstance(result, tuple):
                ok = bool(result[0])
            else:
                ok = bool(result)
        except Exception as exc:
            log('protect_account', f'绑定异常: {exc}', 'FAIL')
            ok = False
    if ok:
        log('protect_account', 'OAuth 阶段备用邮箱绑定成功', 'OK')
        page.wait_for_timeout(800)
        try:
            if _locator_visible(page.locator('#iShowSkip')):
                page.locator('#iShowSkip').first.click(timeout=2500)
                log('protect_account', '绑定后仍有跳过链，已点击', 'INFO')
        except Exception:
            pass
    else:
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        try:
            if _locator_visible(page.locator('#iShowSkip')):
                page.locator('#iShowSkip').first.click(timeout=4000)
                log('protect_account', 'OAuth 绑定失败，已 #iShowSkip', 'WARN')
                page.wait_for_timeout(800)
        except Exception as exc:
            log('protect_account', f'跳过失败: {exc}', 'WARN')
    page.wait_for_timeout(500)
    st = _current_auth_entry_state(page)
    if ok and st == 'protect_account':
        try:
            if not _locator_visible(page.locator('#EmailAddress')) and not _locator_visible(page.locator('#iOttText')):
                return 'unknown'
        except Exception:
            pass
    return st


def _dump_auth_page(page, log, stage='auth_dump'):
    """失败时记录 URL + 正文摘要，便于对照截图。"""
    try:
        url = page.url or ''
    except Exception:
        url = ''
    body = ''
    try:
        body = (page.locator('body').inner_text(timeout=800) or '')[:240].replace('\n', ' ')
    except Exception:
        body = ''
    state = _current_auth_entry_state(page)
    log(stage, f"state={state} url={url[:180]} body={body!r}", 'WARN')
    return state


def _click_personal_account(page, log=None):
    """点 HRD「个人帐户」#msaTile（禁止点工作/学校）。"""
    clicked = False
    # 1) 标准 msa tile
    try:
        tile = page.locator(MSA_TILE_SELECTOR)
        if _locator_visible(tile):
            tile.first.click(timeout=5000)
            clicked = True
            if log:
                log('account_type', '已点击 #msaTile 个人帐户', 'OK')
    except Exception as exc:
        if log:
            log('account_type', f'#msaTile 点击失败: {exc}', 'WARN')

    # 2) 标题区域
    if not clicked:
        try:
            title = page.locator(MSA_TILE_TITLE_SELECTOR)
            if _locator_visible(title):
                title.first.click(timeout=5000)
                clicked = True
                if log:
                    log('account_type', '已点击 #msaTileTitle', 'OK')
        except Exception:
            pass

    # 3) 文案 role=button / 文本
    if not clicked:
        for text in ("个人帐户", "个人账户", "Personal account"):
            try:
                btn = page.get_by_role("button", name=text)
                if _locator_visible(btn):
                    btn.first.click(timeout=5000)
                    clicked = True
                    if log:
                        log('account_type', f'已点击 button:{text}', 'OK')
                    break
            except Exception:
                pass
            try:
                loc = page.get_by_text(text, exact=False)
                if _locator_visible(loc):
                    # 避免点到「重命名你的个人 Microsoft 帐户」链接：优先含 display 的 tile
                    loc.first.click(timeout=5000)
                    clicked = True
                    if log:
                        log('account_type', f'已点击 text:{text}', 'OK')
                    break
            except Exception:
                pass

    if clicked:
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
    elif log:
        log('account_type', '未找到可点击的个人帐户入口', 'WARN')
    return clicked


def _resolve_account_type(page, log, captured_code=None, max_rounds=3):
    """若在帐户类型页，点击个人帐户并返回新状态。"""
    state = _current_auth_entry_state(page)
    for _ in range(max_rounds):
        if state != 'account_type':
            return state
        log('account_type', '检测到个人/工作帐户选择页，点击个人帐户', 'WARN')
        if not _click_personal_account(page, log):
            _dump_auth_page(page, log, 'account_type_dump')
            return 'account_type'
        try:
            _settle_auth_page(page, log, 'account_type')
        except Exception:
            page.wait_for_timeout(800)
        state = _wait_for_auth_state_or_code(
            page,
            captured_code,
            timeout_ms=15000,
            ignore_states=set(),
        )
        # 点完仍可能短暂 unknown
        if state == 'unknown':
            state = _current_auth_entry_state(page)
    return state


def _wait_for_auth_entry_state(page, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, poll_ms=500, ignore_states=None):
    return _wait_for_auth_state_or_code(page, None, timeout_ms=timeout_ms, poll_ms=poll_ms, ignore_states=ignore_states)


def _settle_auth_page(page, log, stage, timeout_ms=AUTH_NAV_TIMEOUT_MS):
    try:
        page.wait_for_load_state('domcontentloaded', timeout=timeout_ms)
    except Exception as e:
        log(stage, f'等待 domcontentloaded 超时: {e}', 'WARN')
    try:
        page.wait_for_load_state('load', timeout=timeout_ms)
    except Exception as e:
        log(stage, f'等待 load 超时，继续检测入口: {e}', 'WARN')
    page.wait_for_timeout(1200)


def _disable_auth_page_autofill(page, log=None):
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('input').forEach((el) => {
                    try {
                        el.setAttribute('autocomplete', 'off');
                        el.setAttribute('autocapitalize', 'off');
                        el.setAttribute('autocorrect', 'off');
                        el.setAttribute('spellcheck', 'false');
                        el.setAttribute('data-lpignore', 'true');
                    } catch (e) {}
                });
            }"""
        )
        if log:
            log('autofill', '已尝试关闭页面输入框自动填充提示', 'INFO')
    except Exception as exc:
        if log:
            log('autofill', f'关闭页面自动填充提示失败: {exc}', 'WARN')


def _submit_email_fill(page, full_email):
    _disable_auth_page_autofill(page)
    locator = page.locator(EMAIL_SELECTOR).first
    locator.click(timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    locator.fill("")
    page.wait_for_timeout(100)
    locator.fill(full_email, timeout=5000)
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.locator(EMAIL_NEXT_SELECTOR).click(timeout=5000)


def _submit_email_type(page, full_email):
    _disable_auth_page_autofill(page)
    locator = page.locator(EMAIL_SELECTOR).first
    locator.click(timeout=5000)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(100)
    locator.type(full_email, delay=35, timeout=10000)
    page.wait_for_timeout(250)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.locator(EMAIL_NEXT_SELECTOR).click(timeout=5000)


def _submit_email_js_exact(page, full_email):
    page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=10000)
    _disable_auth_page_autofill(page)
    page.eval_on_selector(
        EMAIL_SELECTOR,
        """(el, value) => {
            el.focus();
            el.setAttribute('autocomplete', 'off');
            const nativeInputValueSetter =
                Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        }""",
        full_email,
    )
    page.wait_for_timeout(500)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.locator(EMAIL_NEXT_SELECTOR).click(timeout=5000)


def _submit_email(page, full_email, log):
    page.wait_for_selector(EMAIL_SELECTOR, state="visible", timeout=10000)
    methods = [
        ("fill", _submit_email_fill),
        ("type", _submit_email_type),
        ("js_exact", _submit_email_js_exact),
        ("js_exact_retry", _submit_email_js_exact),
    ]
    success_states = ('login_password', 'consent', 'code', 'account_type', 'protect_account')
    last_error = None
    last_stage = 'unknown'
    for name, method in methods:
        try:
            # 提交过程中可能已跳到帐户类型/密码/保护帐户
            cur = _current_auth_entry_state(page)
            if cur in success_states:
                if cur == 'account_type':
                    cur = _resolve_account_type(page, log)
                log('oauth_email', f"提交前已在阶段={cur}", 'OK')
                return cur
            if page.locator(EMAIL_SELECTOR).count() == 0:
                cur = _current_auth_entry_state(page)
                if cur == 'account_type':
                    cur = _resolve_account_type(page, log)
                return cur
            current = page.eval_on_selector(EMAIL_SELECTOR, "(el) => (el.value || '').trim()")
            log('oauth_email', f"尝试 {name}，提交前值={current!r}", 'INFO')
            method(page, full_email)
            stage = _wait_for_auth_entry_state(page, timeout_ms=12000)
            last_stage = stage
            if stage == 'account_type':
                stage = _resolve_account_type(page, log)
                last_stage = stage
            if stage in ('login_password', 'consent', 'code', 'protect_account'):
                log('oauth_email', f"{name} 成功进入阶段={stage}", 'OK')
                return stage
            still_here = page.locator(EMAIL_SELECTOR).count() > 0 and page.locator(EMAIL_SELECTOR).first.is_visible()
            err = ""
            if still_here:
                err = page.eval_on_selector("#usernameError", "(el) => (el.innerText || '').trim()") if page.locator("#usernameError").count() > 0 else ""
            log('oauth_email', f"{name} 后仍未进入下一阶段 stage={stage} error={err!r}", 'WARN')
        except Exception as exc:
            last_error = exc
            brief = _compact_exc(exc)
            log('oauth_email', f"{name} 失败: {brief}", 'WARN')
            # type 时常见：Next 点击超时但页面已导航到密码/帐户类型/同意页
            stage = _current_auth_entry_state(page)
            if stage == 'account_type':
                stage = _resolve_account_type(page, log)
            last_stage = stage
            if stage in ('login_password', 'consent', 'code', 'protect_account'):
                log('oauth_email', f"{name} 异常后已在阶段={stage}", 'OK')
                return stage
    if last_stage in ('login_password', 'consent', 'code', 'account_type', 'protect_account'):
        if last_stage == 'account_type':
            last_stage = _resolve_account_type(page, log)
        return last_stage
    if last_error:
        raise RuntimeError(f"邮箱提交失败: {_compact_exc(last_error)}")
    raise RuntimeError("邮箱提交后未进入密码页")


def _click_use_password(page):
    for text in PASSWORD_BYPASS_TEXTS:
        try:
            btn = page.get_by_role("button", name=text)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass
        try:
            btn = page.get_by_text(text)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass


def _describe_password_candidates(page):
    parts = []
    for selector in ('#passwordEntry', '#i0118', 'input[type="password"]'):
        try:
            locator = page.locator(selector)
            count = locator.count()
            rows = []
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    visible = item.is_visible()
                except Exception as exc:
                    visible = f"err:{exc.__class__.__name__}"
                try:
                    meta = item.evaluate(
                        """(el) => ({
                            id: el.id || '',
                            name: el.name || '',
                            type: el.getAttribute('type') || '',
                            tabindex: el.getAttribute('tabindex') || '',
                            ariaHidden: el.getAttribute('aria-hidden') || '',
                            readonly: el.hasAttribute('readonly'),
                            disabled: !!el.disabled
                        })"""
                    )
                except Exception:
                    meta = {}
                rows.append(f"{idx}:visible={visible},meta={meta}")
            parts.append(f"{selector} count={count} [{' ; '.join(rows)}]")
        except Exception:
            parts.append(f"{selector} error")
    return " | ".join(parts)


def _password_locator(page, log, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000
    last_snapshot = ""
    while time.time() < deadline:
        _click_use_password(page)
        for selector in ('#passwordEntry', '#i0118', 'input[type="password"]'):
            try:
                locator = page.locator(selector)
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        log('oauth_password', f"使用密码框 {selector}[{idx}]", 'INFO')
                        return item, f"{selector}[{idx}]"
                except Exception:
                    continue
        last_snapshot = _describe_password_candidates(page)
        page.wait_for_timeout(300)
    raise RuntimeError(f"未找到可见密码框：{last_snapshot}")


def _submit_password(page, password, log):
    _click_use_password(page)
    _disable_auth_page_autofill(page)
    log('oauth_password', f"密码候选快照：{_describe_password_candidates(page)}", 'INFO')
    locator, locator_name = _password_locator(page, log=log, timeout_ms=15000)
    locator.evaluate(
        """(el, value) => {
            el.focus();
            el.removeAttribute('readonly');
            el.removeAttribute('aria-hidden');
            el.style.opacity = '1';
            el.style.pointerEvents = 'auto';
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        password,
    )
    page.wait_for_timeout(200)
    try:
        filled_len = locator.evaluate("(el) => (el.value || '').length")
        log('oauth_password', f"{locator_name} 已写入密码，长度={filled_len}", 'INFO')
    except Exception:
        log('oauth_password', f"{locator_name} 已写入密码", 'INFO')
    page.wait_for_timeout(400)
    try:
        page.get_by_test_id("primaryButton").click(timeout=5000)
        log('oauth_password', "点击 data-testid=primaryButton 提交密码", 'INFO')
    except Exception:
        page.keyboard.press("Enter")
        log('oauth_password', "主按钮点击失败，改用 Enter 提交密码", 'WARN')


def _has_invalid_password(page):
    for t in PASSWORD_WRONG_TEXTS:
        if _text_exists(page, t):
            return True
    return False


def _has_password_login_blocked(page):
    for t in PASSWORD_BLOCKED_TEXTS:
        if _text_exists(page, t):
            return True
    return False


def _has_unknown_account(page):
    return (
        _text_exists(page, '找不到使用该用户名的帐户')
        or _text_exists(page, '找不到使用该用户名的账户')
        or _text_exists(page, "We couldn't find an account with that username")
        or _text_exists(page, "That Microsoft account doesn't exist")
        or _locator_visible(page.locator('#usernameError'))
    )


def _dismiss_passkey_setup(page, log=None):
    """密码后可能跳到「正在设置密钥」/ fido create，尝试取消回到同意流。"""
    try:
        url = page.url or ''
    except Exception:
        url = ''
    body_hint = False
    try:
        body_hint = (
            _text_exists(page, '正在设置密钥')
            or _text_exists(page, '安全窗口')
            or _text_exists(page, 'passkey')
            or _text_exists(page, '通行密钥')
            or 'fido/create' in url
        )
    except Exception:
        pass
    if not body_hint and 'fido' not in url:
        return False
    if log:
        log('passkey', f'检测到密钥设置页 url={url[:120]}', 'WARN')
    for text in ('取消', 'Cancel', '以后再说', 'Not now', '暂时跳过', 'Skip'):
        try:
            if _locator_visible(page.get_by_role('button', name=text)):
                page.get_by_role('button', name=text).first.click(timeout=3000)
                page.wait_for_timeout(1000)
                if log:
                    log('passkey', f'已点击 {text}', 'OK')
                return True
        except Exception:
            pass
        try:
            loc = page.locator(f'input[type="button"][value="{text}"]')
            if _locator_visible(loc):
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1000)
                if log:
                    log('passkey', f'已点击 input {text}', 'OK')
                return True
        except Exception:
            pass
    # 最后：若仍在 fido 页，直接跳回我们的 authorize（依赖 cookie）
    try:
        page.goto(build_auth_url(prefer_sso=True), timeout=AUTH_NAV_TIMEOUT_MS, wait_until='domcontentloaded')
        page.wait_for_timeout(1200)
        if log:
            log('passkey', '密钥页无法取消，已回跳 authorize', 'WARN')
        return True
    except Exception:
        return False


def _run_cookie_recovery(page, auth_url, log, entry_timeout_ms=AUTH_ENTRY_TIMEOUT_MS):
    last_state = 'unknown'
    for method_name, action in [
        ('reload', lambda: page.reload(wait_until="domcontentloaded", timeout=AUTH_NAV_TIMEOUT_MS)),
        ('location.reload', lambda: page.evaluate("() => location.reload()")),
        ('goto', lambda: page.goto(auth_url, timeout=AUTH_NAV_TIMEOUT_MS, wait_until="domcontentloaded")),
    ]:
        log('cookie_recovery', f'执行 {method_name}', 'WARN')
        try:
            if method_name == 'location.reload':
                with page.expect_navigation(wait_until="domcontentloaded", timeout=AUTH_NAV_TIMEOUT_MS):
                    action()
            else:
                action()
        except Exception as e:
            log('cookie_recovery', f'{method_name} 失败: {e}', 'WARN')
            continue
        _settle_auth_page(page, log, 'cookie_recovery')
        _disable_auth_page_autofill(page, log)
        state = _wait_for_auth_entry_state(page, timeout_ms=entry_timeout_ms)
        if state == 'account_type':
            state = _resolve_account_type(page, log)
        last_state = state
        log('cookie_recovery', f'{method_name} 后状态={state}', 'INFO')
        if state in ('consent', 'login_password', 'code'):
            return state
        if state == 'login_email':
            continue
        if state == 'account_type':
            # 已尝试点击个人帐户仍停在选择页
            continue
        if method_name == 'goto':
            return state
    return 'login_email' if last_state == 'login_email' else last_state


def _digest_post_email_states(
    page, log, state, captured_code=None, temp_mail_cfg=None,
    recovery_already_bound=False, recovery_session=None, failure_hook=None, rounds=4,
):
    """邮箱提交后可能出现的中间页：帐户类型 / 绑定保护 / 验证辅助邮箱 / 密钥 / KMSI。"""
    for _ in range(rounds):
        if state == 'account_type':
            state = _resolve_account_type(page, log, captured_code=captured_code)
            log('account_type', f'处理后状态={state}', 'INFO')
            continue
        if state == 'protect_account':
            state = _handle_protect_account(
                page, log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                already_bound=recovery_already_bound,
            )
            log('protect_account', f'处理后状态={state}', 'INFO')
            continue
        if state == 'proof_verify':
            state = _handle_proof_verify(
                page, log, temp_mail_cfg=temp_mail_cfg,
                recovery_session=recovery_session, failure_hook=failure_hook,
            )
            log('proof_verify', f'处理后状态={state}', 'INFO')
            continue
        if state == 'kmsi':
            state = _handle_kmsi(page, log)
            log('kmsi', f'处理后状态={state}', 'INFO')
            continue
        if state == 'unknown':
            if _dismiss_passkey_setup(page, log):
                state = _wait_for_auth_state_or_code(page, captured_code, timeout_ms=12000)
                continue
            # KMSI 可能落在 unknown
            try:
                from controllers.recovery_bind import is_kmsi_page, _click_kmsi_no
                if is_kmsi_page(page):
                    _click_kmsi_no(page, log=log)
                    state = _wait_for_auth_state_or_code(page, captured_code, timeout_ms=8000)
                    continue
            except Exception:
                pass
        break
    return state


def _perform_login_after_cookie_fail(
    page, full_email, password, log, failure_hook=None, state='login_email',
    captured_code=None, temp_mail_cfg=None, recovery_already_bound=False, recovery_session=None,
):
    state = _digest_post_email_states(
        page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
        recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
        failure_hook=failure_hook, rounds=4,
    )

    if state == 'login_email':
        log('login_email', '开始输入邮箱', 'WARN')
        try:
            email_stage = _submit_email(page, full_email, log)
        except Exception as exc:
            log('login_email', f'邮箱提交异常: {_compact_exc(exc)}', 'WARN')
            email_stage = _current_auth_entry_state(page)
        if email_stage in (
            'login_password', 'consent', 'code', 'account_type',
            'protect_account', 'proof_verify', 'kmsi',
        ):
            state = email_stage
        else:
            state = _wait_for_auth_state_or_code(
                page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_email'}
            )
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4,
        )
        log('login_email', f'邮箱提交后状态={state}', 'INFO')
        if _has_unknown_account(page):
            _dump_auth_page(page, log)
            log('login_email', '邮箱不存在', 'FAIL')
            return False
        if state == 'login_email':
            if failure_hook:
                failure_hook('oauth_login_timeout')
            _dump_auth_page(page, log)
            log('login_email', '邮箱页停留超时', 'FAIL')
            return False

    state = _digest_post_email_states(
        page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
        recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
        failure_hook=failure_hook, rounds=3,
    )

    if state == 'login_password':
        # 冷登录验证辅助邮箱后，有时不必再输密码；若出现密码页再填
        if _has_password_login_blocked(page):
            if failure_hook:
                failure_hook('oauth_password_blocked')
            _dump_auth_page(page, log)
            log('login_password', '密码登录不可用，跳过硬填', 'FAIL')
            return False
        log('login_password', '开始输入密码', 'WARN')
        _submit_password(page, password, log)
        if _has_password_login_blocked(page):
            if failure_hook:
                failure_hook('oauth_password_blocked')
            _dump_auth_page(page, log)
            log('login_password', '检测到密码登录不可用', 'FAIL')
            return False
        if _has_invalid_password(page):
            if failure_hook:
                failure_hook('oauth_password_wrong')
            _dump_auth_page(page, log)
            log('login_password', '检测到密码错误提示', 'FAIL')
            return False
        state = _wait_for_auth_state_or_code(
            page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_password'}
        )
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4,
        )
        log('login_password', f'密码提交后状态={state}', 'INFO')
        if state == 'code':
            return True
        if state != 'consent':
            if _has_password_login_blocked(page):
                if failure_hook:
                    failure_hook('oauth_password_blocked')
                _dump_auth_page(page, log)
                log('login_password', '密码提交后：密码登录不可用', 'FAIL')
            elif _has_invalid_password(page):
                if failure_hook:
                    failure_hook('oauth_password_wrong')
                _dump_auth_page(page, log)
                log('login_password', '检测到密码错误提示', 'FAIL')
            else:
                if failure_hook:
                    failure_hook('oauth_consent_fail')
                _dump_auth_page(page, log)
                log('login_password', f'未进入同意页面 final_state={state}', 'FAIL')
            return False

    # 冷登录常见：邮箱 → proof(codeEntry 自动验证) → kmsi 否 → consent（可能无密码页）
    if state in ('proof_verify', 'kmsi'):
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4,
        )
        log('proof_verify', f'proof/kmsi 处理后状态={state}', 'INFO')
        if state == 'login_password':
            # 验证后若仍要密码，再走一轮
            if not _has_password_login_blocked(page):
                log('login_password', 'proof 后出现密码页，继续填写', 'WARN')
                _submit_password(page, password, log)
                state = _wait_for_auth_state_or_code(
                    page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_password'}
                )
                state = _digest_post_email_states(
                    page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
                    failure_hook=failure_hook, rounds=3,
                )
        if state == 'code':
            return True
        if state == 'consent':
            return True
        if state not in ('consent', 'code'):
            if failure_hook:
                failure_hook('oauth_consent_fail')
            _dump_auth_page(page, log)
            log('proof_verify', f'验证后未进入同意页 final_state={state}', 'FAIL')
            return False

    return state in ('consent', 'code')


def _exchange_code_once(code, proxy_url=None, timeout_sec=20):
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
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
        timeout=timeout_sec,
        proxies=proxies,
    )
    response.raise_for_status()
    return response.json()


def _exchange_code_with_retry(code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    proxy_candidates = []
    saw_network_error = False
    for item in (current_proxy,):
        if item and item not in proxy_candidates:
            proxy_candidates.append(item)
    if token_proxy_getter:
        for _ in range(2):
            try:
                picked = token_proxy_getter(exclude=proxy_candidates[-1] if proxy_candidates else current_proxy)
            except TypeError:
                picked = token_proxy_getter()
            except Exception as exc:
                log('token', f'获取新代理失败: {exc}', 'WARN')
                picked = ""
            if picked and picked not in proxy_candidates:
                proxy_candidates.append(picked)
    proxy_candidates.append("")
    total_attempts = len(proxy_candidates)
    last_error = None
    for idx in range(total_attempts):
        proxy_url = proxy_candidates[idx]
        proxy_text = proxy_url or "direct"
        try:
            log('token', f'开始换 token 第 {idx + 1}/{total_attempts} 次 proxy={proxy_text}', 'INFO')
            data = _exchange_code_once(code, proxy_url=proxy_url or None, timeout_sec=20)
            if 'refresh_token' not in data:
                last_error = RuntimeError(data.get('error_description') or data.get('error') or 'unknown')
                log('token', f"token请求失败 proxy={proxy_text}: {data.get('error', 'unknown')}", 'WARN')
                if idx < total_attempts - 1:
                    time.sleep(1.5 + idx)
                    continue
                break
            return True, data['refresh_token']
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            saw_network_error = True
            log('token', f'网络异常 proxy={proxy_text}: {exc}', 'WARN')
            if idx < total_attempts - 1:
                time.sleep(1.5 + idx)
                continue
        except Exception as exc:
            last_error = exc
            log('token', f'换 token 异常 proxy={proxy_text}: {exc}', 'WARN')
            if idx < total_attempts - 1:
                time.sleep(1.5 + idx)
                continue
    if saw_network_error and failure_hook:
        failure_hook('oauth_token_network_fail')
    if failure_hook:
        failure_hook('oauth_token_fail')
    log('token', f'最终换 token 失败: {last_error}', 'FAIL')
    return False, None


def _click_consent_and_exchange(page, captured_code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    accept_btn = page.locator(CONSENT_SELECTOR)
    accept_btn.wait_for(state='visible', timeout=60000)
    accept_btn.click(timeout=10000)
    log('consent', '点击接受授权', 'OK')

    code = _wait_for_code_capture(page, captured_code, timeout_ms=180000)
    if not code:
        if failure_hook:
            failure_hook('oauth_code_fail')
        log('callback', '3分钟内未捕获到code', 'FAIL')
        return False, None

    log('callback', '捕获到code', 'OK')
    return _exchange_code_with_retry(
        code,
        log=log,
        failure_hook=failure_hook,
        current_proxy=current_proxy,
        token_proxy_getter=token_proxy_getter,
    )


def _exchange_captured_code(page, captured_code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    code = _wait_for_code_capture(page, captured_code, timeout_ms=1000, poll_ms=100)
    if not code:
        return False, None
    log('callback', '已直接捕获到code，跳过同意页', 'OK')
    return _exchange_code_with_retry(
        code,
        log=log,
        failure_hook=failure_hook,
        current_proxy=current_proxy,
        token_proxy_getter=token_proxy_getter,
    )


def get_oauth2_token(page, full_email, password, results_dir=None, prefix='', backup_proxy=None, failure_hook=None, log_hook=None, current_proxy="", token_proxy_getter=None, temp_mail_cfg=None, recovery_already_bound=False, recovery_session=None):
    # 同 context 必须 prefer_sso：不要 sso_reload，否则 cookie 会话被强制打断
    auth_url = build_auth_url(prefer_sso=True)

    def _log(stage, message, level='INFO'):
        if log_hook:
            log_hook(stage, message, level)
            return
        tag = prefix if prefix else "[OAuth2:COOKIE]"
        print(f"{tag}[{level}] {time.strftime('%H:%M:%S')} | {stage} | {message}")

    def _try_flow():
        _log('start', '开始 OAuth2 (同浏览器 context 复用 cookie，无 sso_reload)')
        # 同一 BrowserContext 新开 tab，共享注册后的 login.live.com cookie
        pg = page.context.new_page()
        captured_code = [None]

        def on_request(request):
            code = _extract_code_from_url(request.url)
            if code:
                captured_code[0] = code

        def on_frame_navigated(frame):
            code = _extract_code_from_url(frame.url)
            if code:
                captured_code[0] = code

        pg.on('request', on_request)
        pg.on('framenavigated', on_frame_navigated)

        try:
            t1 = time.time()
            pg.goto(auth_url, timeout=AUTH_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            _settle_auth_page(pg, _log, 'goto')
            _disable_auth_page_autofill(pg, _log)
            _log('goto', f"进入auth页面 (+{time.time()-t1:.0f}s)")

            # SSO 有时慢，多给一点时间再判状态
            state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS)
            _log('entry', f'首次检测状态={state}')

            if state == 'account_type':
                state = _resolve_account_type(pg, _log, captured_code=captured_code)
                _log('entry', f'帐户类型处理后状态={state}')
            if state == 'protect_account':
                state = _handle_protect_account(
                    pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                    already_bound=recovery_already_bound,
                )
                _log('entry', f'保护帐户处理后状态={state}')
            if state == 'proof_verify':
                state = _handle_proof_verify(
                    pg, _log, temp_mail_cfg=temp_mail_cfg,
                    recovery_session=recovery_session, failure_hook=failure_hook,
                )
                _log('entry', f'proof 验证后状态={state}')
            if state == 'kmsi':
                state = _handle_kmsi(pg, _log)
                _log('entry', f'kmsi 处理后状态={state}')

            # 策略：有 cookie 的环境下，login_email 时**不要**连环 reload（会冲掉 SSO）。
            if state == 'login_email':
                _log('entry', '仍需邮箱：跳过连环 recovery，同页直接补登（保留 cookie）', 'WARN')
            elif state == 'unknown':
                _dump_auth_page(pg, _log, 'entry_unknown')
                if _is_account_type_page(pg):
                    state = _resolve_account_type(pg, _log, captured_code=captured_code)
                elif _is_protect_account_page(pg):
                    state = _handle_protect_account(
                        pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                        already_bound=recovery_already_bound,
                    )
                elif _is_proof_verify_page(pg):
                    state = _handle_proof_verify(
                        pg, _log, temp_mail_cfg=temp_mail_cfg,
                        recovery_session=recovery_session, failure_hook=failure_hook,
                    )
                elif _is_kmsi_only_page(pg):
                    state = _handle_kmsi(pg, _log)
                else:
                    _log('entry', 'unknown：单次 goto 重试 authorize', 'WARN')
                    try:
                        pg.goto(auth_url, timeout=AUTH_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                        _settle_auth_page(pg, _log, 'goto_retry')
                        state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=15000)
                    except Exception as exc:
                        _log('entry', f'goto 重试失败: {_compact_exc(exc)}', 'WARN')
                _log('entry', f'处理后状态={state}')

            if state == 'account_type':
                state = _resolve_account_type(pg, _log, captured_code=captured_code)
            if state == 'protect_account':
                state = _handle_protect_account(
                    pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                    already_bound=recovery_already_bound,
                )
            if state == 'proof_verify':
                state = _handle_proof_verify(
                    pg, _log, temp_mail_cfg=temp_mail_cfg,
                    recovery_session=recovery_session, failure_hook=failure_hook,
                )
            if state == 'kmsi':
                state = _handle_kmsi(pg, _log)

            if state in ('login_email', 'login_password', 'account_type', 'protect_account', 'proof_verify', 'kmsi'):
                ok = _perform_login_after_cookie_fail(
                    pg,
                    full_email,
                    password,
                    _log,
                    failure_hook=failure_hook,
                    state=state,
                    captured_code=captured_code,
                    temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound,
                    recovery_session=recovery_session,
                )
                if not ok:
                    return False, None
                state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=8000)
                state = _digest_post_email_states(
                    pg, _log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
                    failure_hook=failure_hook, rounds=3,
                )
                _log('entry', f'登录后阶段={state}', 'INFO')

            if state == 'code':
                return _exchange_captured_code(
                    pg,
                    captured_code,
                    _log,
                    failure_hook=failure_hook,
                    current_proxy=current_proxy,
                    token_proxy_getter=token_proxy_getter,
                )
            if state == 'consent':
                return _click_consent_and_exchange(
                    pg,
                    captured_code,
                    _log,
                    failure_hook=failure_hook,
                    current_proxy=current_proxy,
                    token_proxy_getter=token_proxy_getter,
                )

            if failure_hook:
                failure_hook('oauth_consent_fail')
            _dump_auth_page(pg, _log)
            _log('entry', f'未进入同意或登录页面，最终状态={state}', 'FAIL')
            return False, None
        except Exception as e:
            _log('exception', f'异常: {_compact_exc(e)}', 'FAIL')
            return False, None
        finally:
            try:
                pg.remove_listener('request', on_request)
                pg.remove_listener('framenavigated', on_frame_navigated)
            except Exception:
                pass
            try:
                pg.close()
            except Exception:
                pass

    try:
        success, token = _try_flow()
        if success:
            return True, token
    except Exception as e:
        _log('outer', f'首次尝试异常: {_compact_exc(e)}', 'FAIL')
    return False, None
