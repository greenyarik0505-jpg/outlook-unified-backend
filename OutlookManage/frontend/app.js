const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let ALL_ACCOUNTS = [];
let SELECTED = new Set();
let logTimer = null;
let jobTimer = null;
let SORT = { key: 'id', dir: 'desc' };
let PAGE = { page: 1, size: 50, total: 0, pages: 1 };
let accountRefreshTimer = null;
let CURRENT_JOB_ID = null;
let DISPLAYED_JOB_ID = null;
const LAST_RECOVER_DEBUG = new Map();
const REMOTE_READY = ['synced', 'imported', 'exists'];
let APPLIED_ALIVE_THRESHOLD = { years: 0, months: 0, days: 7 };
let accountRequestController = null;
let accountRequestSequence = 0;
let logsLoading = false;
let jobsOverviewLoading = false;

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || data.reason || data.error || `HTTP ${res.status}`);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

const pct = (value, total) => total > 0 ? Math.max(0, Math.min(100, Math.round((value / total) * 100))) : 0;

function toast(msg, ms = 2800) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function chip(value) {
  const raw = value || '-';
  const lower = String(raw).toLowerCase();
  let cls = '';
  if (lower === 'ok' || lower === 'all' || lower === 'healthy' || lower === 'imported' || lower === 'synced' || lower === 'exists' || lower === 'refresh_ok') cls = 'ok';
  else if (lower === 'banned' || lower === 'fail' || lower === 'token_invalid' || lower === 'other_error' || lower === 'refresh_fail' || lower === 'proto_error') cls = 'fail';
  else if (lower === '-' || lower === '' || lower === 'new' || raw === '未测试') cls = 'muted';
  else cls = 'warn';
  return `<span class="chip ${cls}">${esc(raw)}</span>`;
}

function isBannedAccount(item) {
  // 与后端 is_banned_row 一致：health_status 或 health_severity 任一为 banned
  return item.health_status === 'banned' || item.health_severity === 'banned';
}

function abuseCandidate(item) {
  return isBannedAccount(item);
}

function canShowRecoverAbuse(item) {
  return isBannedAccount(item);
}

function sevClass(sev) {
  if (sev === 'banned' || sev === 'fail') return 'fail';
  if (sev === 'warn') return 'warn';
  if (sev === 'ok') return 'ok';
  return 'muted';
}

function isNormalAccount(item) {
  return item.graph_status === 'ok' || item.imap_status === 'ok' || item.pop_status === 'ok';
}

function isRemoteSynced(item) {
  return isNormalAccount(item) && REMOTE_READY.includes(item.remote_sync_status);
}

function isNeverSynced(item) {
  return isNormalAccount(item) && !REMOTE_READY.includes(item.remote_sync_status);
}

function isUntestedAccount(item) {
  return !item.health_status && !item.last_protocol_test_at && !item.error_detail;
}

function isOtherErrorAccount(item) {
  if (isNormalAccount(item) || isBannedAccount(item) || isUntestedAccount(item)) return false;
  return item.health_status === 'other_error'
    || item.health_status === 'token_invalid'
    || item.status === 'proto_error'
    || !!item.error_detail;
}

