// Frontend Application Logic for Outlook Webmail on Vercel

let state = {
  accounts: [],
  currentAccount: null,
  currentFolder: "inbox",
  messages: [],
  currentMessage: null,
  backendUrl: localStorage.getItem("outlook_backend_url") || "",
  refreshTimer: null,
};

// --- DOM Elements ---
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

// --- Initialization ---
document.addEventListener("DOMContentLoaded", () => {
  loadLocalAccounts();
  setupEventListeners();
  updateLucide();

  if (state.backendUrl) {
    document.getElementById("input-backend-url").value = state.backendUrl;
    syncAccountsFromBackend();
  } else if (state.accounts.length > 0) {
    selectAccount(state.accounts[0]);
  }

  setupAutoRefresh();
});

function updateLucide() {
  if (window.lucide) {
    window.lucide.createIcons();
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
        Нет добавленных аккаунтов.<br>Нажмите «Добавить аккаунт» выше.
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
        <span>${acc.source || "local"}</span>
        <button class="btn btn-secondary btn-sm" style="padding: 1px 5px; font-size: 10px;" title="Удалить аккаунт">✕</button>
      </div>
    `;

    // Click to select
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
  showToast("Аккаунт удален", "success");
}

function selectAccount(acc) {
  state.currentAccount = acc;
  renderAccountsList();
  loadMessages();
}

// --- Messages Handling ---
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
    // If backendUrl is set, we can query backend or use serverless function
    if (state.backendUrl && state.currentAccount.fromBackend) {
      const url = new URL(`${state.backendUrl.replace(/\/+$/, "")}/api/mail/inbox`);
      url.searchParams.set("email", state.currentAccount.email);
      url.searchParams.set("folder", folder);
      if (search) url.searchParams.set("search", search);
      const resp = await fetch(url.toString());
      data = await resp.json();
    } else {
      // Query local Vercel serverless /api/inbox
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

    // Quick OTP click
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

  // Handle OTP
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

  // HTML content
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

// --- Sync Accounts from Backend (Oracle / Render) ---
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
        source: "oracle/render",
      }));

      // Merge backend accounts with local accounts
      const existingEmails = new Set(state.accounts.map((a) => a.email));
      backendAccounts.forEach((ba) => {
        if (!existingEmails.has(ba.email)) {
          state.accounts.push(ba);
        }
      });

      saveLocalAccounts();
      showToast(`Синхронизировано ${backendAccounts.length} аккаунтов с бекенда`, "success");
      if (!state.currentAccount && state.accounts.length > 0) {
        selectAccount(state.accounts[0]);
      }
    }
  } catch (err) {
    showToast(`Не удалось подключиться к бекенду: ${err.message}`, "error");
  }
}

// --- Event Listeners ---
function setupEventListeners() {
  // Navigation folders
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

  // Manual refresh button
  document.getElementById("btn-manual-refresh").addEventListener("click", () => {
    loadMessages();
    showToast("Список писем обновлен", "success");
  });

  // Auto-refresh change
  autoRefreshSelect.addEventListener("change", setupAutoRefresh);

  // Sync accounts button
  document.getElementById("btn-sync-accounts").addEventListener("click", () => {
    if (state.backendUrl) {
      syncAccountsFromBackend();
    } else {
      openModal("modal-settings");
    }
  });

  // Modal open buttons
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

  // Save Account
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

    // Replace if exists, or append
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
    }
  });

  // Tab switching in message viewer
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

  // Copy message body
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
      loadMessages();
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
