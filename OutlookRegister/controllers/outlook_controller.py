import os
import time
import random
import math
import shutil
import threading
from faker import Faker
from patchright.sync_api import sync_playwright


class OutlookController:
    """
    Outlook 自动注册控制器。
    
    职责：浏览器管理、代理选择(IP加权)、注册流程、验证码突破。
    每个线程独立的浏览器实例，通过 thread_local 隔离。
    类变量在所有线程间共享（代理使用计数、IP表现追踪、统计）。
    """

    # === 类变量（所有线程共享）===
    _proxy_usage = {}      # 每个代理端口被选中的次数
    _proxy_config = None   # 代理配置缓存（只解析一次）
    _ip_tracker = {}       # IP表现追踪（仅内存，不持久化）
    _attempts = 0          # 累计验证码尝试次数
    _success = 0           # 累计验证码成功次数
    _ip_info_cache = {}    # IP地理信息缓存（避免重复查询ipinfo）
    _b2_attempts = {'click': 0, 'dblclick': 0, 'hold': 0}
    _b2_success = {'click': 0, 'dblclick': 0, 'hold': 0}
    _state_lock = threading.Lock()

    # 国家代码 → (locale, 默认时区)
    LOCALE_MAP = {
        'JP': ('ja-JP', 'Asia/Tokyo'),       'US': ('en-US', 'America/Chicago'),
        'HK': ('zh-HK', 'Asia/Hong_Kong'),   'SG': ('en-SG', 'Asia/Singapore'),
        'KR': ('ko-KR', 'Asia/Seoul'),       'GB': ('en-GB', 'Europe/London'),
        'DE': ('de-DE', 'Europe/Berlin'),    'FR': ('fr-FR', 'Europe/Paris'),
        'CA': ('en-CA', 'America/Toronto'),  'AU': ('en-AU', 'Australia/Sydney'),
        'TW': ('zh-TW', 'Asia/Taipei'),      'CN': ('zh-CN', 'Asia/Shanghai'),
        'BR': ('pt-BR', 'America/Sao_Paulo'),'IN': ('en-IN', 'Asia/Kolkata'),
        'NL': ('nl-NL', 'Europe/Amsterdam'), 'TH': ('th-TH', 'Asia/Bangkok'),
        'VN': ('vi-VN', 'Asia/Ho_Chi_Minh'), 'MY': ('ms-MY', 'Asia/Kuala_Lumpur'),
        'PH': ('en-PH', 'Asia/Manila'),      'ID': ('id-ID', 'Asia/Jakarta'),
    }

    def __init__(self, config_data):
        """初始化：加载配置 → 创建线程存储 → 初始化统计 → 解析代理"""
        # config.json 已在 main.py 读取并解析，直接传入 dict
        self.wait_time = config_data['bot_protection_wait'] * 1000  # 秒→毫秒
        self.max_captcha_retries = config_data['max_captcha_retries']
        self.captcha_strategy = config_data.get('captcha_strategy', 0)
        self.enable_oauth2 = config_data["oauth2"]['enable_oauth2']
        self.headless = config_data.get('headless', False)
        self.email_suffix = config_data['email_suffix']

        # 公开发行版：固定 patchright 自带 Chromium，不使用指纹/自定义浏览器
        browser_cfg = config_data.get('browser', {}) or {}
        self.browser_executable_path = ''  # 强制空 → patchright builtin
        self.fingerprint_enabled = False
        self.fingerprint_platform = 'windows'
        self.fingerprint_brand = 'Chrome'
        user_data_root = (browser_cfg.get('user_data_root') or '').strip()
        self.browser_user_data_root = user_data_root or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'browser_profiles',
        )
        # 备用邮箱绑定（CF Temp Mail）：概率出现，非固定步骤
        # 注册后 / OAuth 中任一处弹出「保护帐户」页则绑定；未弹出则直接继续
        self.temp_mail_cfg = config_data.get('temp_mail', {}) or {}
        self.bind_recovery_email = bool(self.temp_mail_cfg.get('enabled', True))

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.failure_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.active_resources = []
        self.active_playwrights = []
        self.log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'log')
        os.makedirs(self.log_dir, exist_ok=True)
        if self.fingerprint_enabled or self.browser_executable_path:
            os.makedirs(self.browser_user_data_root, exist_ok=True)
        self.log_path = os.path.join(
            self.log_dir,
            f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{os.getpid()}.txt"
        )
        self.log_plain("[Browser] mode=patchright-chromium (builtin) fingerprint=false")
        self.runtime_stats = {
            'started_at': time.time(),
            'submitted': 0,
            'running': 0,
            'succeeded': 0,
            'failed': 0,
        }

        self.failure_stats = {
            'ip_cant_open': 0,
            'ip_blocked': 0,
            'captcha_fail': 0,
            'captcha_btn2_never_appeared': 0,
            'captcha_btn2_appeared_but_failed': 0,
            'funcaptcha': 0,
            'timeout': 0,
            'register_page_open_fail': 0,
            'register_form_fail': 0,
            'mail_init_fail': 0,
            'oauth_login_timeout': 0,
            'oauth_consent_fail': 0,
            'oauth_code_fail': 0,
            'oauth_token_network_fail': 0,
            'oauth_token_fail': 0,
            'oauth_password_wrong': 0,
            'oauth_password_blocked': 0,
            'oauth_retry_exhausted': 0,
            'recovery_bind_fail': 0,
            'browser_launch_fail': 0,
            'browser_context_fail': 0,
            'browser_page_fail': 0,
            'playwright_runtime_fail': 0,
        }

        cls = type(self)
        if cls._proxy_config is None:
            cls._proxy_config = self._parse_proxy_config(config_data.get('proxy', {}))

    # ============================================================
    # IP 信息查询（国家 + 时区 + 坐标，带缓存）
    # ============================================================
    @classmethod
    def _get_ip_info(cls, proxy_url):
        """查询代理IP的地理信息：国家代码、时区、GPS坐标。结果缓存，同一代理只查一次。"""
        if not proxy_url:
            return {'country': '??', 'timezone': 'UTC', 'loc': None}
        with cls._state_lock:
            if proxy_url in cls._ip_info_cache:
                return cls._ip_info_cache[proxy_url]
        info = {'country': '??', 'timezone': 'UTC', 'loc': None}
        try:
            import requests
            r = requests.get('https://ipinfo.io/json', proxies={'https': proxy_url},
                             timeout=3, headers={'Accept': 'application/json'})
            if r.status_code == 200:
                d = r.json()
                info = {
                    'country': d.get('country', '??'),
                    'timezone': d.get('timezone', 'UTC'),
                    'loc': d.get('loc', None),  # "35.68,139.76"
                }
        except Exception:
            pass
        with cls._state_lock:
            cls._ip_info_cache[proxy_url] = info
        return info

    def bump_failure(self, *names):
        with self.failure_lock:
            for name in names:
                self.failure_stats[name] = self.failure_stats.get(name, 0) + 1

    def _reset_thread_runtime(self):
        for attr in ('_proxy', '_ip_info', '_log_prefix'):
            if hasattr(self.thread_local, attr):
                delattr(self.thread_local, attr)

    def prepare_thread_context(self):
        proxy = getattr(self.thread_local, '_proxy', None)
        if not proxy:
            proxy = self._pick_proxy()
        info = getattr(self.thread_local, '_ip_info', None)
        if info is None:
            info = self._get_ip_info(proxy)
            self.thread_local._ip_info = info
        return proxy, info

    def set_task_prefix(self, task_num, total):
        """设置当前线程的日志前缀： [编号/总-国家-IP] 并缓存IP地理信息"""
        proxy, info = self.prepare_thread_context()
        ip_short = proxy.split('//')[-1] if '//' in proxy else proxy
        self.thread_local._log_prefix = f"[{task_num}/{total}-{info['country']}-{ip_short}]"

    def log_event(self, flow, level, stage, message, attempt=None):
        line = self._format_log_line(flow, level, stage, message, attempt=attempt)
        self.write_log_line(line)

    def make_logger(self, flow, attempt=None):
        def _logger(stage, message, level='INFO'):
            self.log_event(flow, level, stage, message, attempt=attempt)
        return _logger

    def _log(self, msg):
        self.log_event('TASK', 'INFO', 'general', msg)

    def _log_prefix_str(self):
        return getattr(self.thread_local, '_log_prefix', '')

    def _format_log_line(self, flow, level, stage, message, attempt=None):
        prefix = getattr(self.thread_local, '_log_prefix', '')
        attempt_part = f"[A{attempt}]" if attempt is not None else ""
        return f"{prefix}[{flow}]{attempt_part}[{level}] {time.strftime('%H:%M:%S')} | {stage} | {message}"

    def write_log_line(self, line):
        # 强制单行：Playwright Call log 等多行异常不得刷屏
        if line is None:
            return
        text = str(line).replace('\r\n', '\n').replace('\r', '\n')
        if '\n' in text:
            parts = [p.strip() for p in text.split('\n') if p.strip()]
            # 丢弃 Call log 明细行
            kept = []
            for p in parts:
                if p.startswith('Call log:') or p.startswith('- waiting') or p.startswith('- navigated') or p.startswith('- attempting'):
                    continue
                kept.append(p)
            text = ' | '.join(kept) if kept else parts[0]
            if len(text) > 400:
                text = text[:397] + '...'
        with self.log_lock:
            print(text, flush=True)
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(text + '\n')
                f.flush()

    def log_plain(self, message):
        self.write_log_line(message)

    def update_runtime_stats(self, **kwargs):
        with self.runtime_lock:
            self.runtime_stats.update(kwargs)

    def get_runtime_stats(self):
        with self.runtime_lock:
            return dict(self.runtime_stats)

    def set_progress_base(self, succeeded=0, failed=0, started_at=None):
        """跨批次累计基数：进度条连续，不因换批归零。"""
        with self.runtime_lock:
            self._progress_base_succeeded = int(succeeded or 0)
            self._progress_base_failed = int(failed or 0)
            self._progress_run_started_at = started_at if started_at is not None else time.time()
            self.runtime_stats['succeeded'] = self._progress_base_succeeded
            self.runtime_stats['failed'] = self._progress_base_failed
            self.runtime_stats['started_at'] = self._progress_run_started_at

    def note_task_finished(self, success, total_tasks):
        """任务结束更新计数；仅成功时立刻打印进度（勿等 clean_up）。

        字段为**跨批次累计**：成功数 | 当前进度(成功+失败) | 总数 | 成功率 | 总耗时
        """
        with self.runtime_lock:
            if success:
                self.runtime_stats['succeeded'] = self.runtime_stats.get('succeeded', 0) + 1
            else:
                self.runtime_stats['failed'] = self.runtime_stats.get('failed', 0) + 1
            succeeded = self.runtime_stats.get('succeeded', 0)
            failed = self.runtime_stats.get('failed', 0)
            started = (
                getattr(self, '_progress_run_started_at', None)
                or self.runtime_stats.get('started_at')
                or time.time()
            )
        if not success:
            return
        current = succeeded + failed
        total = max(int(total_tasks or 0), 1)
        elapsed = time.time() - started
        rate = succeeded / max(current, 1) * 100
        self.log_plain(
            f"[进度] 成功 {succeeded} | 当前 {current}/{total} | "
            f"成功率 {succeeded}/{current} ({rate:.0f}%) | 总耗时 {elapsed / 60:.1f}min"
        )

    @classmethod
    def reset_shared_state(cls):
        with cls._state_lock:
            cls._proxy_usage.clear()
            cls._ip_tracker.clear()
            cls._ip_info_cache.clear()
            cls._attempts = 0
            cls._success = 0
            cls._b2_attempts = {'click': 0, 'dblclick': 0}
            cls._b2_success = {'click': 0, 'dblclick': 0}

    def penalize_ip(self, penalty=4):
        """惩罚当前IP（OAuth2全部失败、账号不存在等严重错误时调用）。增加失败计数，影响后续代理选择权重。"""
        proxy = getattr(self.thread_local, '_proxy', '')
        key = proxy.split('//')[-1] if '//' in proxy else proxy
        if key:
            with self._state_lock:
                if key not in self._ip_tracker:
                    self._ip_tracker[key] = {'win': 0, 'total': 0}
                self._ip_tracker[key]['total'] += penalty
            self.log_event('PROXY', 'WARN', 'penalize', f"{key} 惩罚 +{penalty}")

    def fresh_proxy_url(self, exclude: str = "") -> str:
        previous_proxy = getattr(self.thread_local, '_proxy', None)
        previous_info = getattr(self.thread_local, '_ip_info', None)
        try:
            if hasattr(self.thread_local, '_proxy'):
                del self.thread_local._proxy
            if hasattr(self.thread_local, '_ip_info'):
                del self.thread_local._ip_info
            for _ in range(4):
                picked = self._pick_proxy()
                if not exclude or picked != exclude:
                    return picked
            return self._pick_proxy()
        finally:
            if hasattr(self.thread_local, '_proxy'):
                del self.thread_local._proxy
            if hasattr(self.thread_local, '_ip_info'):
                del self.thread_local._ip_info
            if previous_proxy:
                self.thread_local._proxy = previous_proxy
            if previous_info is not None:
                self.thread_local._ip_info = previous_info

    def _register_active_browser(self, browser):
        with self.cleanup_lock:
            self.active_resources.append(browser)

    def _unregister_active_browser(self, browser):
        with self.cleanup_lock:
            self.active_resources = [item for item in self.active_resources if item is not browser]

    def _register_active_playwright(self, playwright):
        with self.cleanup_lock:
            self.active_playwrights.append(playwright)

    def _unregister_active_playwright(self, playwright):
        with self.cleanup_lock:
            self.active_playwrights = [item for item in self.active_playwrights if item is not playwright]

    @staticmethod
    def _browser_failure_key(stage, message):
        msg = (message or "").lower()
        if stage == 'playwright':
            return 'playwright_runtime_fail'
        if 'event loop is closed' in msg or 'playwright already stopped' in msg or 'asyncio loop' in msg:
            return 'playwright_runtime_fail'
        if stage == 'launch':
            return 'browser_launch_fail'
        if stage == 'context':
            return 'browser_context_fail'
        return 'browser_page_fail'

    def _log_browser_failure(self, stage, exc):
        message = str(exc)
        failure_key = self._browser_failure_key(stage, message)
        self.bump_failure(failure_key)
        self.log_event('BROWSER', 'FAIL', stage, f"{message} | class={failure_key}")
        return failure_key

    def _dispose_thread_playwright(self):
        playwright = getattr(self.thread_local, 'playwright', None)
        if not playwright:
            return
        try:
            playwright.stop()
        except Exception:
            pass
        self._unregister_active_playwright(playwright)
        try:
            del self.thread_local.playwright
        except Exception:
            pass

    def _dispose_thread_browser(self):
        browser = getattr(self.thread_local, 'browser', None)
        if not browser:
            return
        try:
            browser.close()
        except Exception:
            pass
        self._unregister_active_browser(browser)
        try:
            del self.thread_local.browser
        except Exception:
            pass

    def _thread_playwright(self):
        playwright = getattr(self.thread_local, 'playwright', None)
        if playwright:
            return playwright
        try:
            playwright = sync_playwright().start()
        except Exception as exc:
            self._log_browser_failure('playwright', exc)
            return None
        self.thread_local.playwright = playwright
        self._register_active_playwright(playwright)
        self.log_event('BROWSER', 'INFO', 'playwright', '线程级 Playwright 已初始化')
        return playwright

    # ============================================================
    # 代理
    # ============================================================
    @classmethod
    def _parse_proxy_config(cls, pc):
        """解析代理配置：单端口 or 端口池。返回 {type, host, ports, max_per}"""
        mode = pc.get('mode', 'single')
        proxy_type = pc.get('type', 'http')
        host = pc.get('host', '127.0.0.1')
        if mode == 'single':
            ports = [pc.get('single_port', 7890)]
        else:
            ports = list(range(pc.get('port_start', 24000), pc.get('port_end', 24064) + 1))
        return {'type': proxy_type, 'host': host, 'ports': ports, 'max_per': pc.get('max_per_proxy', 20)}

    def _pick_proxy(self):
        """选择代理端口：两步——①过滤（排除用满的+烂IP）②加权随机（胜率高的优先）"""
        cfg = self._proxy_config
        with self._state_lock:
            available = []
            for p in cfg['ports']:
                if self._proxy_usage.get(p, 0) >= cfg['max_per']:
                    continue
                key = f"{cfg['host']}:{p}"
                info = self._ip_tracker.get(key, {})
                total = info.get('total', 0)
                win = info.get('win', 0)
                fail = max(total - win, 0)
                if total >= 2 and win == 0:
                    continue
                if fail >= 4 and win * 2 < fail:
                    continue
                available.append(p)
            if not available:
                available = list(cfg['ports'])
                for p in available:
                    self._proxy_usage[p] = 0
            weights = []
            for p in available:
                key = f"{cfg['host']}:{p}"
                info = self._ip_tracker.get(key, {})
                total = info.get('total', 0)
                win = info.get('win', 0)
                fail = max(total - win, 0)
                rate = win / total if total else 0.5
                weight = ((1 + win * 4) / (1 + fail * 3)) * (max(rate, 0.05) ** 2)
                weights.append(max(0.01, weight))
            port = random.choices(available, weights=weights, k=1)[0]
            self._proxy_usage[port] = self._proxy_usage.get(port, 0) + 1
        proxy_url = f"{cfg['type']}://{cfg['host']}:{port}"
        self.thread_local._proxy = proxy_url
        return proxy_url

    # ============================================================
    # 浏览器管理
    # ============================================================
    def _resolve_timezone(self, info):
        """代理国家 → 时区；ipinfo 有效时区优先。"""
        country = (info or {}).get('country', '??')
        locale_map = OutlookController.LOCALE_MAP
        tz = locale_map.get(country, locale_map.get('US'))[1]
        raw_tz = (info or {}).get('timezone', 'UTC')
        if raw_tz and raw_tz != 'UTC':
            tz = raw_tz
        return tz

    def _make_fingerprint_seed(self, proxy_url):
        """每个任务生成独立指纹种子（32-bit 正整数）。"""
        # 混入代理端口 + 时间 + 随机，避免多任务共用同一设备指纹
        port_part = 0
        try:
            hostport = proxy_url.split('//')[-1]
            port_part = int(hostport.rsplit(':', 1)[-1])
        except Exception:
            pass
        seed = (int(time.time() * 1000) ^ (port_part * 2654435761) ^ random.getrandbits(32)) & 0x7FFFFFFF
        if seed == 0:
            seed = random.randint(1, 0x7FFFFFFF)
        return seed

    def _prepare_user_data_dir(self, seed):
        """为本次浏览器创建独立 user-data-dir。"""
        base = self.browser_user_data_root
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"fp_{seed}_{os.getpid()}_{threading.get_ident()}")
        os.makedirs(path, exist_ok=True)
        return path

    def _cleanup_user_data_dir(self, path):
        if not path:
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def clear_browser_profiles_dir(root, log_fn=None):
        """清空 browser_profiles 目录下全部内容（保留目录本身）。"""
        if not root:
            return 0
        try:
            os.makedirs(root, exist_ok=True)
        except Exception:
            return 0
        removed = 0
        try:
            names = os.listdir(root)
        except Exception:
            return 0
        for name in names:
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                removed += 1
            except Exception:
                pass
        if log_fn:
            try:
                log_fn(f"[Cleanup] 已清空 browser_profiles 共 {removed} 项: {root}")
            except Exception:
                pass
        return removed

    def clear_browser_profiles_root(self, log=True):
        """清空本实例配置的 fingerprint profile 根目录。"""
        root = getattr(self, 'browser_user_data_root', None)
        log_fn = self.log_plain if log and hasattr(self, 'log_plain') else None
        return self.clear_browser_profiles_dir(root, log_fn=log_fn)

    def launch_browser(self):
        """启动浏览器：选代理 → fingerprint-chromium(可选) → 反检测参数。
        返回 (playwright, browser_or_context)。
        使用自定义 chrome.exe 时走 launch_persistent_context（独立 profile）。
        """
        try:
            p = self._thread_playwright()
            if not p:
                return False, False
            proxy_url, info = self.prepare_thread_context()
            tz = self._resolve_timezone(info)
            locale = 'zh-CN'
            viewport = {
                'width': random.choice([1366, 1440, 1536, 1680, 1920]),
                'height': random.choice([768, 864, 900, 1050, 1080]),
            }

            args = [
                '--lang=zh-CN',
                '--accept-lang=zh-CN,zh,en-US,en',
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                '--disable-autofill-keyboard-accessory-view',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                '--disable-non-proxied-udp',
                # 抑制 Windows Hello / Passkey / 安全密钥系统弹窗（网页层仍可能出「创建通行密钥」，靠后续取消/直达邮箱）
                '--disable-webauthn',
                '--disable-features=WebAuthentication,WebAuthenticationConditionalUI,WebAuthenticationCable,WebAuthenticationHybridTransport,WebAuthenticationPasskeysUI,Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication,PasswordManagerOnboarding,PasswordImport,BiometricAuthenticationInSettings',
                '--disable-save-password-bubble',
                '--disable-password-manager-reauthentication',
                '--disable-component-update',
                '--disable-sync', '--disable-default-apps',
                f'--timezone={tz}',
            ]

            common = {
                'headless': self.headless,
                'args': args,
                'proxy': {"server": proxy_url, "bypass": "localhost"},
            }

            exe = self.browser_executable_path
            profile_dir = None
            seed = None
            self.thread_local._persistent_context = False

            if exe:
                if not os.path.isfile(exe):
                    self.log_event('BROWSER', 'FAIL', 'launch_detail', f"浏览器路径不存在: {exe}")
                    return False, False
                seed = self._make_fingerprint_seed(proxy_url) if self.fingerprint_enabled else random.randint(1, 0x7FFFFFFF)
                profile_dir = self._prepare_user_data_dir(seed)
                if self.fingerprint_enabled:
                    args.append(f'--fingerprint={seed}')
                    if self.fingerprint_platform:
                        args.append(f'--fingerprint-platform={self.fingerprint_platform}')
                    if self.fingerprint_brand:
                        args.append(f'--fingerprint-brand={self.fingerprint_brand}')
                mode = 'fingerprint-chromium' if self.fingerprint_enabled else 'custom-chromium'
                self.log_event(
                    'BROWSER', 'INFO', 'launch',
                    f"exe={mode} path={exe} seed={seed} fp={self.fingerprint_enabled} tz={tz} proxy={proxy_url.split('//')[-1]}"
                )
                # Playwright 要求 user_data_dir 走 persistent_context，不能塞进 args
                ctx_opts = {
                    **common,
                    'executable_path': exe,
                    'locale': locale,
                    'timezone_id': tz,
                    'viewport': viewport,
                }
                if info.get('loc'):
                    try:
                        lat, lng = info['loc'].split(',')
                        ctx_opts['geolocation'] = {'latitude': float(lat), 'longitude': float(lng)}
                    except Exception:
                        pass
                b = p.chromium.launch_persistent_context(profile_dir, **ctx_opts)
                self.thread_local._persistent_context = True
            else:
                # executable_path 为空：使用 patchright 自带 Chromium（A/B：对照指纹浏览器）
                self.log_event(
                    'BROWSER', 'INFO', 'launch',
                    f"exe=patchright-chromium fp=false tz={tz} proxy={proxy_url.split('//')[-1]}"
                )
                b = p.chromium.launch(**common)

            self.thread_local._browser_profile_dir = profile_dir
            self.thread_local._fingerprint_seed = seed
            self._register_active_browser(b)
            return p, b
        except Exception as e:
            profile_dir = getattr(self.thread_local, '_browser_profile_dir', None)
            self._cleanup_user_data_dir(profile_dir)
            if hasattr(self.thread_local, '_browser_profile_dir'):
                delattr(self.thread_local, '_browser_profile_dir')
            failure_key = self._log_browser_failure('launch', e)
            self.log_event('BROWSER', 'FAIL', 'launch_detail', f"启动浏览器失败: {e}")
            if failure_key == 'playwright_runtime_fail':
                self._dispose_thread_browser()
                self._dispose_thread_playwright()
            return False, False

    def get_thread_browser(self):
        """获取当前线程的浏览器。首次调用时创建，之后复用。线程隔离，各自独立。"""
        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                return False
            self.thread_local.browser = b
        return self.thread_local.browser

    def get_thread_page(self):
        browser = self.get_thread_browser()
        if not browser:
            return None

        # fingerprint-chromium 使用 persistent context：browser 实际是 BrowserContext
        if getattr(self.thread_local, '_persistent_context', False):
            try:
                pages = list(browser.pages)
                # 优先复用已有标签页（自定义 Chromium 有时禁止 Target.createTarget）
                if pages:
                    page = pages[0]
                    for extra in pages[1:]:
                        try:
                            extra.close()
                        except Exception:
                            pass
                    try:
                        page.goto('about:blank', timeout=10000)
                    except Exception:
                        pass
                    return page
                return browser.new_page()
            except Exception as exc:
                self._log_browser_failure('page', exc)
                self._dispose_thread_browser()
                return None

        _, info = self.prepare_thread_context()
        locale = 'zh-CN'  # 强制中文（元素定位依赖中文 text）
        tz = self._resolve_timezone(info)
        viewport = {'width': random.choice([1366, 1440, 1536, 1680, 1920]),
                    'height': random.choice([768, 864, 900, 1050, 1080])}
        context_opts = {
            'locale': locale,
            'timezone_id': tz,
            'viewport': viewport,
        }
        if info.get('loc'):
            try:
                lat, lng = info['loc'].split(',')
                context_opts['geolocation'] = {'latitude': float(lat), 'longitude': float(lng)}
            except Exception:
                pass
        context = None
        try:
            context = browser.new_context(**context_opts)
        except Exception as exc:
            failure_key = self._log_browser_failure('context', exc)
            self._dispose_thread_browser()
            if failure_key == 'playwright_runtime_fail':
                self._dispose_thread_playwright()
            return None
        try:
            return context.new_page()
        except Exception as exc:
            self._log_browser_failure('page', exc)
            try:
                context.close()
            except Exception:
                pass
            self._dispose_thread_browser()
            return None

    def clean_up(self, page=None, type="all_browser"):
        """
        资源清理。
        - done_browser: 关闭当前线程的浏览器和page（OAuth2重试前调用，确保下次拿新IP）
        - all_browser: 关闭所有活跃浏览器（程序结束时调用）
        """
        if type == "done_browser":
            if page:
                try:
                    page.context.close()
                except Exception:
                    pass
            profile_dir = getattr(self.thread_local, '_browser_profile_dir', None)
            self._dispose_thread_browser()
            self._cleanup_user_data_dir(profile_dir)
            if hasattr(self.thread_local, '_browser_profile_dir'):
                delattr(self.thread_local, '_browser_profile_dir')
            if hasattr(self.thread_local, '_fingerprint_seed'):
                delattr(self.thread_local, '_fingerprint_seed')
            self._reset_thread_runtime()
        elif type == "all_browser":
            profile_dir = getattr(self.thread_local, '_browser_profile_dir', None)
            with self.cleanup_lock:
                browsers = list(self.active_resources)
                playwights = list(self.active_playwrights)
                self.active_resources.clear()
                self.active_playwrights.clear()
            for browser in browsers:
                try:
                    browser.close()
                except Exception:
                    pass
            for playwright in playwights:
                try:
                    playwright.stop()
                except Exception:
                    pass
            self._cleanup_user_data_dir(profile_dir)
            if hasattr(self.thread_local, '_browser_profile_dir'):
                delattr(self.thread_local, '_browser_profile_dir')
            if hasattr(self.thread_local, '_fingerprint_seed'):
                delattr(self.thread_local, '_fingerprint_seed')
            # 关掉浏览器后再清空整个 profiles 根目录（正常/异常收尾都走这里）
            try:
                self.clear_browser_profiles_root(log=True)
            except Exception:
                pass

    # ============================================================
    # 注册流程
    # ============================================================
    def outlook_register(self, page, email, password):
        """
        完整的Outlook注册流程。
        
        步骤：打开注册页 → 同意条款 → 填邮箱 → 填密码
        → 填生日 → 填姓名 → 提交 → 检测风控 → 通过验证码 → 等邮箱初始化
        
        返回: True(注册成功) 或 False(失败)
        """
        fake = Faker()
        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1999, 2007))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 25))

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=30000, wait_until="domcontentloaded")
            page.get_by_text('同意并继续').wait_for(timeout=30000)
            start_time = time.time()
            page.wait_for_timeout(0.1 * self.wait_time)
            page.get_by_text('同意并继续').click(timeout=30000)
        except Exception:
            self.bump_failure('ip_cant_open', 'register_page_open_fail')
            self._log("[Fail:IP] - IP质量不佳，无法打开Outlook注册页面，请换IP重试")
            return False

        try:
            # 选择是 outlook还是hotmail
            if self.email_suffix == "@hotmail.com":
                page.get_by_text("@outlook.com").click(timeout=10000)
                page.locator(f'[role="option"]:text-is("@hotmail.com")').click()

            # 填充邮箱
            email_input = page.locator('[aria-label="新建电子邮件"]')
            email_input.click()
            email_input.fill(email, timeout=10000)

            # 点击 "下一步
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.wait_for_timeout(0.02 * self.wait_time)

            #填充密码
            page.locator('[type="password"]').type(password, delay=0.004 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)
            
            # 点击 "下一步
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.wait_for_timeout(0.03 * self.wait_time)

            # 填充出生的年份
            page.locator('[name="BirthYear"]').fill(year, timeout=10000)

            # 填充出生日期,实际上不会走 try，走的是Except。因为 有浮层的存在，
            try:
                # 填充月份
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator('[name="BirthMonth"]').select_option(value=month, timeout=1000)

                # 填充日期
                page.wait_for_timeout(0.05 * self.wait_time)
                page.locator('[name="BirthDay"]').select_option(value=day)
            except Exception:

                # 填充月份
                page.locator('[name="BirthMonth"]').click()
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator(f'[role="option"]:text-is("{month}月")').click()
                page.wait_for_timeout(0.04 * self.wait_time)

                # 填充日期
                page.locator('[name="BirthDay"]').click()
                page.wait_for_timeout(0.03 * self.wait_time)
                page.locator(f'[role="option"]:text-is("{day}日")').click()
                page.locator('[data-testid="primaryButton"]').click(timeout=5000)

            # 填充姓氏
            page.locator('#lastNameInput').type(lastname, delay=0.002 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)

            # 填充名字
            page.locator('#firstNameInput').fill(firstname, timeout=10000)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            # 点击 "下一步
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                self.bump_failure('ip_blocked')
                self._log("[Fail:IP] - 当前IP已被微软风控拦截，请更换IP重试")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                self.bump_failure('funcaptcha')
                self._log("[Fail:Captcha] - 验证码类型为FunCaptcha而非按压验证码，当前IP暂不支持，请换IP重试")
                return False

            # 策略 2：只自动填表到验证码界面，验证码 + 进邮箱 + OAuth 全部由你手动
            if self.captcha_strategy == 2:
                return self._hand_off_at_captcha(page, email, password)

            # 验证码是否通过
            captcha_result = self.handle_captcha(page)
            # 没有通过，报错
            if not captcha_result:
                raise TimeoutError

            # 验证码通过后：跳过辅助邮箱 / 通行密钥拦截，进入邮箱
            if self._enter_mailbox_after_register(page):
                self._log(f'Success:Captcha] - {email}{self.email_suffix} 验证码通过，已进入邮箱。')
            else:
                self._log(
                    f'Success:Captcha] - {email}{self.email_suffix} 验证码通过，但未确认进入邮箱（已尝试跳过/直达）。'
                )

        except Exception:
            self.bump_failure('captcha_fail', 'register_form_fail')
            self._log("[Fail:Captcha] - 验证码未通过（已达最大重试次数），请换IP后重新注册")
            return False

        # 走到这里说明验证码过了，注册成功
        self._log(f'Success:Email Registration] - {email}{self.email_suffix}: {password}')

        # 如果不需要oauth2，则直接结束，返回true
        if not self.enable_oauth2:
            return True

        # 邮箱初始化 + cookie/SSO 沉淀：进 OAuth 前固定多等几秒
        # 证据：过早跳 authorize 常落到 #i0116；重开浏览器更糟
        oauth_settle_ms = 7000
        try:
            page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
            self.log_event('REGISTER', 'INFO', 'mail_init', f'收件箱就绪，等待 {oauth_settle_ms}ms 沉淀 cookie')
            page.wait_for_timeout(oauth_settle_ms)
            return True
        except Exception:
            self.bump_failure('mail_init_fail')
            self.log_event(
                'REGISTER', 'WARN', 'mail_init',
                f'邮箱未初始化，仍等待 {oauth_settle_ms}ms 后继续 OAuth2',
            )
            try:
                page.wait_for_timeout(oauth_settle_ms)
            except Exception:
                pass
            return True

    def _is_mailbox_url(self, page):
        try:
            url = page.url or ''
        except Exception:
            return False
        if 'outlook.live.com/mail/' not in url:
            return False
        # 注册入口不算已进入邮箱
        if 'prompt=create_account' in url:
            return False
        return True

    def _click_if_visible(self, locator, timeout_ms=2500):
        try:
            target = locator.first
            if target.count() <= 0:
                return False
            if not target.is_visible():
                return False
            target.click(timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _try_bind_recovery_email(self, page):
        """保护帐户页：创建临时邮箱 → 填 #EmailAddress → 接码 → #iOttText。失败则调用方再 skip。"""
        if not self.bind_recovery_email:
            return False
        try:
            from controllers.recovery_bind import bind_recovery_email, is_protect_account_page, is_ott_code_page
        except Exception as exc:
            self.log_event('REGISTER', 'WARN', 'recovery', f'加载 recovery_bind 失败: {exc}')
            return False
        if not is_protect_account_page(page) and not is_ott_code_page(page):
            return False

        def _log(stage, message, level='INFO'):
            self.log_event('REGISTER', level, stage, message)

        result = bind_recovery_email(page, self.temp_mail_cfg, log=_log)
        # 兼容 (ok, session) 或旧版 bool
        if isinstance(result, tuple):
            ok, session = result[0], (result[1] if len(result) > 1 else None)
        else:
            ok, session = bool(result), None
        if ok:
            self.thread_local.recovery_email_bound = True
            self.thread_local.recovery_email_skipped = False
            if session:
                self.thread_local.recovery_mail_session = session
                self.log_event(
                    'REGISTER', 'INFO', 'recovery_session',
                    f"已保存辅助邮箱会话 addr={session.get('address')}",
                )
        else:
            self.bump_failure('recovery_bind_fail')
        return ok

    def _mark_recovery_skipped(self):
        """注册阶段未绑定、点了暂时跳过 → OAuth 仍可能再弹保护帐户页。"""
        if not getattr(self.thread_local, 'recovery_email_bound', False):
            self.thread_local.recovery_email_skipped = True

    def recovery_bind_status(self):
        """供 OAuth 判断：bound / skipped / session(address+jwt 冷登录接码用)。"""
        return {
            'bound': bool(getattr(self.thread_local, 'recovery_email_bound', False)),
            'skipped': bool(getattr(self.thread_local, 'recovery_email_skipped', False)),
            'session': getattr(self.thread_local, 'recovery_mail_session', None),
        }

    def _dismiss_post_register_intercepts(self, page):
        """注册成功后、进 mail/0 前：优先绑定辅助邮箱，再处理通行密钥。

        正常路径：验证码通过 →「让我们来保护你的帐户」→ 绑定临时邮箱+接码
        → 取消通行密钥 → mail/0。绑定成功后 OAuth 通常不再出现该页。
        """
        acted = False

        # 1) 「让我们来保护你的帐户」：主路径绑定；失败才暂时跳过
        try:
            from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
            on_protect = is_protect_account_page(page) or is_ott_code_page(page)
        except Exception:
            on_protect = page.locator('#EmailAddress').count() > 0 or page.locator('#iOttText').count() > 0

        if on_protect and self.bind_recovery_email:
            if self._try_bind_recovery_email(page):
                self.log_event(
                    'REGISTER', 'OK', 'recovery_bind',
                    '注册阶段备用邮箱绑定成功（OAuth 通常不再出现此页）',
                )
                acted = True
            else:
                if self._click_if_visible(page.locator('#iShowSkip')):
                    self._mark_recovery_skipped()
                    self.log_event(
                        'REGISTER', 'WARN', 'skip_recovery',
                        '注册阶段绑定失败，已 #iShowSkip；OAuth 仍可能再要求绑定',
                    )
                    acted = True
        elif on_protect or page.locator('#iShowSkip').count() > 0:
            # temp_mail.enabled=false 时：只跳过
            if self._click_if_visible(page.locator('#iShowSkip')):
                self._mark_recovery_skipped()
                self.log_event('REGISTER', 'INFO', 'skip_recovery', '已点击 #iShowSkip 暂时跳过辅助邮箱')
                acted = True
            else:
                for text in ('暂时跳过', 'Skip for now', 'Skip'):
                    try:
                        loc = page.get_by_role('link', name=text)
                        if self._click_if_visible(loc):
                            self._mark_recovery_skipped()
                            self.log_event('REGISTER', 'INFO', 'skip_recovery', f'已点击跳过链接: {text}')
                            acted = True
                            break
                    except Exception:
                        pass
                    try:
                        loc = page.get_by_text(text, exact=False)
                        if self._click_if_visible(loc):
                            self._mark_recovery_skipped()
                            self.log_event('REGISTER', 'INFO', 'skip_recovery', f'已点击跳过文案: {text}')
                            acted = True
                            break
                    except Exception:
                        pass

        # 2) Windows 通行密钥 / Hello：优先点「取消」#idBtn_Back
        passkey_hint = False
        try:
            body = (page.locator('body').inner_text(timeout=800) or '')[:1200]
            passkey_hint = any(
                k in body
                for k in (
                    '通行密钥', 'Windows Hello', 'passkey', 'Passkey',
                    '更快速地登录', 'face, fingerprint', 'security key',
                    '使用 Windows Hello', '创建通行密钥',
                )
            )
        except Exception:
            pass

        back = page.locator('#idBtn_Back')
        try:
            if back.count() > 0 and back.first.is_visible():
                value = ''
                try:
                    value = ((back.first.get_attribute('value') or '') + ' ' + (back.first.inner_text() or '')).strip()
                except Exception:
                    value = ''
                is_cancel = any(k in value for k in ('取消', 'Cancel', 'No', 'not now', 'Not now', '暂时不要'))
                # 仅在确认是通行密钥/Hello 页时点取消；避免保护帐户流程里误点「取消」
                if passkey_hint and is_cancel:
                    if self._click_if_visible(back):
                        self.log_event(
                            'REGISTER', 'INFO', 'skip_passkey',
                            f'已点击 #idBtn_Back value={value[:40]!r} passkey_hint={passkey_hint}',
                        )
                        acted = True
        except Exception:
            pass

        # 文本兜底
        if passkey_hint:
            for text in ('取消', 'Cancel', '暂时不要', 'Not now', 'Skip for now'):
                try:
                    if self._click_if_visible(page.get_by_role('button', name=text)):
                        self.log_event('REGISTER', 'INFO', 'skip_passkey', f'已点击按钮: {text}')
                        acted = True
                        break
                except Exception:
                    pass
                try:
                    if self._click_if_visible(page.locator(f'input[type="button"][value="{text}"]')):
                        self.log_event('REGISTER', 'INFO', 'skip_passkey', f'已点击 input: {text}')
                        acted = True
                        break
                except Exception:
                    pass

        return acted

    def _enter_mailbox_after_register(self, page, timeout_ms=45000):
        """验证码通过后进入邮箱。

        保护帐户/绑定辅助邮箱为**概率事件**（日志 2026-07-19 多批验证）：
          - 可能在注册后立刻出现 → 出现则绑定
          - 可能完全不出现 → 直接 mail/0 + OAuth（正常）
          - 也可能仅在 OAuth 中出现 → OAuth 侧再绑
        不因「未出现」而长时间空等。
        """
        mail_url = 'https://outlook.live.com/mail/0/'
        deadline = time.time() + timeout_ms / 1000.0
        force_count = 0
        # 短等：给拦截页一点渲染时间；不出现则继续
        protect_probe_deadline = time.time() + 5.0
        saw_protect = False

        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass

        while time.time() < deadline:
            try:
                from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
                on_protect = is_protect_account_page(page) or is_ott_code_page(page)
            except Exception:
                on_protect = (
                    page.locator('#EmailAddress').count() > 0
                    or page.locator('#iOttText').count() > 0
                    or page.locator('#iShowSkip').count() > 0
                )
            if on_protect:
                if not saw_protect:
                    self.log_event(
                        'REGISTER', 'INFO', 'recovery',
                        '检测到保护帐户页（概率出现），开始绑定辅助邮箱',
                    )
                saw_protect = True

            if self._dismiss_post_register_intercepts(page):
                page.wait_for_timeout(800)
                continue

            if self._is_mailbox_url(page) and not on_protect:
                if page.locator('#iShowSkip').count() == 0 and page.locator('#EmailAddress').count() == 0:
                    st = self.recovery_bind_status()
                    self.log_event(
                        'REGISTER', 'OK', 'mail_enter',
                        f'已在邮箱页 recovery_bound={st["bound"]} skipped={st["skipped"]} '
                        f'saw_protect={saw_protect} url={page.url}',
                    )
                    return True

            # 短探针窗口：仅多等几秒看是否弹出保护页
            if self.bind_recovery_email and not saw_protect and time.time() < protect_probe_deadline:
                page.wait_for_timeout(400)
                continue

            if force_count < 2:
                force_count += 1
                try:
                    self.log_event(
                        'REGISTER', 'INFO', 'mail_goto',
                        f'跳转邮箱({force_count}) saw_protect={saw_protect} {mail_url}',
                    )
                    page.goto(mail_url, timeout=25000, wait_until='domcontentloaded')
                    page.wait_for_timeout(1200)
                    self._dismiss_post_register_intercepts(page)
                    page.wait_for_timeout(600)
                    if self._is_mailbox_url(page):
                        if not self._dismiss_post_register_intercepts(page):
                            if page.locator('#EmailAddress').count() == 0 and page.locator('#iShowSkip').count() == 0:
                                st = self.recovery_bind_status()
                                self.log_event(
                                    'REGISTER', 'OK', 'mail_enter',
                                    f'直达邮箱 recovery_bound={st["bound"]} skipped={st["skipped"]} saw_protect={saw_protect}',
                                )
                                return True
                except Exception as exc:
                    self.log_event('REGISTER', 'WARN', 'mail_goto', f'跳转邮箱失败: {exc}')
                    page.wait_for_timeout(800)
            else:
                page.wait_for_timeout(500)

        try:
            page.goto(mail_url, timeout=20000, wait_until='domcontentloaded')
            self._dismiss_post_register_intercepts(page)
        except Exception:
            pass

        ok = self._is_mailbox_url(page)
        try:
            final_url = page.url
        except Exception:
            final_url = ''
        self.log_event(
            'REGISTER',
            'OK' if ok else 'WARN',
            'mail_enter',
            f'最终 url={final_url} ok={ok}',
        )
        return ok

    # ============================================================
    # 验证码入口
    # ============================================================
    def handle_captcha(self, page):
        """验证码入口。captcha_strategy: 0=全自动按压, 1=半自动(暂停等你手动按)"""
        if self.captcha_strategy == 1:
            return self._captcha_manual(page)
        return self._captcha_hold(page)

    def _captcha_manual(self, page):
        """半自动模式：程序暂停，轮询检测是否进入邮箱（最多5分钟），你手动按压验证码"""
        self.log_event('CAPTCHA', 'WARN', 'manual', '请手动完成验证码按压，等待进入邮箱...')
        for _ in range(300):
            page.wait_for_timeout(1000)
            try:
                if 'outlook.live.com/mail/0/' in page.url:
                    page.wait_for_timeout(2000)
                    self.log_event('CAPTCHA', 'OK', 'manual', '已进入邮箱！')
                    return True
            except Exception:
                pass
        self.log_event('CAPTCHA', 'FAIL', 'manual', '超时（5分钟），未进入邮箱。')
        return False

    # ============================================================
    # 全自动按压验证码
    # ============================================================
    def _captcha_hold(self, page):
        """全自动按压主循环：找目标 → 移动 → 按压 → 微颤 → 点按钮2 → 检查结果"""
        if not self._wait_for_captcha_frame(page):
            self.bump_failure('captcha_btn2_never_appeared')
            self.penalize_ip(penalty=4)
            self._log("未检测到验证码iframe")
            return False

        # 微软验证码是嵌套iframe结构
        frame1 = page.frame_locator('iframe[title="验证质询"]')
        frame2 = frame1.frame_locator('iframe[style*="display: block"]')
        self._human_prelude(page)
        btn2_seen = False

        for attempt in range(self.max_captcha_retries + 1):
            self._log(f"Hold {attempt+1}/{self.max_captcha_retries+1}")
            page.wait_for_timeout(random.randint(200, 600))

            # ① 在iframe中找到可点击的目标元素
            box, target_label = self._find_target(frame2, attempt)
            if not box:
                continue

            cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            # ② 选择按压位置（中心/边缘/角落/随机）
            pos_name, x, y = self._pick_position(box, cx, cy)
            self._log(f"target={target_label} pos={pos_name}")

            # ③ 从远处Bezier曲线移动到目标按钮
            from_x, from_y = x + random.uniform(-250, 250), y + random.uniform(-250, 250)
            page.mouse.move(from_x, from_y, steps=1)
            page.wait_for_timeout(random.randint(40, 150))
            self._natural_move(page, from_x, from_y, x, y)

            # ④ C:double-tap — 双击→松开→长按
            page.mouse.down(); page.wait_for_timeout(random.randint(25, 55))
            page.mouse.up();   page.wait_for_timeout(random.randint(80, 220))
            page.mouse.down(); page.wait_for_timeout(random.randint(25, 55))
            page.mouse.up();   page.wait_for_timeout(random.randint(120, 380))
            page.mouse.down()

            # ⑤ 按住并圆形微颤，等按钮2出现
            appeared = self._hold_and_wait(page, frame2, x, y)
            if not appeared:
                page.mouse.up()
                continue
            btn2_seen = True

            # ⑥ click或dblclick轻量偏置轮换
            bm = self._pick_b2mode()
            self._record_b2_attempt(bm)
            if not self._execute_b2(page, frame2, x, y, bm):
                continue

            # ⑦ 检查验证码是否通过
            success, retry = self._check_captcha_result(page, frame1, frame2)
            if not success:
                break
            if not retry:
                with self._state_lock:
                    OutlookController._attempts += 1
                    OutlookController._success += 1
                self._record_b2_success(bm)
                self._record_ip('win')
                self._print_stats()
                return True

        with self._state_lock:
            OutlookController._attempts += 1
        if btn2_seen:
            self.bump_failure('captcha_btn2_appeared_but_failed')
        else:
            self.bump_failure('captcha_btn2_never_appeared')
            self.penalize_ip(penalty=4)
        self._record_ip('loss')
        self._print_stats()
        return False

    def _record_ip(self, result):
        """记录本次运行中IP的表现（仅内存，不持久化）。result: 'win' 或 'loss'"""
        proxy = getattr(self.thread_local, '_proxy', '')
        key = proxy.split('//')[-1] if '//' in proxy else proxy
        if key:
            with self._state_lock:
                if key not in self._ip_tracker:
                    self._ip_tracker[key] = {'win': 0, 'total': 0}
                self._ip_tracker[key]['total'] += 1
                if result == 'win':
                    self._ip_tracker[key]['win'] += 1

    def _print_stats(self):
        """打印当前累计的验证码通过率"""
        with self._state_lock:
            a = max(OutlookController._attempts, 1)
            s = OutlookController._success
            b2_attempts = dict(OutlookController._b2_attempts)
            b2_success = dict(OutlookController._b2_success)
        b2_fragments = []
        for mode in ('click', 'dblclick'):
            attempts = b2_attempts.get(mode, 0)
            if attempts <= 0:
                continue
            wins = b2_success.get(mode, 0)
            rate = wins / attempts * 100
            b2_fragments.append(f"{mode}:{wins}/{attempts}={rate:.0f}%")
        suffix = f" | b2={' '.join(b2_fragments)}" if b2_fragments else ""
        self._log(f"[Stats] {s}/{a}={s / a * 100:.0f}%{suffix}")

    # ============================================================
    # iframe / 人类化 / 鼠标移动
    # ============================================================
    def _wait_for_captcha_frame(self, page):
        """轮询等待验证码iframe加载，最多15秒"""
        for _ in range(15):
            try:
                # 微软验证码嵌套iframe：外层title="验证质询"，内层style*="display:block"
                f1 = page.frame_locator('iframe[title="验证质询"]')
                if f1.locator('iframe').count() > 0:
                    f2 = f1.frame_locator('iframe[style*="display: block"]')  # 内层可见iframe
                    for sel in ['[aria-label="可访问性挑战"]', 'circle', 'svg', '[role="button"]']:
                        try:
                            cnt = f2.locator(sel).count()
                            if cnt > 0:
                                box = f2.locator(sel).first.bounding_box()
                                if box and box['width'] > 5:
                                    self._log(f"iframe就绪: {sel}")
                                    page.wait_for_timeout(random.randint(500, 1500))
                                    return True
                        except Exception: continue
            except Exception: pass
            page.wait_for_timeout(1000)
        return False

    def _human_prelude(self, page):
        """验证码前的随机行为：滚动、游荡、停顿、手抖，模拟真人操作"""
        for _ in range(random.randint(1, 4)):
            act = random.random()
            if act < 0.3:
                page.evaluate(f'window.scrollBy(0, {random.randint(-200, 200)})')
                page.wait_for_timeout(random.randint(200, 800))
            elif act < 0.5:
                page.mouse.move(random.randint(100, 600), random.randint(100, 500), steps=random.randint(3, 8))
                page.wait_for_timeout(random.randint(300, 1200))
            elif act < 0.75:
                page.wait_for_timeout(random.randint(500, 2500))
            else:
                try:
                    pos = page.evaluate('() => ({x: 400 + Math.random()*100, y: 300 + Math.random()*100})')
                    page.mouse.move(pos['x'], pos['y'], steps=1)
                except Exception:
                    pass
                page.wait_for_timeout(random.randint(100, 400))

    def _natural_move(self, page, x1, y1, x2, y2):
        """三段式人类鼠标轨迹：阶段1 Bezier加速接近(70%步数) → 阶段2 随机过冲 → 阶段3 微调修正"""
        # 控制点随机偏移，确保每次轨迹都不同
        cpx = (x1 + x2) / 2 + random.uniform(-150, 150)
        cpy = (y1 + y2) / 2 + random.uniform(-120, 120)
        # 阶段1: 加速接近 (ease-out 减速)
        steps1 = random.randint(8, 18)
        for i in range(steps1 + 1):
            t = i / steps1
            ease = 1 - (1 - t) ** 3
            px = (1 - ease) * x1 + ease * x2
            py = (1 - ease) * y1 + ease * y2
            bx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cpx + t ** 2 * x2
            py = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cpy + t ** 2 * y2
            px = px * 0.6 + bx * 0.4  # 混合线性进度 + Bezier弯曲
            page.mouse.move(px, py, steps=1)
            page.wait_for_timeout(random.randint(6, 18))
        # 阶段2: 过冲 (超过目标再回来，模拟手没停稳)
        if random.random() < 0.6:
            page.mouse.move(x2 + random.uniform(2, 8) * random.choice([-1, 1]),
                            y2 + random.uniform(2, 6) * random.choice([-1, 1]), steps=1)
            page.wait_for_timeout(random.randint(30, 80))
        # 阶段3: 修正到精确位置
        page.mouse.move(x2, y2, steps=1)
        page.wait_for_timeout(random.randint(20, 60))

    # ============================================================
    # 目标定位 / 位置 / 按压 / 微颤 / 按钮2
    # ============================================================
    def _find_target(self, frame2, attempt):
        """在验证码iframe中遍历候选选择器，找到尺寸>8px的第一个可见目标"""
        for sel in ['[aria-label="可访问性挑战"]', 'circle', 'ellipse',
                    'svg circle', 'svg ellipse', '[role="button"]', 'svg']:
            try:
                candidates = frame2.locator(sel)
                cnt = candidates.count()
                if cnt > 0:
                    box = candidates.nth(attempt % min(cnt, 3)).bounding_box()
                    if box and box['width'] > 8 and box['height'] > 8:
                        return box, f"{sel}[{attempt % min(cnt, 3)}/{cnt}]"
            except Exception: continue
        return None, ""

    def _pick_position(self, box, cx, cy):
        """在目标元素上随机选取按压点：中心12%、边缘18%、角落18%、随机偏移52%"""
        r = random.random()
        if r < 0.12:
            return "center", cx + random.uniform(-3, 3), cy + random.uniform(-3, 3)
        elif r < 0.30:
            e = random.choice(['t', 'b', 'l', 'r'])
            if e == 't':   return f"edge.{e}", cx + random.uniform(-box['width']*0.3, box['width']*0.3), box['y'] + random.uniform(1, 5)
            elif e == 'b': return f"edge.{e}", cx + random.uniform(-box['width']*0.3, box['width']*0.3), box['y']+box['height'] - random.uniform(1, 5)
            elif e == 'l': return f"edge.{e}", box['x'] + random.uniform(1, 5), cy + random.uniform(-box['height']*0.3, box['height']*0.3)
            else:          return f"edge.{e}", box['x']+box['width'] - random.uniform(1, 5), cy + random.uniform(-box['height']*0.3, box['height']*0.3)
        elif r < 0.48:
            c = random.choice(['tl', 'tr', 'bl', 'br'])
            if c == 'tl':   return f"corner.{c}", box['x'] + random.uniform(2, 8), box['y'] + random.uniform(2, 8)
            elif c == 'tr': return f"corner.{c}", box['x']+box['width'] - random.uniform(2, 8), box['y'] + random.uniform(2, 8)
            elif c == 'bl': return f"corner.{c}", box['x'] + random.uniform(2, 8), box['y']+box['height'] - random.uniform(2, 8)
            else:           return f"corner.{c}", box['x']+box['width'] - random.uniform(2, 8), box['y']+box['height'] - random.uniform(2, 8)
        else:
            return "random", cx + random.uniform(-box['width']*0.4, box['width']*0.4), cy + random.uniform(-box['height']*0.4, box['height']*0.4)

    def _hold_and_wait(self, page, frame2, x, y):
        """按住状态下圆形微颤，等待"再次按下"按钮出现。出现后延续按压1.5-4.5s"""
        self._circular_tremor(page, x, y, duration_ms=random.randint(600, 1800))
        appeared = False
        btn2_selectors = ['[aria-label="再次按下"]', '[aria-label*="再次"]', '[aria-label*="按下"]']
        for sel in btn2_selectors:
            try:
                frame2.locator(sel).wait_for(state='visible', timeout=10000)
                appeared = True
                break
            except Exception: continue
        if appeared:
            extra_ms = random.randint(1500, 4500)
            self._log(f"btn2出现, 延续{extra_ms}ms")
            self._circular_tremor(page, x, y, duration_ms=extra_ms)
        return appeared

    def _circular_tremor(self, page, x, y, duration_ms):
        """按住期间的圆周微颤，模拟手指自然颤抖"""
        steps = max(duration_ms // 50, 5)
        radius = random.uniform(0.3, 2.0)
        for i in range(steps):
            angle = 2 * math.pi * i / steps + random.uniform(-0.3, 0.3)
            tx = x + math.cos(angle) * radius * random.uniform(0.7, 1.3)
            ty = y + math.sin(angle) * radius * random.uniform(0.7, 1.3)
            page.mouse.move(tx, ty, steps=1)
            page.wait_for_timeout(random.randint(35, 70))

    def _pick_b2mode(self):
        """轻量延续旧版策略：保留探索，但优先当前运行中表现更好的btn2模式。"""
        with self._state_lock:
            attempts = dict(OutlookController._b2_attempts)
            wins = dict(OutlookController._b2_success)
        weights = {}
        for mode in ('click', 'dblclick'):
            attempted = attempts.get(mode, 0)
            success = wins.get(mode, 0)
            if attempted >= 10:
                rate = success / max(attempted, 1)
                weights[mode] = rate ** 2 * 10 if rate >= 0.30 else max(0.05, rate)
            elif attempted >= 5:
                weights[mode] = max(0.1, success / max(attempted, 1))
            else:
                weights[mode] = 1.0
        return random.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

    def _record_b2_attempt(self, mode):
        with self._state_lock:
            OutlookController._b2_attempts[mode] = OutlookController._b2_attempts.get(mode, 0) + 1

    def _record_b2_success(self, mode):
        with self._state_lock:
            OutlookController._b2_success[mode] = OutlookController._b2_success.get(mode, 0) + 1

    def _execute_b2(self, page, frame2, x, y, bm):
        """操作按钮2：定位 → 移动 → click或dblclick"""
        page.wait_for_timeout(random.randint(300, 900))
        btn2_selectors = ['[aria-label="再次按下"]', '[aria-label*="再次"]', '[aria-label*="按下"]']
        btn2_box = None
        for sel in btn2_selectors:
            try:
                btn2_box = frame2.locator(sel).bounding_box()
                if btn2_box: break
            except Exception: continue
        if not btn2_box:
            return False
        # 在按钮2上随机偏移点击位置
        b2cx, b2cy = btn2_box['x']+btn2_box['width']/2, btn2_box['y']+btn2_box['height']/2
        x2 = b2cx + random.uniform(-btn2_box['width']*0.35, btn2_box['width']*0.35)
        y2 = b2cy + random.uniform(-btn2_box['height']*0.35, btn2_box['height']*0.35)
        page.mouse.move(x2, y2, steps=random.randint(3, 10))
        page.wait_for_timeout(random.randint(50, 180))
        if bm == "dblclick":
            page.mouse.click(x2, y2)
            page.wait_for_timeout(random.randint(80, 200))
            page.mouse.click(x2 + random.uniform(-3, 3), y2 + random.uniform(-3, 3))
        else:
            page.mouse.click(x2, y2)
        return True

    def _check_captcha_result(self, page, frame1, frame2):
        """检测验证码结果。返回 (success, retry):
        - (True, False): 通过
        - (True, True): 需重试
        - (False, False): 失败/IP被封
        """
        try:
            page.locator('.draw').wait_for(state="detached")  # 等待加载动画消失
            try:
                page.locator('[role="status"][aria-label="正在加载..."]').wait_for(timeout=5000)
                page.wait_for_timeout(8000)
                if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护').count() > 0:
                    return False, False  # IP被风控
                if frame2.locator('[aria-label="可访问性挑战"]').count() > 0:
                    return True, True    # 验证码重置，需要重试
                return True, False        # 验证码通过
            except Exception:
                if page.get_by_text('取消').count() > 0:
                    return True, False    # 取消按钮出现 → 通过
                frame1.get_by_text("请再试一次").wait_for(timeout=15000)  # 提示重试
                return True, True
        except Exception:
            if page.get_by_text('取消').count() > 0:
                return True, False
            return False, False           # .draw未消失 → 失败
