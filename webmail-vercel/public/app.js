// Complete Application Controller: Firebase Auth Whitelist, Webmail, OTP Viewer, and Autoreg Console

const WHITELIST_EMAIL = "greenyarik0505@gmail.com";

// --- Firebase Configuration ---
const firebaseConfig = {
  projectId: "marvel-quiz-260920",
  appId: "1:12875503898:web:6b3d6e98f05f1389bb74a3",
  storageBucket: "marvel-quiz-260920.firebasestorage.app",
  apiKey: "AIzaSyBKKsrW_Jzj4PC46MRpwBLFnr3RT1ihEo4",
  authDomain: "marvel-quiz-260920.firebaseapp.com",
  messagingSenderId: "12875503898",
};

let auth = null;
let googleProvider = null;
try {
  firebase.initializeApp(firebaseConfig);
  auth = firebase.auth();
  googleProvider = new firebase.auth.GoogleAuthProvider();
} catch (e) {
  console.error("Firebase init error:", e);
}

// --- Application State ---
let state = {
  user: null,
  currentView: "mail", // "mail" or "autoreg"
  accounts: [],
  currentAccount: null,
  currentFolder: "inbox",
  messages: [],
  currentMessage: null,
  backendUrl: localStorage.getItem("outlook_backend_url") || "https://outlook-backend-cgy5.onrender.com",
  refreshTimer: null,
  autoregTimer: null,
  lastLogLength: 0,
};

// --- DOM Elements ---
const authOverlay = document.getElementById("auth-overlay");
const authErrorMsg = document.getElementById("auth-error-msg");
const btnGoogleLogin = document.getElementById("btn-google-login");
const appContainer = document.getElementById("app-container");
const sidebarUserAvatar = document.getElementById("sidebar-user-avatar");
const sidebarUserName = document.getElementById("sidebar-user-name");
const sidebarUserEmail = document.getElementById("sidebar-user-email");
const btnLogout = document.getElementById("btn-logout");

const tabNavMail = document.getElementById("tab-nav-mail");
const tabNavAutoreg = document.getElementById("tab-nav-autoreg");
const viewMailWrapper = document.getElementById("view-mail-wrapper");
const viewAutoregWrapper = document.getElementById("view-autoreg-wrapper");

// Mail Elements
const accountsContainer = document.getElementById("accounts-container");
const messagesContainer = document.getElementById("messages-container");
const viewEmpty = document.getElementById("view-empty");
const viewContent = document.getElementById("view-content");
const viewSubject = document.getElementById("view-subject");
const viewFromName = document.getElementById("view-from-name");
const viewFromEmail = document.getElementById("view-from-email");
const viewDate = document.getElementById("view-date");
const viewAvatar = document.getElementById("view-avatar");
const viewOtpBanner = document.getElementById("view-otp-banner");
const otpDisplayValue = document.getElementById("otp-display-value");
const btnCopyOtp = document.getElementById("btn-copy-otp");
const btnOpenLink = document.getElementById("btn-open-link");
const viewIframe = document.getElementById("view-iframe");
const viewText = document.getElementById("view-text");
const searchInput = document.getElementById("search-input");
const autoRefreshSelect = document.getElementById("auto-refresh-select");
const badgeInbox = document.getElementById("badge-inbox");

// Autoreg Elements
const metricStatus = document.getElementById("metric-status");
const metricRegistered = document.getElementById("metric-registered");
const metricTasks = document.getElementById("metric-tasks");
const metricPid = document.getElementById("metric-pid");
const inputAutoregTasks = document.getElementById("input-autoreg-tasks");
const inputAutoregConcurrent = document.getElementById("input-autoreg-concurrent");
const inputAutoregSuffix = document.getElementById("input-autoreg-suffix");
const inputAutoregProxy = document.getElementById("input-autoreg-proxy");
const btnStartAutoreg = document.getElementById("btn-start-autoreg");
const btnStopAutoreg = document.getElementById("btn-stop-autoreg");
const btnSyncDbNow = document.getElementById("btn-sync-db-now");
const terminalBody = document.getElementById("terminal-body");
const checkAutoscroll = document.getElementById("check-autoscroll");
const btnCopyTerminal = document.getElementById("btn-copy-terminal");
const btnClearTerminal = document.getElementById("btn-clear-terminal");
const autoregActionStatus = document.getElementById("autoreg-action-status");