function formatDateTime(value) {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function getAliveDurationMs(item) {
  if (!item?.registered_at) return null;
  const start = new Date(item.registered_at);
  if (Number.isNaN(start.getTime())) return null;
  const endBase = isNormalAccount(item) ? new Date() : (item.last_alive_at ? new Date(item.last_alive_at) : null);
  if (!endBase || Number.isNaN(endBase.getTime()) || endBase <= start) return null;
  return endBase.getTime() - start.getTime();
}

function formatAliveDuration(item) {
  const durationMs = getAliveDurationMs(item);
  if (durationMs == null) return '-';
  let seconds = Math.floor(durationMs / 1000);
  const days = Math.floor(seconds / 86400);
  seconds -= days * 86400;
  const hours = Math.floor(seconds / 3600);
  seconds -= hours * 3600;
  const minutes = Math.floor(seconds / 60);
  const parts = [];
  if (days) parts.push(`${days}天`);
  if (hours || days) parts.push(`${hours}小时`);
  parts.push(`${minutes}分钟`);
  return parts.join('');
}

function getAliveThresholdInputs() {
  const years = Math.max(0, parseInt($('#alive-years')?.value || '0', 10) || 0);
  const months = Math.max(0, parseInt($('#alive-months')?.value || '0', 10) || 0);
  const days = Math.max(0, parseInt($('#alive-days')?.value || '0', 10) || 0);
  return { years, months, days };
}

function getAliveThresholdMs() {
  const { years, months, days } = APPLIED_ALIVE_THRESHOLD;
  const totalDays = years * 365 + months * 30 + days;
  return totalDays * 86400 * 1000;
}

function isAliveOverThreshold(item) {
  const durationMs = getAliveDurationMs(item);
  return durationMs != null && durationMs > getAliveThresholdMs();
}

function syncAliveThresholdInputs() {
  $('#alive-years').value = String(APPLIED_ALIVE_THRESHOLD.years);
  $('#alive-months').value = String(APPLIED_ALIVE_THRESHOLD.months);
  $('#alive-days').value = String(APPLIED_ALIVE_THRESHOLD.days);
}

async function updateAliveThresholdSummary() {
  const el = $('#alive-threshold-count');
  if (!el) return;
  try {
    const params = accountQuery({ filter: 'alive_over_threshold', page: 1, page_size: 1 });
    const data = await api(`/api/accounts?${params}`);
    el.textContent = String(data.total || 0);
  } catch (_) { el.textContent = '-'; }
}

function applyAliveThreshold(showToast = true) {
  APPLIED_ALIVE_THRESHOLD = getAliveThresholdInputs();
  PAGE.page = 1;
  loadAccounts().catch((e) => toast(e.message, 4200));
  updateAliveThresholdSummary();
  if (showToast) toast('存活时间阈值已应用');
}

function resetAliveThreshold() {
  APPLIED_ALIVE_THRESHOLD = { years: 0, months: 0, days: 7 };
  syncAliveThresholdInputs();
  updateAliveThresholdSummary();
  PAGE.page = 1;
  loadAccounts().catch((e) => toast(e.message, 4200));
  toast('已重置为 7 天');
}

function applyStatsToUi(stats) {
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  const s = {
    total: stats.total ?? stats.total_accounts ?? 0,
    normal: stats.normal ?? stats.healthy ?? 0,
    banned: stats.banned ?? 0,
    otherError: stats.other_error ?? stats.otherError ?? 0,
    synced: stats.synced ?? stats.remote_ready ?? 0,
    untested: stats.untested ?? 0,
    graph: stats.graph ?? 0,
    imapPop: stats.imap_pop ?? stats.imapPop ?? 0,
    neverSynced: stats.never_synced ?? stats.neverSynced ?? 0,
  };
  set('#stat-total', s.total);
  set('#stat-normal', s.normal);
  set('#stat-banned', s.banned);
  set('#stat-other-error', s.otherError);
  set('#stat-synced', s.synced);
  set('#stat-untested', s.untested);
  set('#stat-graph', s.graph);
  set('#stat-imap-pop', s.imapPop);
  set('#stat-risk', s.banned + s.otherError);
  set('#sidebar-total', s.total);
  set('#sidebar-normal', s.normal);
  set('#donut-total', s.total);
  set('#legend-normal', s.normal);
  set('#legend-banned', s.banned);
  set('#legend-error', s.otherError);
  set('#normal-rate', `可用率 ${pct(s.normal, s.total)}%`);
  set('#sync-rate', `覆盖率 ${pct(s.synced, s.normal)}%`);
  set('#coverage-sync-label', `${s.synced} / ${s.normal}`);
  const segments = [s.normal, s.banned, s.otherError, s.untested];
  const segmentTotal = Math.max(1, segments.reduce((sum, value) => sum + value, 0));
  const colors = ['var(--green)', 'var(--red)', '#e69a35', '#d3dae4'];
  const stops = [];
  let offset = 0;
  segments.forEach((value, index) => {
    const end = offset + (value / segmentTotal) * 100;
    stops.push(`${colors[index]} ${offset}% ${end}%`);
    offset = end;
  });
  const donut = $('#health-donut');
  if (donut) donut.style.background = `conic-gradient(${stops.join(',')})`;
  const widths = {
    '#coverage-sync': pct(s.synced, s.normal),
    '#coverage-graph': pct(s.graph, s.total),
    '#coverage-imap': pct(s.imapPop, s.total),
  };
  Object.entries(widths).forEach(([selector, width]) => {
    const el = $(selector);
    if (el) el.style.width = `${width}%`;
  });
  renderWorkflow(s);
  return s;
}

async function loadStatus() {
  try {
    const d = await api('/api/status');
    if (d.summary) applyStatsToUi(d.summary);
  } catch (_) { /* 忽略，等账号列表回填 */ }
}

function renderWorkflow(stats) {
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#flow-total', `${stats.total} 个账号`);
  set('#flow-untested', `${stats.untested} 个未测试`);
  set('#flow-unsynced', `${stats.neverSynced} 个未上传`);
  set('#flow-maintain', `${stats.banned + stats.otherError} 个需关注`);
  const desc = $('#next-action-desc');
  const btn = $('#next-action-btn');
  if (!desc || !btn) return;
  if (stats.total === 0) {
    desc.textContent = '当前没有账号，先在工作台导入账号或加载 oauth2.txt。';
    btn.textContent = '去工作台导入';
    btn.dataset.target = '#workspace';
    btn.dataset.action = '';
  } else if (stats.untested > 0) {
    desc.textContent = `还有 ${stats.untested} 个账号未测试，建议在工作台先完成协议测试。`;
    btn.textContent = '测试未测试';
    btn.dataset.target = '#workspace';
    btn.dataset.action = 'test-untested';
  } else if (stats.neverSynced > 0) {
    desc.textContent = `有 ${stats.neverSynced} 个可用账号尚未上传远程，建议一键同步未上传。`;
    btn.textContent = '同步未上传';
    btn.dataset.target = '#workspace';
    btn.dataset.action = 'sync-never-synced';
  } else {
    desc.textContent = '当前没有明显待办。可在账号池维护，或到导出页按条件出货。';
    btn.textContent = '打开导出';
    btn.dataset.target = '#export';
    btn.dataset.action = '';
  }
}

function renderProtocolStatus(it) {
  return `<div class="protocol-stack">
    <span>Graph ${chip(it.graph_status)}</span>
    <span>IMAP ${chip(it.imap_status)}</span>
    <span>POP ${chip(it.pop_status)}</span>
    <span>SMTP ${chip(it.smtp_status)}</span>
  </div>`;
}

function renderHealthStatus(it, reasonHtml) {
  return `<div class="status-stack">
    ${chip(it.health_status || '未测试')}
    <div class="reason-cell">${reasonHtml}</div>
  </div>`;
}

function renderRegisteredAlive(it) {
  return `<div class="status-stack">
    <span class="nowrap">${esc(formatDateTime(it.registered_at))}</span>
    <small class="muted">${esc(formatAliveDuration(it))}</small>
  </div>`;
}

function renderTable() {
  const pageRows = ALL_ACCOUNTS;
  const total = PAGE.total;
  const pages = PAGE.pages;
  if (PAGE.page < 1) PAGE.page = 1;
  $('#account-table').innerHTML = pageRows.length ? pageRows.map((it) => {
    const reason = it.ban_reason || it.error_detail || '';
    const reasonHtml = reason ? `<span class="reason ${sevClass(it.health_severity)}">${esc(reason)}</span>` : '<span class="muted">-</span>';
    const checked = SELECTED.has(it.id) ? 'checked' : '';
    const recoverBtn = canShowRecoverAbuse(it)
      ? `<button class="action-link" data-act="recover" data-id="${it.id}">恢复ABUSE</button>`
      : '';
    return `<tr data-id="${it.id}">
      <td><input type="checkbox" class="row-check" data-id="${it.id}" ${checked}></td>
      <td>${it.id}</td>
      <td class="email-cell" data-id="${it.id}">${esc(it.email)}</td>
      <td>${renderHealthStatus(it, reasonHtml)}</td>
      <td>${renderProtocolStatus(it)}</td>
      <td>${renderRegisteredAlive(it)}</td>
      <td class="nowrap">${esc(it.last_protocol_test_at || '-')}</td>
      <td>${chip(it.remote_sync_status)}</td>
      <td>
        <div class="action-group">
          <button class="action-link" data-act="refresh" data-id="${it.id}">刷新</button>
          <button class="action-link" data-act="protocol" data-id="${it.id}">测试</button>
          ${recoverBtn}
          <button class="action-link" data-act="copy2" data-id="${it.id}">复制账密</button>
          <button class="action-link" data-act="copy4" data-id="${it.id}">复制全部</button>
        </div>
      </td>
    </tr>`;
  }).join('') : '<tr><td colspan="9" class="empty-state"><b>暂无账号</b><span>下一步：到工作台导入账号或加载 oauth2.txt。</span><button class="btn sm primary" data-empty-jump="#workspace">去工作台</button></td></tr>';
  $('#page-info').textContent = `${PAGE.page} / ${pages}`;
  $('#table-summary').textContent = `筛选结果 ${total} 个 · 本页 ${pageRows.length} · 已选 ${SELECTED.size}`;
  $('#selected-count').textContent = `已选 ${SELECTED.size}`;
  document.querySelectorAll('th.sortable').forEach((th) => {
    th.querySelector('.sort-ind').textContent = th.dataset.sort === SORT.key ? (SORT.dir === 'asc' ? '↑' : '↓') : '↕';
  });
  $('#select-all').checked = pageRows.length > 0 && pageRows.every((it) => SELECTED.has(it.id));
}

function aliveDays() {
  const { years, months, days } = APPLIED_ALIVE_THRESHOLD;
  return years * 365 + months * 30 + days;
}

function accountQuery(overrides = {}) {
  const values = {
    page: PAGE.page,
    page_size: PAGE.size,
    search: $('#search-input')?.value.trim() || '',
    filter: $('#filter-select')?.value || 'all',
    domain: $('#domain-select')?.value || '',
    sort: SORT.key,
    dir: SORT.dir,
    alive_days: aliveDays(),
    ...overrides,
  };
  return new URLSearchParams(values).toString();
}

async function loadAccounts() {
  const sequence = ++accountRequestSequence;
  if (accountRequestController) accountRequestController.abort();
  accountRequestController = new AbortController();
  try {
    const d = await api(`/api/accounts?${accountQuery()}`, { signal: accountRequestController.signal });
    if (sequence !== accountRequestSequence) return;
    ALL_ACCOUNTS = d.items;
    PAGE.page = d.page;
    PAGE.total = d.total;
    PAGE.pages = d.pages;
    renderTable();
  } catch (error) {
    if (error.name !== 'AbortError') throw error;
  } finally {
    if (sequence === accountRequestSequence) accountRequestController = null;
  }
}

async function loadLogs() {
  if (logsLoading) return;
  logsLoading = true;
  try {
    const d = await api('/api/logs');
    const el = $('#log-output');
    el.innerHTML = d.lines.length ? d.lines.map((l) => {
      let cls = 'log-info';
      if (/\[FAIL\]|失败|错误/.test(l)) cls = 'log-fail';
      else if (/\[WARN\]/.test(l)) cls = 'log-warn';
      else if (/\[OK\]|成功|完成/.test(l)) cls = 'log-ok';
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join('\n') : '暂无日志';
    el.scrollTop = el.scrollHeight;
  } finally {
    logsLoading = false;
  }
}

function scheduleLogPoll(delay = 4000) {
  clearTimeout(logTimer);
  const enabled = $('#auto-log')?.checked;
  if (!enabled || document.hidden || location.hash !== '#logs') return;
  logTimer = setTimeout(async () => {
    try { await loadLogs(); } catch (_) { /* 下一轮继续 */ }
    scheduleLogPoll(4000);
  }, delay);
}

function scheduleAccountRefresh(delay = 10000) {
  clearTimeout(accountRefreshTimer);
  if (document.hidden) return;
  accountRefreshTimer = setTimeout(async () => {
    if (location.hash === '#accounts') {
      try { await loadAccounts(); } catch (_) { /* 下一轮继续 */ }
    }
    scheduleAccountRefresh(10000);
  }, delay);
}

function jobStateLabel(job) {
  if (job.export_partial_ready) return ['可部分导出', 'warn'];
  if (job.export_resume_ready) return ['待继续删除', 'warn'];
  if (job.state === 'running') return ['运行中', 'special'];
  if (job.state === 'done') return ['已完成', 'ok'];
  if (job.state === 'failed') return ['失败', 'fail'];
  return ['已取消', 'muted'];
}

async function loadJobOverview() {
  if (jobsOverviewLoading) return;
  jobsOverviewLoading = true;
  try {
    const data = await api('/api/jobs');
    const items = data.jobs || [];
    const active = items.filter((job) => job.state === 'running').length;
    $('#sidebar-active-jobs').textContent = String(active);
    $('#active-job-count').textContent = `${active} 个运行中`;
    const body = $('#dashboard-jobs');
    if (!body) return;
    body.innerHTML = items.slice(0, 8).map((job) => {
      const [label, cls] = jobStateLabel(job);
      const progress = job.type === 'export' ? exportProgress(job) : pct(job.processed || 0, job.total || 0);
      return `<tr><td><b>${esc(JOB_NAME[job.type] || job.type || '后台任务')}</b></td>`
        + `<td><span class="chip ${cls}">${label}</span></td>`
        + `<td>${progress == null ? '-' : `${progress}%`}</td>`
        + `<td class="nowrap">${esc(job.started_at || '-')}</td>`
        + `<td><button class="job-focus-btn" data-job-focus="${esc(job.id)}">查看</button></td></tr>`;
    }).join('') || '<tr><td colspan="5" class="empty-state">暂无任务</td></tr>';
  } finally {
    jobsOverviewLoading = false;
  }
}

async function refreshAll() {
  const calls = [loadStatus(), loadAccounts(), loadJobOverview()];
  if (location.hash === '#logs') calls.push(loadLogs());
  const results = await Promise.allSettled(calls);
  const failed = results.find((result) => result.status === 'rejected');
  if (failed) throw failed.reason;
}

// ---------- 导入 ----------
async function doPreview() {
  const text = $('#account-input').value.trim();
  if (!text) return toast('请输入账号');
  const d = await api('/api/accounts/import-preview', { method: 'POST', body: JSON.stringify({ text }) });
  $('#import-result').innerHTML = `预览：有效 <b>${d.valid}</b> 行 · 新增 <b>${d.new}</b> · 覆盖 <b>${d.overwrite}</b> · 输入内重复 ${d.dup_in_input} · <span class="${d.error_count ? 'fail' : ''}">错误 ${d.error_count}</span>`
    + (d.errors.length ? `<div class="err-list">${d.errors.map((e) => `第${e.line_no}行: ${esc(e.error)}`).join('<br>')}</div>` : '');
}

function importErrorBreakdown(errors, errorCount) {
  if (!errorCount) return '';
  const counts = {};
  (errors || []).forEach((e) => { counts[e.error] = (counts[e.error] || 0) + 1; });
  const lines = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([r, c]) => `<div class="reason-line"><span>${esc(r)}</span><em class="fail">${c}</em></div>`).join('');
  return `<div class="reason-break"><b class="fail">错误明细（共 ${errorCount}）</b>${lines}</div>`;
}

async function doImport() {
  const text = $('#account-input').value.trim();
  if (!text) return toast('请输入账号');
  const d = await api('/api/accounts/import-text', { method: 'POST', body: JSON.stringify({ text }) });
  $('#import-result').innerHTML = `导入完成：<b>新增 ${d.inserted}</b> · 覆盖 ${d.updated} · 总有效 ${d.inserted + d.updated} · 错误 ${d.error_count}`
    + importErrorBreakdown(d.errors, d.error_count);
  toast('导入完成，建议执行一键测试未测试');
  await refreshAll();
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append('file', file);
  const d = await api('/api/accounts/import-file', { method: 'POST', body: fd });
  $('#import-result').innerHTML = `文件导入：<b>新增 ${d.inserted}</b> · 覆盖 ${d.updated} · 总有效 ${d.inserted + d.updated} · 错误 ${d.error_count}`
    + importErrorBreakdown(d.errors, d.error_count);
  toast('文件导入完成，建议执行一键测试未测试');
  await refreshAll();
}

// ---------- 单账号操作 ----------
function copyText(text) {
  const done = () => toast('已复制到剪贴板');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败'); }
  ta.remove();
}
async function copyAccount(id, full) {
  // 列表不再带 password/refresh_token，复制完整行时走详情接口
  try {
    if (!full) {
      const a = ALL_ACCOUNTS.find((x) => x.id === id);
      if (!a) return;
      // 仅邮箱+密码也需详情（列表无 password）
      const d = await api(`/api/accounts/${id}/detail`);
      const acc = d.account || {};
      copyText(`${acc.email}----${acc.password || ''}`);
      return;
    }
    const d = await api(`/api/accounts/${id}/detail`);
    const acc = d.account || {};
    copyText(`${acc.email}----${acc.password || ''}----${acc.client_id || ''}----${acc.refresh_token || ''}`);
  } catch (e) {
    toast(e.message || '复制失败', 4200);
  }
}

async function rowAction(act, id) {
  if (act === 'detail') return openDrawer(id);
  if (act === 'copy2') return void copyAccount(id, false);
  if (act === 'copy4') return void copyAccount(id, true);
  const actionLabel = act === 'refresh' ? '刷新中' : act === 'protocol' ? '测试中' : '恢复中';
  toast(`账号 #${id} ${actionLabel}…`, 4000);
  try {
    const path = act === 'refresh'
      ? `/api/accounts/${id}/refresh`
      : act === 'protocol'
        ? `/api/accounts/${id}/protocol-test`
        : `/api/accounts/${id}/recover-abuse`;
    const d = await api(path, { method: 'POST' });
    if (act === 'recover') {
      if (d.debug_log) LAST_RECOVER_DEBUG.set(id, d.debug_log);
      else LAST_RECOVER_DEBUG.delete(id);
    }
    const msg = d.success
      ? (d.reason || d.health || '完成')
      : `失败：${d.reason || ''}${d.debug_log ? ` | 日志：${d.debug_log}` : ''}`;
    toast(msg, 5200);
    if (act === 'recover') {
      await refreshAll();
      await openDrawer(id);
      return;
    }
  } catch (e) {
    const debugLog = e?.payload?.debug_log || '';
    if (act === 'recover') {
      if (debugLog) LAST_RECOVER_DEBUG.set(id, debugLog);
      else LAST_RECOVER_DEBUG.delete(id);
    }
    toast(`失败：${e.message}${debugLog ? ` | 日志：${debugLog}` : ''}`, 7000);
    if (act === 'recover') {
      await refreshAll();
      await openDrawer(id);
      return;
    }
  }
  await refreshAll();
}

// ---------- 批量 ----------
const BATCH_API = {
  refresh: '/api/accounts/batch/refresh',
  protocol: '/api/accounts/batch/protocol',
  'recover-abuse': '/api/accounts/batch/recover-abuse',
  'remote-import': '/api/remote/import',
};
const BATCH_NAME = { refresh: '批量刷新', protocol: '批量协议测试', 'recover-abuse': '批量恢复 ABUSE', 'remote-import': '同步到远程' };
const JOB_NAME = {
  refresh: '批量刷新',
  protocol: '批量协议测试',
  'recover-abuse': '批量恢复 ABUSE',
  'remote-import': '远程同步',
  delete: '批量删除',
};

const PAGE_IDS = ['overview', 'workspace', 'accounts', 'export', 'config', 'logs'];
// 旧 hash 兼容
const PAGE_ALIASES = {
  import: 'workspace',
  tasks: 'workspace',
};

function switchPage(hash) {
  let id = String(hash || '#overview').replace(/^#/, '');
  if (PAGE_ALIASES[id]) id = PAGE_ALIASES[id];
  if (!PAGE_IDS.includes(id)) id = 'overview';
  PAGE_IDS.forEach((pid) => {
    const el = document.getElementById(pid);
    if (el) el.classList.toggle('active', pid === id);
  });
  document.querySelectorAll('.nav a[href^="#"]').forEach((a) => {
    const href = (a.getAttribute('href') || '').replace(/^#/, '');
    a.classList.toggle('active', href === id);
  });
  document.body.classList.remove('sidebar-open');
  mobileNavOpen = false;
  if (location.hash !== `#${id}`) {
    try { history.replaceState(null, '', `#${id}`); } catch (_) { location.hash = id; }
  }
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' }));
  scheduleLogPoll(id === 'logs' ? 0 : 4000);
  if (id === 'overview') loadJobOverview().catch(() => {});
  return id;
}

function scrollToSection(target) {
  const hash = String(target || '');
  if (hash.startsWith('#')) {
    switchPage(hash);
    return;
  }
  const el = document.querySelector(target);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function runWorkflowAction(action) {
  if (action === 'test-untested') return $('#test-untested-btn')?.click();
  if (action === 'sync-never-synced') return $('#sync-never-synced-btn')?.click();
  return null;
}

async function startBatch(type, all) {
  const ids = all ? null : Array.from(SELECTED);
  if (!all && ids.length === 0) return toast('请先勾选账号');
  const concurrency = parseInt($('#concurrency').value, 10) || 8;
  try {
    const d = await api(BATCH_API[type], { method: 'POST', body: JSON.stringify({ ids, concurrency }) });
    toast(`${BATCH_NAME[type]} 已启动，共 ${d.total} 个，并发 ${d.concurrency}`);
    pollJob(d.job_id, BATCH_NAME[type]);
  } catch (e) {
    toast(`启动失败：${e.message}`, 4200);
  }
}

function renderReasons(reasons) {
  if (!reasons) return '';
  const blocks = [];
  const labels = { ok: ['成功明细', 'ok'], skip: ['跳过明细', 'warn'], fail: ['失败原因', 'fail'] };
  for (const st of ['fail', 'skip', 'ok']) {
    const m = reasons[st];
    if (!m || Object.keys(m).length === 0) continue;
    const [label, cls] = labels[st];
    const entries = Object.entries(m).sort((a, b) => b[1] - a[1]);
    blocks.push(`<div class="reason-break"><b class="${cls}">${label}</b>` +
      entries.map(([r, c]) => `<div class="reason-line"><span>${esc(r)}</span><em class="${cls}">${c}</em></div>`).join('') +
      `</div>`);
  }
  return blocks.join('');
}

function exportTarget(j) {
  const requested = Number(j.export_requested_count) || 0;
  if (requested > 0) return requested;
  const stored = parseInt(sessionStorage.getItem(`export-target:${j.id}`), 10) || 0;
  if (stored > 0) return stored;
  const input = $('#export-count');
  const entered = parseInt(input?.value, 10) || 0;
  if (input && input.value !== input.defaultValue && entered > 0) return entered;
  return Number(j.export_count) || 0;
}

function exportProgress(j) {
  if (j.export_phase === 'deleting' && j.export_count) {
    return Math.min(100, Math.round(((j.export_deleted || 0) / j.export_count) * 100));
  }
  if (j.state === 'done') return 100;
  const target = exportTarget(j);
  return target ? Math.min(100, Math.round(((j.succeeded || 0) / target) * 100)) : null;
}

function renderExportActions(j) {
  const wrap = $('#job-export-actions');
  const partialBtn = $('#export-partial-btn');
  const resumeBtn = $('#export-resume-btn');
  const downloadBtn = $('#export-download-btn');
  const retryRemoteBtn = $('#export-retry-remote-btn');
  const deleteFileBtn = $('#export-delete-file-btn');
  const discardBtn = $('#export-discard-btn');
  const isExport = j.type === 'export';
  DISPLAYED_JOB_ID = isExport ? j.id : null;
  wrap.classList.toggle('hidden', !isExport || !(
    j.export_partial_ready || j.export_resume_ready || j.export_ready || j.export_remote_failed
  ));
  partialBtn.classList.toggle('hidden', !j.export_partial_ready);
  resumeBtn.classList.toggle('hidden', !j.export_resume_ready);
  downloadBtn.classList.toggle('hidden', !j.export_ready);
  retryRemoteBtn.classList.toggle('hidden', !(j.export_remote_failed > 0));
  deleteFileBtn.classList.toggle('hidden', !j.export_ready);
  discardBtn.classList.toggle('hidden', !j.export_partial_ready);
  if (j.export_partial_ready) {
    partialBtn.textContent = `导出已通过的 ${j.export_partial_count || j.succeeded || 0} 个`;
  }
  $('#job-cancel-btn').classList.toggle('hidden', j.state !== 'running');
}

function renderJobStats(j) {
  if (j.type !== 'export') {
    return `<div class="job-counts">总数 ${j.total} · 处理 ${j.processed} · <span class="ok">成功 ${j.succeeded}</span> · <span class="fail">失败 ${j.failed}</span> · <span class="warn">跳过 ${j.skipped}</span></div>`;
  }
  const target = exportTarget(j);
  const abnormal = (j.failed || 0) + (j.export_not_alive || 0);
  const targetText = target || '同步中';
  const lead = j.state === 'done'
    ? `<span class="ok">已导出 ${j.export_count || j.succeeded || 0}</span>`
    : `目标 ${targetText} · <span class="ok">可导出 ${j.succeeded || 0}</span>`;
  return `<div class="job-counts">${lead} · <span class="warn">Abuse ${j.export_abused || 0}</span> · <span class="fail">异常 ${abnormal}</span> · 已删除 ${j.export_deleted || 0}</div>`;
}

function pollJob(jobId, title, autoDownload = false, onComplete = null) {
  const panel = $('#job-panel');
  panel.classList.remove('hidden');
  $('#job-title').textContent = title;
  $('#job-state').textContent = '已启动';
  $('#job-bar').style.width = '0%';
  $('#job-stats').innerHTML = '<div class="job-counts">任务已启动，等待首批结果…</div>';
  CURRENT_JOB_ID = jobId;
  clearTimeout(jobTimer);
  let stopped = false;
  const tick = async () => {
    try {
      const d = await api(`/api/jobs/${jobId}`);
      if (CURRENT_JOB_ID !== jobId) return;
      const j = d.job;
      const pct = exportProgress(j);
      $('#job-bar').style.width = (pct ?? 0) + '%';
      const stateText = j.export_partial_ready
        ? `已终止，可导出 ${j.export_partial_count || j.succeeded || 0} 个`
        : (j.export_resume_ready
          ? '删除流程已中断'
          : (j.state === 'done'
            ? '已完成'
            : (j.state === 'cancelled' ? '已取消' : (j.state === 'failed' ? '失败' : (pct == null ? '复测中' : `${pct}%`)))));
      $('#job-state').textContent = stateText;
      $('#job-stats').innerHTML =
        renderJobStats(j)
        + (j.error ? `<div class="reason-break"><b class="fail">任务错误</b><div class="reason-line"><span>${esc(j.error)}</span></div></div>` : '')
        + renderReasons(j.reasons);
      renderExportActions(j);
      if (j.state === 'done' || j.state === 'cancelled' || j.state === 'failed') {
        stopped = true;
        clearTimeout(jobTimer);
        CURRENT_JOB_ID = null;
        if (j.state === 'done' && j.type === 'export') {
          const fname = j.export_filename || 'export.txt';
          const n = j.export_count != null ? j.export_count : j.succeeded;
          if (autoDownload && j.export_ready) {
            toast(`${title} 完成：导出 ${n} 个（outlook ${j.export_outlook || 0} / hotmail ${j.export_hotmail || 0}），开始下载 ${fname}`, 6000);
            triggerExportDownload(j.export_id || jobId);
          } else {
            toast(`${title} 已完成：导出 ${n} 个，可点击“重新下载”`, 5000);
          }
        } else if (j.export_partial_ready) {
          toast(`${title} 已终止：可选择导出已复测通过的 ${j.export_partial_count || j.succeeded || 0} 个`, 6000);
        } else if (j.export_resume_ready) {
          toast(`${title} 删除流程已中断，可继续完成删除`, 6000);
        } else {
          const msg = j.state === 'cancelled'
            ? `${title} 已取消：已处理 ${j.processed}，可导出 ${j.succeeded}`
            : (j.state === 'failed'
              ? `${title} 失败：${j.error || '未知错误'}`
              : `${title} 完成：成功 ${j.succeeded}，失败 ${j.failed}，跳过 ${j.skipped}`);
          toast(msg, 5000);
        }
        await refreshAll();
        if (onComplete) await onComplete();
      }
    } catch (e) {
      stopped = true;
      clearTimeout(jobTimer);
      CURRENT_JOB_ID = null;
      $('#job-state').textContent = '轮询失败';
      $('#job-stats').innerHTML = `<div class="reason-break"><b class="fail">任务轮询失败</b><div class="reason-line"><span>${esc(e.message || '未知错误')}</span></div></div>`;
      toast(`任务轮询失败：${e.message || '未知错误'}`, 5000);
    } finally {
      if (!stopped && CURRENT_JOB_ID === jobId) jobTimer = setTimeout(tick, 2000);
    }
  };
  tick();
}

async function restoreRunningJob() {
  if (CURRENT_JOB_ID) return;
  const d = await api('/api/jobs');
  const items = d.jobs || [];
  const job = items.find((j) => j.state === 'running')
    || items.find((j) => j.type === 'export' && (j.export_partial_ready || j.export_resume_ready || j.export_ready));
  if (!job) return;
  const title = JOB_NAME[job.type] || job.type || '当前任务';
  pollJob(job.id, `恢复任务：${title}`, job.state === 'running' && job.type === 'export');
}

// ---------- 详情抽屉 ----------
async function openDrawer(id) {
  try {
    const d = await api(`/api/accounts/${id}/detail`);
    const a = d.account;
    const lastRecoverDebug = LAST_RECOVER_DEBUG.get(id) || '';
    $('#drawer-title').textContent = a.email;
    const reason = a.ban_reason || a.error_detail;
    const recoverBtn = canShowRecoverAbuse(a)
      ? `<button class="btn sm secondary" data-dact="recover" data-id="${id}">恢复 ABUSE</button>`
      : '';
    const histRows = d.history.length ? d.history.map((h) =>
      `<tr><td class="nowrap">${esc(h.created_at)}</td><td>${esc(h.action)}</td><td>${chip(h.status)}</td><td>${esc(h.detail)}</td></tr>`
    ).join('') : '<tr><td colspan="4" class="muted">暂无历史</td></tr>';
    $('#drawer-content').innerHTML = `
      ${reason ? `<div class="reason-box ${sevClass(a.health_severity)}">${esc(reason)}</div>` : ''}
      <div class="kv-grid">
        <div><span>Health</span>${chip(a.health_status || '未测试')}</div>
        <div><span>Graph</span>${chip(a.graph_status)}</div>
        <div><span>IMAP</span>${chip(a.imap_status)}</div>
        <div><span>POP</span>${chip(a.pop_status)}</div>
        <div><span>SMTP</span>${chip(a.smtp_status)}</div>
        <div><span>Remote</span>${chip(a.remote_sync_status || '未同步')}</div>
        <div><span>注册时间</span>${esc(formatDateTime(a.registered_at))}</div>
        <div><span>存活时间</span><small>${esc(formatAliveDuration(a))}</small></div>
        <div><span>最近刷新</span>${esc(a.last_refresh_at || '-')}</div>
        <div><span>最近测试</span>${esc(a.last_protocol_test_at || '-')}</div>
        <div><span>恢复状态</span>${chip(a.recovery_status || '-')}</div>
        ${lastRecoverDebug ? `<div class="kv-full"><span>本次恢复日志</span><small>${esc(lastRecoverDebug)}</small></div>` : ''}
        <div><span>刷新错误</span><small>${esc(a.last_refresh_error || '-')}</small></div>
        <div><span>远程错误</span><small>${esc(a.remote_sync_error || '-')}</small></div>
        <div class="kv-full"><span>密码（可编辑）</span><input class="edit-field" id="edit-password" value="${esc(a.password)}"></div>
        <div class="kv-full"><span>client_id（可编辑）</span><input class="edit-field" id="edit-client_id" value="${esc(a.client_id)}"></div>
        <div class="kv-full"><span>Refresh Token / 授权令牌（可编辑）</span><textarea class="edit-field" id="edit-refresh_token" rows="3">${esc(a.refresh_token)}</textarea></div>
        <div class="kv-full"><span>备注（可编辑）</span><input class="edit-field" id="edit-remark" value="${esc(a.remark || '')}"></div>
      </div>
      <div class="drawer-actions">
        <button class="btn sm primary" data-dact="save" data-id="${id}">保存修改到本地库</button>
        <button class="btn sm secondary" data-dact="refresh" data-id="${id}">刷新 token</button>
        <button class="btn sm success" data-dact="protocol" data-id="${id}">协议测试</button>
        ${recoverBtn}
        <button class="btn sm danger" data-dact="remote-remove" data-id="${id}">从远程移除(留本地)</button>
        <button class="btn sm danger" data-dact="purge" data-id="${id}">彻底删除(本地+远程)</button>
      </div>
      <h4>历史记录</h4>
      <div class="table-wrap"><table class="mini"><thead><tr><th>时间</th><th>动作</th><th>状态</th><th>详情</th></tr></thead><tbody>${histRows}</tbody></table></div>
    `;
    $('#drawer').classList.remove('hidden');
  } catch (e) { toast(`加载详情失败：${e.message}`); }
}

function closeDrawer() { $('#drawer').classList.add('hidden'); }

async function drawerAction(act, id) {
  if (act === 'save') {
    await api(`/api/accounts/${id}`, { method: 'PUT', body: JSON.stringify({
      password: $('#edit-password').value,
      client_id: $('#edit-client_id').value,
      refresh_token: $('#edit-refresh_token').value,
      remark: $('#edit-remark').value,
    }) });
    toast('已保存到本地数据库');
    await refreshAll();
    return openDrawer(id);
  }
  if (act === 'remote-remove') {
    if (!confirm('从远程移除该账号？本地记录保留。')) return;
    await api(`/api/accounts/${id}/remote-remove`, { method: 'POST' });
    toast('已从远程移除（本地保留）'); closeDrawer(); return refreshAll();
  }
  if (act === 'purge') {
    if (!confirm('彻底删除该账号（本地+远程）？不可恢复！')) return;
    await api(`/api/accounts/${id}?remote=true`, { method: 'DELETE' });
    toast('已彻底删除（本地+远程）'); closeDrawer(); return refreshAll();
  }
  await rowAction(act, id);
  await openDrawer(id);
}

// ---------- 配置 ----------
const FIXED_DOMAINS = ['outlook.com', 'hotmail.com'];

function setMapGid(domain, value) {
  const el = domain === 'hotmail.com' ? $('#map-gid-hotmail') : $('#map-gid-outlook');
  el.value = value === undefined || value === null || value === '' ? '' : value;
}

function readMapGid(domain) {
  const el = domain === 'hotmail.com' ? $('#map-gid-hotmail') : $('#map-gid-outlook');
  const gid = parseInt(el.value, 10);
  return Number.isInteger(gid) ? gid : null;
}

async function loadConfig() {
  const d = await api('/api/config');
  const c = d.config;
  $('#cfg-proxy').value = c.proxy_url || '';
  $('#cfg-remote-url').value = c.remote_base_url || '';
  $('#cfg-remote-pass').value = c.remote_password || '';
  $('#cfg-recipient').value = c.external_recipient || '';
  $('#cfg-skip-unmapped').checked = c.skip_unmapped !== false;
  $('#concurrency').value = c.default_concurrency || 100;
  $('#export-concurrency').value = c.export_concurrency || 8;
  const map = c.group_map || {};
  FIXED_DOMAINS.forEach((domain) => setMapGid(domain, map[domain]));
}

async function saveConfig() {
  const group_map = {};
  FIXED_DOMAINS.forEach((domain) => {
    const gid = readMapGid(domain);
    if (gid !== null) group_map[domain] = gid;
  });
  await api('/api/config', { method: 'PUT', body: JSON.stringify({
    proxy_url: $('#cfg-proxy').value,
    remote_base_url: $('#cfg-remote-url').value,
    remote_password: $('#cfg-remote-pass').value,
    group_map,
    skip_unmapped: $('#cfg-skip-unmapped').checked,
    external_recipient: $('#cfg-recipient').value,
  }) });
  toast('配置已保存');
}

// ---------- 事件绑定 ----------
// 统计卡片点击筛选
$$('.stat-card[data-filter]').forEach((card) => {
  card.style.cursor = 'pointer';
  card.onclick = () => {
    const filterValue = card.dataset.filter;
    $('#filter-select').value = filterValue;
    PAGE.page = 1;
    switchPage('#accounts');
    loadAccounts().catch((e) => toast(e.message, 4200));
  };
});
$$('.chart-legend [data-filter]').forEach((item) => {
  item.onclick = () => {
    $('#filter-select').value = item.dataset.filter;
    PAGE.page = 1;
    switchPage('#accounts');
    loadAccounts().catch((e) => toast(e.message, 4200));
  };
});

$('#next-action-btn').onclick = () => {
  const btn = $('#next-action-btn');
  scrollToSection(btn.dataset.target || '#accounts');
  if (btn.dataset.action) setTimeout(() => runWorkflowAction(btn.dataset.action), 350);
};
$$('.workflow-step[data-jump]').forEach((step) => {
  step.onclick = () => scrollToSection(step.dataset.jump);
});

$('#preview-btn').onclick = () => doPreview().catch((e) => toast(e.message, 4200));
$('#import-btn').onclick = () => doImport().catch((e) => toast(e.message, 4200));
$('#clear-btn').onclick = () => { $('#account-input').value = ''; $('#import-result').textContent = ''; };
$('#load-default-btn').onclick = () => api('/api/accounts/load-default-file').then((d) => { refreshAll(); toast(`已导入 oauth2.txt（新增 ${d.inserted}/覆盖 ${d.updated}），建议执行一键测试未测试`); }).catch((e) => toast(e.message, 4200));
$('#file-input').onchange = (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]).catch((err) => toast(err.message, 4200)); };
$('#refresh-view-btn').onclick = () => refreshAll().catch((e) => toast(e.message, 4200));
$('#save-config-btn').onclick = () => saveConfig().catch((e) => toast(e.message, 4200));
$('#top-refresh-btn').onclick = () => refreshAll().then(() => toast('数据已刷新')).catch((e) => toast(e.message, 4200));
$('#global-search-input').onkeydown = (event) => {
  if (event.key !== 'Enter') return;
  $('#search-input').value = event.currentTarget.value.trim();
  PAGE.page = 1;
  switchPage('#accounts');
  loadAccounts().catch((e) => toast(e.message, 4200));
};
$('#mobile-menu-btn').onclick = () => {
  mobileNavOpen = !mobileNavOpen;
  document.body.classList.toggle('sidebar-open', mobileNavOpen);
};
$('#dashboard-jobs').onclick = (event) => {
  const button = event.target.closest('[data-job-focus]');
  if (!button) return;
  const jobId = button.dataset.jobFocus;
  const row = button.closest('tr');
  const title = row?.querySelector('td b')?.textContent || '后台任务';
  pollJob(jobId, title, false);
};
let searchTimer = null;
$('#search-input').oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { PAGE.page = 1; loadAccounts().catch((e) => toast(e.message, 4200)); }, 250);
};
$('#filter-select').onchange = () => { PAGE.page = 1; loadAccounts().catch((e) => toast(e.message, 4200)); };
$('#domain-select').onchange = () => { PAGE.page = 1; loadAccounts().catch((e) => toast(e.message, 4200)); };
$('#apply-alive-threshold-btn').onclick = () => applyAliveThreshold(true);
$('#reset-alive-threshold-btn').onclick = () => resetAliveThreshold();
$('#page-size').onchange = (e) => { PAGE.size = parseInt(e.target.value, 10) || 50; PAGE.page = 1; loadAccounts(); };
$('#page-prev').onclick = () => { if (PAGE.page > 1) { PAGE.page--; loadAccounts(); } };
$('#page-next').onclick = () => { if (PAGE.page < PAGE.pages) { PAGE.page++; loadAccounts(); } };
document.querySelectorAll('th.sortable').forEach((th) => {
  th.onclick = () => {
    const key = th.dataset.sort;
    if (SORT.key === key) SORT.dir = SORT.dir === 'asc' ? 'desc' : 'asc';
    else { SORT.key = key; SORT.dir = 'asc'; }
    loadAccounts().catch((e) => toast(e.message, 4200));
  };
});
$('#drawer-close').onclick = closeDrawer;
$('#drawer-mask').onclick = closeDrawer;

$('#select-all').onchange = (e) => {
  // 全选/取消：仅作用于当前页（符合“控制当前页全部数据选中”语义）
  ALL_ACCOUNTS.forEach((it) => {
    if (e.target.checked) SELECTED.add(it.id); else SELECTED.delete(it.id);
  });
  renderTable();
};

document.querySelectorAll('[data-batch]').forEach((btn) => {
  btn.onclick = () => startBatch(btn.dataset.batch, btn.dataset.all === '1');
});

async function deleteBatch(mode) {
  // mode: 'remote' = 从远程移除(留本地)  |  'both' = 本地+远程彻底删除
  const ids = Array.from(SELECTED);
  if (!ids.length) return toast('请先勾选账号');
  const both = mode === 'both';
  const label = both ? '彻底删除(本地+远程)' : '从远程移除(本地保留)';
  if (!confirm(`确认对 ${ids.length} 个账号执行「${label}」？${both ? '本地记录也会删除，不可恢复！' : '本地记录保留。'}`)) return;
  try {
    const concurrency = parseInt($('#concurrency').value, 10) || 8;
    const path = both ? '/api/accounts/batch/delete' : '/api/accounts/batch/remote-remove';
    const body = both ? { ids, remote: true, concurrency } : { ids, concurrency };
    const d = await api(path, { method: 'POST', body: JSON.stringify(body) });
    if (d.job_id) { toast(`${label} 已启动，共 ${d.total} 个`); pollJob(d.job_id, label); }
    else { toast(`完成，共 ${d.deleted} 个`); SELECTED.clear(); await refreshAll(); }
  } catch (e) { toast(`操作失败：${e.message}`, 4200); }
}
document.querySelectorAll('[data-del]').forEach((btn) => {
  btn.onclick = () => deleteBatch(btn.dataset.del);
});

function exportMinDays() {
  const preset = $('#export-days-preset')?.value || '7';
  if (preset === 'custom') return Math.max(0, parseInt($('#export-days-custom')?.value, 10) || 0);
  return Math.max(0, parseInt(preset, 10) || 0);
}

function exportPayload() {
  const count = Math.max(1, parseInt($('#export-count')?.value, 10) || 1);
  const requestedParts = parseInt($('#export-file-parts')?.value, 10) || 1;
  const file_parts = Math.max(1, Math.min(100, count, requestedParts));
  const domain = $('#export-domain')?.value || 'all';
  const min_registered_days = exportMinDays();
  // 默认复测：无勾选框时也视为 true
  const retestEl = $('#export-retest');
  const retest = retestEl ? !!retestEl.checked : true;
  const concurrency = Math.max(1, Math.min(100, parseInt($('#export-concurrency')?.value, 10) || 8));
  return { count, file_parts, domain, min_registered_days, retest, concurrency };
}

function exportSplitSummary(count, fileParts) {
  const size = Math.floor(count / fileParts);
  const extra = count % fileParts;
  return extra
    ? `${extra} 个文件各 ${size + 1} 条，其余各 ${size} 条`
    : `每个文件 ${size} 条`;
}

function triggerExportDownload(jobId) {
  const a = document.createElement('a');
  a.href = `/api/accounts/export/download/${encodeURIComponent(jobId)}`;
  a.download = '';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

$('#export-days-preset') && ($('#export-days-preset').onchange = () => {
  const wrap = $('#export-days-custom-wrap');
  if (!wrap) return;
  wrap.classList.toggle('hidden', $('#export-days-preset').value !== 'custom');
});
// 默认开启导出前复测
if ($('#export-retest')) $('#export-retest').checked = true;
$('#export-confirm-btn') && ($('#export-confirm-btn').onclick = async () => {
  const p = exportPayload();
  if (!confirm(
    `确认导出最多 ${p.count} 个存活账号？\n` +
    `文件：${p.file_parts} 个（${exportSplitSummary(p.count, p.file_parts)}）\n` +
    `后缀：${p.domain}；注册满 ${p.min_registered_days} 天；复测：${p.retest ? '是' : '否'}；并发：${p.concurrency}\n` +
    `成功导出的账号将从本地与远程删除，且不可恢复。`
  )) return;
  try {
    const d = await api('/api/accounts/export/run', { method: 'POST', body: JSON.stringify(p) });
    sessionStorage.setItem(`export-target:${d.job_id}`, String(p.count));
    toast(`导出任务已启动${d.filename ? ` → ${d.filename}` : ''}`);
    pollJob(d.job_id, '导出', true);
  } catch (e) {
    toast(`启动导出失败：${e.message}`, 4200);
  }
});
$('#goto-export-btn') && ($('#goto-export-btn').onclick = () => switchPage('#export'));

$('#account-table').onclick = (e) => {
  const emptyJump = e.target.closest('[data-empty-jump]');
  if (emptyJump) return scrollToSection(emptyJump.dataset.emptyJump);
  const checkEl = e.target.closest('.row-check');
  if (checkEl) {
    const id = +checkEl.dataset.id;
    if (checkEl.checked) SELECTED.add(id); else SELECTED.delete(id);
    $('#selected-count').textContent = `已选 ${SELECTED.size}`;
    return;
  }
  const actBtn = e.target.closest('[data-act]');
  if (actBtn) return rowAction(actBtn.dataset.act, +actBtn.dataset.id);
  const emailCell = e.target.closest('.email-cell');
  if (emailCell) return openDrawer(+emailCell.dataset.id);
  const row = e.target.closest('tr[data-id]');
  if (row) return openDrawer(+row.dataset.id);
};

$('#drawer-content').onclick = (e) => {
  const btn = e.target.closest('[data-dact]');
  if (btn) drawerAction(btn.dataset.dact, +btn.dataset.id).catch((err) => toast(err.message, 4200));
};

$('#auto-log').onchange = (e) => {
  if (e.target.checked) loadLogs().catch(() => {});
  scheduleLogPoll(4000);
};

$('#clear-logs-btn').onclick = async () => {
  if (!confirm('确认清空运行日志（包括 logs/app.log 文件内容）？')) return;
  try { await api('/api/logs/clear', { method: 'POST' }); await loadLogs(); toast('日志已清空'); }
  catch (e) { toast(`清空失败：${e.message}`, 4200); }
};
$('#select-filtered-btn').onclick = async () => {
  try {
    const data = await api(`/api/accounts/ids?${accountQuery()}`);
    data.ids.forEach((id) => SELECTED.add(id));
    renderTable();
    toast(`已选中筛选结果 ${data.total} 个`);
  } catch (e) { toast(`全选失败：${e.message}`, 4200); }
};
$('#clear-selected-btn').onclick = () => { SELECTED.clear(); renderTable(); };

$('#job-cancel-btn').onclick = async () => {
  if (!CURRENT_JOB_ID) return toast('没有正在运行的任务');
  const jobId = CURRENT_JOB_ID;
  const isExport = DISPLAYED_JOB_ID === jobId;
  const message = isExport
    ? '确认立即终止导出？复测已通过的账号会保留，可随后选择部分导出。'
    : '确认立即终止当前任务？在途操作会停止，未处理项目将跳过。';
  if (!confirm(message)) return;
  const btn = $('#job-cancel-btn');
  btn.disabled = true;
  $('#job-state').textContent = '已取消';
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    toast('终止请求已提交，正在整理已完成结果', 3500);
    pollJob(jobId, isExport ? '导出' : '任务', false);
  } catch (e) {
    toast(`终止失败：${e.message}`, 4200);
    $('#job-state').textContent = '运行中';
  } finally {
    btn.disabled = false;
  }
};

async function continueExportJob() {
  if (!DISPLAYED_JOB_ID) return;
  const jobId = DISPLAYED_JOB_ID;
  try {
    await api(`/api/accounts/export/${encodeURIComponent(jobId)}/finalize`, { method: 'POST' });
    toast('导出文件与删除流程已启动');
    pollJob(jobId, '导出', true);
  } catch (e) {
    toast(`继续导出失败：${e.message}`, 4200);
  }
}

$('#export-partial-btn').onclick = async () => {
  if (!confirm('确认导出当前已复测通过的账号？文件落盘后，这些账号会从本地与远程删除。')) return;
  await continueExportJob();
};
$('#export-resume-btn').onclick = continueExportJob;
$('#export-download-btn').onclick = () => {
  if (DISPLAYED_JOB_ID) triggerExportDownload(DISPLAYED_JOB_ID);
};
$('#export-retry-remote-btn').onclick = async () => {
  if (!DISPLAYED_JOB_ID) return;
  const exportJobId = DISPLAYED_JOB_ID;
  try {
    const data = await api(`/api/accounts/export/${encodeURIComponent(exportJobId)}/retry-remote-delete`, { method: 'POST' });
    toast(`远程删除重试已启动，共 ${data.total} 个`);
    pollJob(data.job_id, '重试远程删除', false, async () => {
      const detail = await api(`/api/jobs/${encodeURIComponent(exportJobId)}`);
      $('#job-title').textContent = '导出';
      $('#job-stats').innerHTML = renderJobStats(detail.job) + renderReasons(detail.job.reasons);
      renderExportActions(detail.job);
    });
  } catch (e) { toast(`重试失败：${e.message}`, 4200); }
};
$('#export-delete-file-btn').onclick = async () => {
  if (!DISPLAYED_JOB_ID || !confirm('确认删除本次导出文件？')) return;
  try {
    await api(`/api/accounts/export/${encodeURIComponent(DISPLAYED_JOB_ID)}/file`, { method: 'DELETE' });
    toast('导出文件已删除');
    $('#export-download-btn').classList.add('hidden');
    $('#export-delete-file-btn').classList.add('hidden');
  } catch (e) { toast(`删除失败：${e.message}`, 4200); }
};
$('#export-discard-btn').onclick = async () => {
  if (!DISPLAYED_JOB_ID || !confirm('确认保留这些账号并结束本次导出任务？')) return;
  try {
    await api(`/api/accounts/export/${encodeURIComponent(DISPLAYED_JOB_ID)}/discard`, { method: 'POST' });
    toast('已保留账号并结束本次任务');
    $('#job-export-actions').classList.add('hidden');
    await refreshAll();
  } catch (e) {
    toast(`结束任务失败：${e.message}`, 4200);
  }
};

$('#concurrency').onchange = async () => {
  const val = parseInt($('#concurrency').value, 10);
  if (!val || val < 1 || val > 100) { toast('并发数必须在 1-100 之间', 3000); return; }
  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify({ default_concurrency: val }) });
    toast('并发数已保存');
  } catch (e) { toast(`保存失败：${e.message}`, 4200); }
};