// --- Initialization ---
document.addEventListener("DOMContentLoaded", () => {
  setupAuth();
  loadLocalAccounts();
  setupEventListeners();
  updateLucide();
  setupAutoRefresh();

  if (document.getElementById("input-backend-url")) {
    document.getElementById("input-backend-url").value = state.backendUrl;
  }
});

function updateLucide() {
  if (window.lucide) {
    window.lucide.createIcons();
  }
}

// --- Firebase Google Auth & Whitelist Logic ---
function setupAuth() {
  if (!auth) return;

  btnGoogleLogin.addEventListener("click", async () => {
    authErrorMsg.style.display = "none";
    try {
      const result = await auth.signInWithPopup(googleProvider);
      handleAuthUser(result.user);
    } catch (err) {
      showAuthError(`Ошибка входа Google: ${err.message}`);
    }
  });

  btnLogout.addEventListener("click", async () => {
    await auth.signOut();
    state.user = null;
    appContainer.style.display = "none";
    authOverlay.style.display = "flex";
    showToast("Вы вышли из системы", "success");
  });

  auth.onAuthStateChanged((user) => {
    if (user) {
      handleAuthUser(user);
    } else {
      appContainer.style.display = "none";
      authOverlay.style.display = "flex";
    }
  });
}

function handleAuthUser(user) {
  const userEmail = (user.email || "").toLowerCase().trim();
  const allowed = WHITELIST_EMAIL.toLowerCase().trim();

  if (userEmail === allowed) {
    // Whitelist Access GRANTED
    state.user = user;
    authOverlay.style.display = "none";
    appContainer.style.display = "flex";

    sidebarUserName.innerText = user.displayName || "Admin";
    sidebarUserEmail.innerText = user.email;
    if (user.photoURL) {
      sidebarUserAvatar.src = user.photoURL;
    }

    showToast(`Добро пожаловать, ${user.displayName || user.email}!`, "success");
    syncAccountsFromBackend();
    startAutoregPoller();
  } else {
    // Access DENIED
    auth.signOut();
    showAuthError(`⛔ Доступ запрещен. Аккаунт ${user.email} не авторизован.`);
  }
}

function showAuthError(msg) {
  authErrorMsg.innerText = msg;
  authErrorMsg.style.display = "block";
}

// --- View Switcher (Mail vs Autoreg) ---
function switchView(viewName) {
  state.currentView = viewName;
  if (viewName === "mail") {
    tabNavMail.className = "btn btn-primary";
    tabNavAutoreg.className = "btn btn-secondary";
    viewMailWrapper.style.display = "flex";
    viewAutoregWrapper.style.display = "none";
  } else {
    tabNavMail.className = "btn btn-secondary";
    tabNavAutoreg.className = "btn btn-primary";
    viewMailWrapper.style.display = "none";
    viewAutoregWrapper.style.display = "flex";
    fetchAutoregLogs();
  }
}

// --- Autoregistration Controller ---
async function startAutoreg() {
  const tasks = parseInt(inputAutoregTasks.value, 10) || 5;
  const concurrent = parseInt(inputAutoregConcurrent.value, 10) || 1;
  const suffix = inputAutoregSuffix.value || "@outlook.com";
  const proxy = inputAutoregProxy ? inputAutoregProxy.value.trim() : "";

  btnStartAutoreg.disabled = true;
  autoregActionStatus.innerText = "Запуск воркера...";

  try {
    const url = `${state.backendUrl.replace(/\/+$/, "")}/api/autoreg/start`;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tasks: tasks,
        concurrent: concurrent,
        email_suffix: suffix,
        headless: true,
        proxy: proxy || null,
      }),
    });
    const data = await resp.json();

    if (data.success) {
      showToast(`Авторегистрация запущена (Задач: ${tasks}, Потоков: ${concurrent})!`, "success");
      btnStopAutoreg.disabled = false;
      autoregActionStatus.innerText = `Воркер запущен (PID ${data.pid || "Active"})`;
      fetchAutoregLogs();
    } else {
      throw new Error(data.error || "Не удалось запустить воркер");
    }
  } catch (err) {
    showToast(`Ошибка запуска: ${err.message}`, "error");
    btnStartAutoreg.disabled = false;
    autoregActionStatus.innerText = `Ошибка: ${err.message}`;
  }
}

async function stopAutoreg() {
  autoregActionStatus.innerText = "Остановка воркера...";
  try {
    const url = `${state.backendUrl.replace(/\/+$/, "")}/api/autoreg/stop`;
    const resp = await fetch(url, { method: "POST" });
    const data = await resp.json();

    if (data.success) {
      showToast("Воркер авторегистрации остановлен", "success");
      btnStartAutoreg.disabled = false;
      btnStopAutoreg.disabled = true;
      autoregActionStatus.innerText = "Воркер остановлен";
    }
  } catch (err) {
    showToast(`Ошибка остановки: ${err.message}`, "error");
  }
}

async function syncDbNow() {
  btnSyncDbNow.disabled = true;
  try {
    const url = `${state.backendUrl.replace(/\/+$/, "")}/api/autoreg/sync`;
    const resp = await fetch(url, { method: "POST" });
    const data = await resp.json();
    showToast(`Синхронизировано новых аккаунтов: ${data.imported_count || 0}`, "success");
    syncAccountsFromBackend();
  } catch (err) {
    showToast(`Ошибка синхронизации: ${err.message}`, "error");
  } finally {
    btnSyncDbNow.disabled = false;
  }
}

function startAutoregPoller() {
  if (state.autoregTimer) clearInterval(state.autoregTimer);
  state.autoregTimer = setInterval(() => {
    fetchAutoregLogs();
  }, 2000);
}

async function fetchAutoregLogs() {
  if (!state.backendUrl) return;

  try {
    const url = `${state.backendUrl.replace(/\/+$/, "")}/api/autoreg/logs?limit=150`;
    const resp = await fetch(url);
    const data = await resp.json();

    if (data.success) {
      const status = data.status || {};
      const isRunning = Boolean(status.running);

      metricStatus.innerText = isRunning ? "🟢 Работает" : "⚪ Остановлен";
      metricStatus.style.color = isRunning ? "var(--success)" : "var(--text-dim)";
      metricRegistered.innerText = String(status.registered_count || 0);
      metricPid.innerText = status.pid ? `#${status.pid}` : "—";
      metricTasks.innerText = String(inputAutoregTasks.value);

      btnStartAutoreg.disabled = isRunning;
      btnStopAutoreg.disabled = !isRunning;

      renderTerminalLines(data.lines || []);
    }
  } catch (err) {
    // Silently continue polling
  }
}

function renderTerminalLines(lines) {
  if (!lines || lines.length === 0) return;

  terminalBody.innerHTML = "";
  lines.forEach((line) => {
    const div = document.createElement("div");
    let cls = "terminal-line";
    if (line.includes("[ERROR]") || line.includes("Fail") || line.includes("ERR")) {
      cls += " error";
    } else if (line.includes("[WARN]")) {
      cls += " warn";
    } else if (line.includes("[INIT]") || line.includes("[DB]") || line.includes("Success")) {
      cls += " info";
    }
    div.className = cls;
    div.innerText = line;
    terminalBody.appendChild(div);
  });

  if (checkAutoscroll.checked) {
    terminalBody.scrollTop = terminalBody.scrollHeight;
  }
}

// --- Accounts Management ---
function loadLocalAccounts() {
  try {
    const raw = localStorage.getItem("outlook_accounts");
    state.accounts = raw ? JSON.parse(raw) : [];
  } catch (e) {
    state.accounts = [];
  }
  renderAccountsList();
}

function saveLocalAccounts() {
  localStorage.setItem("outlook_accounts", JSON.stringify(state.accounts));
  renderAccountsList();
}

function renderAccountsList() {
  accountsContainer.innerHTML = "";
  if (state.accounts.length === 0) {
    accountsContainer.innerHTML = `
      <div style="padding: 16px 12px; font-size: 12px; color: var(--text-dim); text-align: center;">
        Нет аккаунтов.<br>Нажмите «+» или синхронизируйте с бекендом.
      </div>
    `;
    return;
  }

  state.accounts.forEach((acc) => {
    const isSelected = state.currentAccount && state.currentAccount.email === acc.email;
    const card = document.createElement("div");
    card.className = `account-card ${isSelected ? "active" : ""}`;
    card.innerHTML = `
      <div class="account-email" title="${acc.email}">${acc.email}</div>
      <div class="account-meta">
        <span>${acc.source || "server"}</span>
        <button class="btn btn-secondary btn-sm" style="padding: 1px 5px; font-size: 10px;" title="Удалить">✕</button>
      </div>
    `;

    card.addEventListener("click", (e) => {
      if (e.target.tagName.toLowerCase() === "button") {
        e.stopPropagation();
        deleteAccount(acc.email);
        return;
      }
      selectAccount(acc);
    });

    accountsContainer.appendChild(card);
  });
}