$('#export-concurrency').onchange = async () => {
  const val = parseInt($('#export-concurrency').value, 10);
  if (!val || val < 1 || val > 100) { toast('导出并发必须在 1-100 之间', 3000); return; }
  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify({ export_concurrency: val }) });
    toast('导出并发已保存');
  } catch (e) { toast(`保存失败：${e.message}`, 4200); }
};

$('#test-untested-btn').onclick = async () => {
  const concurrency = parseInt($('#concurrency').value, 10) || 24;
  try {
    const d = await api('/api/accounts/batch/test-untested', { method: 'POST', body: JSON.stringify({ concurrency }) });
    toast(`一键测试未测试 已启动，共 ${d.total} 个，并发 ${d.concurrency}`);
    pollJob(d.job_id, '一键测试未测试');
  } catch (e) { toast(`启动失败：${e.message}`, 4200); }
};

$('#test-missing-registration-btn').onclick = async () => {
  const concurrency = parseInt($('#concurrency').value, 10) || 24;
  try {
    const d = await api('/api/accounts/batch/test-missing-registration', { method: 'POST', body: JSON.stringify({ concurrency }) });
    toast(`补测注册/存活时间 已启动，共 ${d.total} 个，并发 ${d.concurrency}`);
    pollJob(d.job_id, '补测注册/存活时间');
  } catch (e) { toast(`启动失败：${e.message}`, 4200); }
};