function deleteAccount(email) {
  state.accounts = state.accounts.filter((a) => a.email !== email);
  saveLocalAccounts();
  if (state.currentAccount && state.currentAccount.email === email) {
    state.currentAccount = state.accounts[0] || null;
    if (state.currentAccount) {
      selectAccount(state.currentAccount);
    } else {
      messagesContainer.innerHTML = `<div style="padding:40px 20px;text-align:center;color:var(--text-dim);">Добавьте аккаунт слева</div>`;
      viewEmpty.style.display = "flex";
      viewContent.style.display = "none";
    }
  }
  showToast("Аккаунт удален из списка", "success");
}

function selectAccount(acc) {
  state.currentAccount = acc;
  renderAccountsList();
  loadMessages();
}

// --- Mail Messages Handling ---
async function loadMessages() {
  if (!state.currentAccount) return;

  const folder = state.currentFolder;
  const search = searchInput.value.trim();

  messagesContainer.innerHTML = `
    <div style="padding: 30px; text-align: center; color: var(--text-dim);">
      <div style="display:inline-block;width:20px;height:20px;border:2px solid var(--primary);border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;"></div>
      <div style="margin-top: 8px; font-size: 13px;">Загрузка писем...</div>
    </div>
  `;

  try {
    let data;
    if (state.backendUrl && state.currentAccount.fromBackend) {
      const url = new URL(`${state.backendUrl.replace(/\/+$/, "")}/api/mail/inbox`);
      url.searchParams.set("email", state.currentAccount.email);
      url.searchParams.set("folder", folder);
      if (search) url.searchParams.set("search", search);
      const resp = await fetch(url.toString());
      data = await resp.json();
    } else {
      const resp = await fetch("/api/inbox", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: state.currentAccount.client_id,
          refresh_token: state.currentAccount.refresh_token,
          folder: folder,
          search: search,
        }),
      });
      data = await resp.json();
    }

    if (!data.success) {
      throw new Error(data.error || "Не удалось загрузить почту");
    }

    state.messages = data.messages || [];
    if (folder === "inbox") {
      badgeInbox.innerText = String(state.messages.length);
    }
    renderMessagesList();
  } catch (err) {
    messagesContainer.innerHTML = `
      <div style="padding: 24px; color: var(--danger); font-size: 13px; text-align: center;">
        ⚠️ Ошибка: ${err.message}
      </div>
    `;
  }
}

function renderMessagesList() {
  messagesContainer.innerHTML = "";
  if (state.messages.length === 0) {
    messagesContainer.innerHTML = `
      <div style="padding: 40px 20px; text-align: center; color: var(--text-dim); font-size: 13px;">
        В папке нет писем
      </div>
    `;
    return;
  }

  state.messages.forEach((msg) => {
    const isSelected = state.currentMessage && state.currentMessage.id === msg.id;
    const card = document.createElement("div");
    card.className = `message-card ${isSelected ? "selected" : ""} ${!msg.is_read ? "unread" : ""}`;

    const dateFormatted = formatDate(msg.received_at);

    let quickOtpBadge = "";
    if (msg.quick_code) {
      quickOtpBadge = `
        <div class="quick-otp-badge" data-code="${msg.quick_code}" title="Нажмите, чтобы скопировать код">
          🔑 Код: ${msg.quick_code}
        </div>
      `;
    }

    card.innerHTML = `
      <div class="msg-header">
        <span class="msg-from" title="${msg.from_email}">${msg.from_name || msg.from_email || "Неизвестный"}</span>
        <span class="msg-date">${dateFormatted}</span>
      </div>
      <div class="msg-subject">${escapeHtml(msg.subject)}</div>
      <div class="msg-preview">${escapeHtml(msg.preview || "")}</div>
      ${quickOtpBadge}
    `;

    const badgeEl = card.querySelector(".quick-otp-badge");
    if (badgeEl) {
      badgeEl.addEventListener("click", (e) => {
        e.stopPropagation();
        copyToClipboard(msg.quick_code, "Код скопирован в буфер!");
      });
    }

    card.addEventListener("click", () => {
      openMessage(msg.id);
    });

    messagesContainer.appendChild(card);
  });
}

async function openMessage(messageId) {
  if (!state.currentAccount) return;

  viewEmpty.style.display = "none";
  viewContent.style.display = "flex";

  viewSubject.innerText = "Загрузка письма...";
  viewFromName.innerText = "";
  viewFromEmail.innerText = "";
  viewDate.innerText = "";
  viewOtpBanner.style.display = "none";
  viewIframe.srcdoc = "<div style='font-family:sans-serif;padding:20px;color:#666;'>Загрузка тела письма...</div>";
  viewText.innerText = "";

  try {
    let data;
    if (state.backendUrl && state.currentAccount.fromBackend) {
      const url = `${state.backendUrl.replace(/\/+$/, "")}/api/mail/message/${encodeURIComponent(messageId)}?email=${encodeURIComponent(state.currentAccount.email)}`;
      const resp = await fetch(url);
      data = await resp.json();
    } else {
      const resp = await fetch("/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: state.currentAccount.client_id,
          refresh_token: state.currentAccount.refresh_token,
          id: messageId,
        }),
      });
      data = await resp.json();
    }

    if (!data.success) {
      throw new Error(data.error || "Не удалось загрузить письмо");
    }

    state.currentMessage = data;
    renderMessageDetail(data);
  } catch (err) {
    showToast(`Ошибка: ${err.message}`, "error");
    viewSubject.innerText = "Ошибка загрузки";
  }
}

function renderMessageDetail(msg) {
  viewSubject.innerText = msg.subject || "(Без темы)";
  viewFromName.innerText = msg.from_name || msg.from_email || "Неизвестный";
  viewFromEmail.innerText = msg.from_email || "";
  viewDate.innerText = formatDate(msg.received_at, true);

  const initial = (msg.from_name || msg.from_email || "?").charAt(0).toUpperCase();
  viewAvatar.innerText = initial;

  if (msg.otp && msg.otp.code) {
    viewOtpBanner.style.display = "flex";
    otpDisplayValue.innerText = msg.otp.code;
    btnCopyOtp.onclick = () => copyToClipboard(msg.otp.code, `Код ${msg.otp.code} скопирован!`);

    if (msg.otp.link) {
      btnOpenLink.style.display = "inline-flex";
      btnOpenLink.href = msg.otp.link;
    } else {
      btnOpenLink.style.display = "none";
    }
  } else {
    viewOtpBanner.style.display = "none";
  }

  const htmlContent = msg.body_content || msg.body_preview || "";
  if (msg.body_type === "html" || htmlContent.includes("<")) {
    viewIframe.srcdoc = htmlContent;
    viewText.innerText = msg.body_preview || "";
  } else {
    viewIframe.srcdoc = `<pre style="font-family:sans-serif;white-space:pre-wrap;padding:16px;">${escapeHtml(htmlContent)}</pre>`;
    viewText.innerText = htmlContent;
  }

  updateLucide();
}

// --- Sync Accounts from Backend ---
async function syncAccountsFromBackend() {
  if (!state.backendUrl) return;

  try {
    const url = `${state.backendUrl.replace(/\/+$/, "")}/api/mail/accounts`;
    const resp = await fetch(url);
    const data = await resp.json();

    if (data.success && Array.isArray(data.accounts)) {
      const backendAccounts = data.accounts.map((a) => ({
        email: a.email,
        fromBackend: true,
        source: "backend",
      }));

      const existingEmails = new Set(state.accounts.map((a) => a.email));
      backendAccounts.forEach((ba) => {
        if (!existingEmails.has(ba.email)) {
          state.accounts.push(ba);
        }
      });

      saveLocalAccounts();
      if (!state.currentAccount && state.accounts.length > 0) {
        selectAccount(state.accounts[0]);
      }
    }
  } catch (err) {
    console.error("Backend sync failed:", err);
  }
}