$('#sync-never-synced-btn').onclick = async () => {
  const concurrency = parseInt($('#concurrency').value, 10) || 24;
  try {
    const d = await api('/api/accounts/batch/sync-never-synced', { method: 'POST', body: JSON.stringify({ concurrency }) });
    toast(`一键同步未上传 已启动，共 ${d.total} 个，并发 ${d.concurrency}`);
    pollJob(d.job_id, '一键同步未上传');
  } catch (e) { toast(`启动失败：${e.message}`, 4200); }
};

$('#reconcile-remote-btn').onclick = async () => {
  if (!confirm('开始校准？将上传正常账号，并从远程批量分组移除本地封禁或已不存在的账号。')) return;
  const concurrency = parseInt($('#concurrency').value, 10) || 24;
  try {
    const d = await api('/api/remote/reconcile', { method: 'POST', body: JSON.stringify({ concurrency }) });
    toast(`远程校准已启动，正常 ${d.normal} 个，封禁 ${d.banned} 个`);
    pollJob(d.job_id, '校准本地与远程');
  } catch (e) { toast(`启动失败：${e.message}`, 4200); }
};

// 左侧 TAB：子页面切换（非滚动锚点）
document.querySelectorAll('.nav a').forEach((a) => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    switchPage(a.getAttribute('href'));
  });
});
window.addEventListener('hashchange', () => switchPage(location.hash || '#overview'));
document.addEventListener('visibilitychange', () => {
  scheduleLogPoll(document.hidden ? 4000 : 0);
  scheduleAccountRefresh(document.hidden ? 10000 : 0);
});
switchPage(location.hash || '#overview');

loadConfig().catch(() => {});
syncAliveThresholdInputs();
updateAliveThresholdSummary();
refreshAll().catch((e) => toast(e.message, 4200));
scheduleAccountRefresh(10000);
restoreRunningJob().catch(() => {});
if (window.lucide) window.lucide.createIcons({ attrs: { 'aria-hidden': 'true' } });