// --- Setup Event Listeners ---
function setupEventListeners() {
  // Navigation tabs (Mail vs Autoreg)
  tabNavMail.addEventListener("click", () => switchView("mail"));
  tabNavAutoreg.addEventListener("click", () => switchView("autoreg"));

  // Autoreg control buttons
  btnStartAutoreg.addEventListener("click", startAutoreg);
  btnStopAutoreg.addEventListener("click", stopAutoreg);
  btnSyncDbNow.addEventListener("click", syncDbNow);
  btnClearTerminal.addEventListener("click", () => {
    terminalBody.innerHTML = `<div class="terminal-line info">[SYS] Terminal cleared.</div>`;
  });
  if (btnCopyTerminal) {
    btnCopyTerminal.addEventListener("click", () => {
      const lineEls = Array.from(terminalBody.querySelectorAll(".terminal-line"));
      if (!lineEls || lineEls.length === 0) {
        showToast("Логи пусты", "error");
        return;
      }
      const text = lineEls.map((el) => el.innerText).join("\n");
      copyToClipboard(text, "Все логи скопированы в буфер!");
    });
  }

  // Mail folder tabs
  document.querySelectorAll(".nav-folders .nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".nav-folders .nav-item").forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      state.currentFolder = item.dataset.folder;
      loadMessages();
    });
  });

  // Search input
  let searchTimeout = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      loadMessages();
    }, 400);
  });

  document.getElementById("btn-manual-refresh").addEventListener("click", () => {
    loadMessages();
    showToast("Письма обновлены", "success");
  });

  autoRefreshSelect.addEventListener("change", setupAutoRefresh);

  document.getElementById("btn-sync-accounts").addEventListener("click", () => {
    syncAccountsFromBackend();
    showToast("Аккаунты синхронизированы", "success");
  });

  document.getElementById("btn-open-connect").addEventListener("click", () => openModal("modal-connect"));
  document.getElementById("btn-open-settings").addEventListener("click", () => openModal("modal-settings"));

  // Account line quick-parser
  document.getElementById("input-account-line").addEventListener("input", (e) => {
    const val = e.target.value.trim();
    if (val.includes("----")) {
      const parts = val.split("----").map((s) => s.trim());
      if (parts.length >= 4) {
        document.getElementById("input-email").value = parts[0];
        document.getElementById("input-client-id").value = parts[2];
        document.getElementById("input-refresh-token").value = parts.slice(3).join("----");
      }
    }
  });

  // Save manual account
  document.getElementById("btn-save-account").addEventListener("click", () => {
    const email = document.getElementById("input-email").value.trim();
    const clientId = document.getElementById("input-client-id").value.trim() || "d3590ed6-52b3-4102-aeff-aad2292ab01c";
    const refreshToken = document.getElementById("input-refresh-token").value.trim();

    if (!email || !refreshToken) {
      alert("Укажите хотя бы Email и Refresh Token");
      return;
    }

    const newAcc = {
      email,
      client_id: clientId,
      refresh_token: refreshToken,
      source: "manual",
    };

    const idx = state.accounts.findIndex((a) => a.email === email);
    if (idx >= 0) {
      state.accounts[idx] = newAcc;
    } else {
      state.accounts.unshift(newAcc);
    }

    saveLocalAccounts();
    closeModal("modal-connect");
    selectAccount(newAcc);
    showToast(`Аккаунт ${email} добавлен`, "success");
  });

  // Save Settings
  document.getElementById("btn-save-settings").addEventListener("click", () => {
    const url = document.getElementById("input-backend-url").value.trim();
    state.backendUrl = url;
    localStorage.setItem("outlook_backend_url", url);
    closeModal("modal-settings");
    showToast("Настройки сохранены", "success");
    if (url) {
      syncAccountsFromBackend();
      fetchAutoregLogs();
    }
  });

  // Message viewer tabs (HTML vs Text)
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".view-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const mode = tab.dataset.tab;
      if (mode === "html") {
        viewIframe.style.display = "block";
        viewText.style.display = "none";
      } else {
        viewIframe.style.display = "none";
        viewText.style.display = "block";
      }
    });
  });

  // Copy body
  document.getElementById("btn-copy-body").addEventListener("click", () => {
    if (state.currentMessage) {
      const text = state.currentMessage.body_content || state.currentMessage.body_preview || "";
      copyToClipboard(text, "Текст письма скопирован");
    }
  });

  // Raw open in new window
  document.getElementById("btn-raw-open").addEventListener("click", () => {
    if (state.currentMessage && state.currentMessage.body_content) {
      const win = window.open("", "_blank");
      win.document.write(state.currentMessage.body_content);
      win.document.close();
    }
  });
}

function setupAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  const sec = parseInt(autoRefreshSelect.value, 10);
  if (sec > 0) {
    state.refreshTimer = setInterval(() => {
      if (state.currentView === "mail") {
        loadMessages();
      }
    }, sec * 1000);
  }
}

// --- Utilities ---
function openModal(id) {
  document.getElementById(id).classList.add("active");
}

function closeModal(id) {
  document.getElementById(id).classList.remove("active");
}

function copyToClipboard(text, successMsg = "Скопировано") {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    showToast(successMsg, "success");
  });
}

function showToast(msg, type = "success") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerText = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 3500);
}

function formatDate(isoStr, full = false) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return isoStr;

  if (full) {
    return d.toLocaleString("ru-RU", {
      day: "numeric",
      month: "long",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 1) return "Только что";
  if (diffMin < 60) return `${diffMin} мин назад`;
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
