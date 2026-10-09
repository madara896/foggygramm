// FoggyGramm frontend (M1) — monochrome glass UI.
// Setup, admin/regular login, account add/restore, user management,
// a 3D cloud that peeks over the cards, follows the pointer, covers its
// eyes while a password is typed, and shows hints.

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

function show(view) {
  document.body.dataset.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  const el = $(`view-${view}`);
  void el.offsetWidth; // restart the entrance animation
  el.classList.add("active");
}

let ACCOUNTS = [];

// ------------------------------------------------------------------- boot
async function boot() {
  const status = await api("/status");
  if (!status.initialized) return show("setup");
  try {
    const me = await api("/me");
    return enterDashboard(me);
  } catch (e) {
    if (!status.unlocked) $("login-lock-note").classList.remove("hidden");
    show("login");
  }
}

// ------------------------------------------------------------------ setup
$("btn-setup").onclick = async () => {
  const body = {
    api_id: parseInt($("setup-api-id").value, 10),
    api_hash: $("setup-api-hash").value.trim(),
    admin_username: $("setup-admin-user").value.trim(),
    admin_password: $("setup-admin-pass").value,
  };
  if (!Number.isFinite(body.api_id)) {
    alert("API ID must be a number.");
    return;
  }
  if (!body.api_hash) {
    alert("API Hash is required.");
    return;
  }
  try {
    await api("/setup", { method: "POST", body: JSON.stringify(body) });
    await api("/login", {
      method: "POST",
      body: JSON.stringify({ username: body.admin_username, password: body.admin_password }),
    });
    await enterDashboard({ username: body.admin_username, role: "admin" });
  } catch (e) {
    alert(e.message);
  }
};

// ------------------------------------------------------------------ login
$("btn-login").onclick = async () => {
  $("login-error").textContent = "";
  try {
    const data = await api("/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("login-user").value.trim(),
        password: $("login-pass").value,
      }),
    });
    await enterDashboard({ username: $("login-user").value.trim(), role: data.role });
  } catch (e) {
    $("login-error").textContent = e.message;
  }
};

$("btn-logout").onclick = async () => {
  await api("/logout", { method: "POST" });
  location.reload();
};

// -------------------------------------------------------------- dashboard
async function enterDashboard(me) {
  show("dash");
  $("who-name").textContent = me.username;
  $("who-role").textContent = me.role;
  const isAdmin = me.role === "admin";
  document.querySelectorAll(".admin-only").forEach((el) =>
    el.classList.toggle("hidden", !isAdmin)
  );
  await loadAccounts();
  if (isAdmin) {
    await loadUsers();
    await loadSyncTab();
  }
}

function switchTab(name) {
  document.querySelectorAll(".seg").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  ["accounts", "chats", "users", "sync"].forEach((t) =>
    $(`tab-${t}`).classList.toggle("hidden", name !== t)
  );
  if (name === "sync") loadSyncTab();
  if (name === "chats") loadChats();
}
document.querySelectorAll(".seg").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

const initial = (text) => (text || "?").trim().slice(0, 1).toUpperCase();

async function loadAccounts() {
  const data = await api("/accounts");
  ACCOUNTS = data.accounts || [];
  const list = $("accounts-list");
  list.innerHTML = "";
  if (!ACCOUNTS.length) {
    list.innerHTML = `<div class="empty">No accounts yet.</div>`;
    return;
  }
  ACCOUNTS.forEach((acc, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.style.animationDelay = `${i * 45}ms`;
    row.innerHTML = `
      <div class="avatar">${initial(acc.label)}</div>
      <div class="meta">
        <div class="title">${acc.label || acc.phone}</div>
        <div class="sub">${acc.phone || ""}${acc.username ? " · @" + acc.username : ""}</div>
      </div>
      <div class="dot ${acc.connected ? "on" : ""}" title="${acc.connected ? "connected" : "offline"}"></div>
    `;
    if ($("who-role").textContent === "admin") {
      const del = document.createElement("button");
      del.className = "btn ghost small";
      del.textContent = "Remove";
      del.onclick = async () => {
        if (!confirm(`Remove ${acc.label || acc.phone}?`)) return;
        await api(`/accounts/${acc.id}`, { method: "DELETE" });
        await loadAccounts();
      };
      row.appendChild(del);
    }
    list.appendChild(row);
  });
}

async function loadUsers() {
  const data = await api("/users");
  const list = $("users-list");
  list.innerHTML = "";
  if (!data.users.length) {
    list.innerHTML = `<div class="empty">No regular users yet.</div>`;
    return;
  }
  data.users.forEach((u, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.style.animationDelay = `${i * 45}ms`;
    const names = u.account_ids
      .map((id) => (ACCOUNTS.find((a) => a.id === id) || {}).label || id)
      .join(", ");
    row.innerHTML = `
      <div class="avatar">${initial(u.username)}</div>
      <div class="meta">
        <div class="title">${u.username}</div>
        <div class="sub">${u.slots} slot(s) · ${names || "no accounts assigned"}</div>
      </div>
    `;
    const del = document.createElement("button");
    del.className = "btn ghost small";
    del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm(`Delete user ${u.username}?`)) return;
      await api(`/users/${u.username}`, { method: "DELETE" });
      await loadUsers();
    };
    row.appendChild(del);
    list.appendChild(row);
  });
}

// ------------------------------------------------------------- M2 sync tab
const BACKUP_MODE_LABELS = {
  "manual": "Manual — only when I press “Back up now”",
  "on-change": "On change — after every account or user change",
  "daily": "Daily — automatically, at most once a day",
};

const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "never";

async function loadSyncTab() {
  const settings = await api("/backup/settings");
  const modes = $("backup-modes");
  modes.innerHTML = "";
  Object.entries(BACKUP_MODE_LABELS).forEach(([mode, label]) => {
    const el = document.createElement("label");
    el.className = "check";
    el.innerHTML = `<input type="radio" name="bmode" value="${mode}" /> <span>${label}</span>`;
    const radio = el.querySelector("input");
    radio.checked = settings.backup_mode === mode;
    radio.onchange = async () => {
      await api("/backup/settings", {
        method: "POST",
        body: JSON.stringify({ mode, keep: parseInt($("backup-keep").value, 10) || 10 }),
      });
      await loadSyncTab();
    };
    modes.appendChild(el);
  });
  $("backup-keep").value = settings.keep;
  $("backup-keep").onchange = async () => {
    await api("/backup/settings", {
      method: "POST",
      body: JSON.stringify({
        mode: document.querySelector('input[name="bmode"]:checked').value,
        keep: parseInt($("backup-keep").value, 10) || 10,
      }),
    });
    await loadSyncTab();
  };

  const { backups } = await api("/backup/list");
  const last = backups[0];
  $("backup-status").textContent = last
    ? `Last backup: ${fmtTime(last.created_at)} (${last.label}) · keeping ${backups.length}`
    : "No backups yet.";

  const list = $("backups-list");
  list.innerHTML = backups.length ? "" : `<div class="empty">No backups yet.</div>`;
  backups.forEach((b, i) => {
    const row = document.createElement("div");
    row.className = "row";
    row.style.animationDelay = `${i * 45}ms`;
    const counts = b.accounts >= 0 ? `${b.accounts} account(s) · ${b.users} user(s)` : "encrypted copy";
    row.innerHTML = `
      <div class="avatar">${initial(b.label)}</div>
      <div class="meta">
        <div class="title">${b.label}</div>
        <div class="sub">${fmtTime(b.created_at)} · ${b.mode} · ${counts}</div>
      </div>
    `;
    const restore = document.createElement("button");
    restore.className = "btn primary small";
    restore.textContent = "Restore";
    restore.onclick = async () => {
      if (!confirm(`Restore backup "${b.label}"? Your current vault is kept as a safety backup first.`)) return;
      await api("/backup/restore", { method: "POST", body: JSON.stringify({ id: b.id }) });
      await loadAccounts();
      await loadSyncTab();
    };
    const del = document.createElement("button");
    del.className = "btn ghost small";
    del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm(`Delete backup "${b.label}"?`)) return;
      await api(`/backup/${b.id}`, { method: "DELETE" });
      await loadSyncTab();
    };
    row.appendChild(restore);
    row.appendChild(del);
    list.appendChild(row);
  });

  const sync = await api("/sync/status");
  $("sync-status").textContent = !sync.has_export
    ? "Sync folder is empty — export first."
    : `Export from ${sync.device || "unknown device"} at ${fmtTime(sync.exported_at)}` +
      (sync.differs === false ? " · identical to this vault."
        : sync.local_newer ? " · this vault is newer."
        : sync.local_newer === false ? " · sync folder is newer."
        : "");
}

$("btn-backup-now").onclick = async () => {
  await api("/backup/now", { method: "POST", body: JSON.stringify({ label: "manual" }) });
  await loadSyncTab();
};

$("btn-sync-export").onclick = async () => {
  await api("/sync/export", { method: "POST" });
  await loadSyncTab();
};

$("btn-sync-import").onclick = async () => {
  if (!confirm("Import the sync folder's vault? Your current vault is kept as a safety backup first.")) return;
  try {
    await api("/sync/import", { method: "POST" });
  } catch (e) {
    alert(e.message);
    return;
  }
  await loadAccounts();
  await loadSyncTab();
};

// ---------------------------------------------------------------- M3 chats
const esc = (s) => (s || "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtClock = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
};

const CHAT = { account: null, peer: null, timer: null };

function isAdmin() {
  return $("who-role").textContent === "admin";
}

async function loadChats() {
  if (CHAT.timer) { clearInterval(CHAT.timer); CHAT.timer = null; }
  const sel = $("chat-account");
  sel.innerHTML = "";
  if (!ACCOUNTS.length) {
    sel.innerHTML = `<option value="">No accounts available</option>`;
    $("dialogs-list").innerHTML = `<div class="empty">No accounts yet.</div>`;
    $("messages-list").innerHTML = "";
    return;
  }
  ACCOUNTS.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `${a.label || a.phone}${a.connected ? "" : " (offline)"}`;
    sel.appendChild(opt);
  });
  if (!CHAT.account || !ACCOUNTS.some((a) => a.id === CHAT.account)) {
    CHAT.account = ACCOUNTS[0].id;
    CHAT.peer = null;
  }
  sel.value = CHAT.account;
  $("composer-note").style.display = isAdmin() ? "none" : "";
  await loadDialogs();
  CHAT.timer = setInterval(async () => {
    if ($("tab-chats").classList.contains("hidden")) return;
    await loadDialogs(true);
    if (CHAT.peer) await loadHistory(false);
  }, 5000);
}

$("chat-account").onchange = async () => {
  CHAT.account = $("chat-account").value;
  CHAT.peer = null;
  $("thread-title").textContent = "Select a chat";
  $("messages-list").innerHTML = "";
  await loadDialogs();
};

async function loadDialogs(silent) {
  if (!CHAT.account) return;
  let dialogs = [];
  try {
    const data = await api(`/chats?account_id=${encodeURIComponent(CHAT.account)}&limit=30`);
    dialogs = data.dialogs || [];
  } catch (e) {
    if (!silent) $("dialogs-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  const list = $("dialogs-list");
  list.innerHTML = dialogs.length ? "" : `<div class="empty">No conversations yet.</div>`;
  dialogs.forEach((d) => {
    const row = document.createElement("div");
    row.className = "row dialog" + (CHAT.peer === d.peer_id ? " active" : "");
    row.innerHTML = `
      <div class="avatar">${esc(initial(d.title))}</div>
      <div class="meta">
        <div class="title">${esc(d.title)}</div>
        <div class="sub">${d.last_out ? "You: " : ""}${esc(d.last_text)}</div>
      </div>
      ${d.unread ? `<div class="badge">${d.unread > 99 ? "99+" : d.unread}</div>` : ""}
      <div class="sub">${esc(fmtClock(d.last_date))}</div>
    `;
    row.onclick = () => openThread(d.peer_id, d.title);
    list.appendChild(row);
  });
}

async function openThread(peer_id, title) {
  CHAT.peer = peer_id;
  $("thread-title").textContent = title;
  document.querySelectorAll("#dialogs-list .dialog").forEach((el) => el.classList.remove("active"));
  await loadHistory(true);
  await loadDialogs(true);
}

function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}

async function loadHistory(scroll) {
  if (!CHAT.account || !CHAT.peer) return;
  let messages = [];
  try {
    const data = await api(
      `/chats/history?account_id=${encodeURIComponent(CHAT.account)}&peer_id=${CHAT.peer}&limit=30`
    );
    messages = data.messages || [];
  } catch (e) {
    $("messages-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  const box = $("messages-list");
  const stick = scroll || nearBottom(box) || !box.children.length;
  box.innerHTML = messages.length ? "" : `<div class="empty">No messages yet. Say hi.</div>`;
  messages.forEach((m) => {
    const wrap = document.createElement("div");
    wrap.className = "msg" + (m.out ? " out" : "");
    wrap.innerHTML = `
      <div class="bubble">${esc(m.text) || "<i>empty message</i>"}</div>
      <div class="msg-time">${esc(fmtClock(m.date))}${m.has_media ? " · 📎" : ""}</div>
    `;
    box.appendChild(wrap);
  });
  if (stick) box.scrollTop = box.scrollHeight;
}

async function sendCurrent() {
  const input = $("composer-input");
  const text = input.value.trim();
  if (!text || !CHAT.account || !CHAT.peer) return;
  input.value = "";
  try {
    await api("/chats/send", {
      method: "POST",
      body: JSON.stringify({ account_id: CHAT.account, peer_id: CHAT.peer, text }),
    });
  } catch (e) {
    alert(e.message);
    input.value = text;
    return;
  }
  await loadHistory(true);
  await loadDialogs(true);
}

$("btn-send").onclick = sendCurrent;
$("composer-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendCurrent();
});

// ---------------------------------------------------------- account modal
function resetAccountModal() {
  $("acct-step-phone").classList.remove("hidden");
  $("acct-step-code").classList.add("hidden");
  $("acct-step-pass").classList.add("hidden");
  $("acct-error").textContent = "";
  $("acct-phone").value = "";
  $("acct-code").value = "";
  $("acct-pass").value = "";
}
$("btn-add-account").onclick = () => {
  resetAccountModal();
  $("modal-account").classList.remove("hidden");
};
$("btn-close-modal").onclick = () => $("modal-account").classList.add("hidden");

let LOGIN_ID = null;

$("btn-send-code").onclick = async () => {
  $("acct-error").textContent = "";
  try {
    const data = await api("/accounts/start", {
      method: "POST",
      body: JSON.stringify({ phone: $("acct-phone").value.trim() }),
    });
    LOGIN_ID = data.login_id;
    $("acct-phone-echo").textContent = $("acct-phone").value.trim();
    $("acct-step-phone").classList.add("hidden");
    $("acct-step-code").classList.remove("hidden");
  } catch (e) {
    $("acct-error").textContent = e.message;
  }
};

$("btn-verify-code").onclick = async () => {
  $("acct-error").textContent = "";
  try {
    const data = await api("/accounts/verify", {
      method: "POST",
      body: JSON.stringify({ login_id: LOGIN_ID, code: $("acct-code").value.trim() }),
    });
    if (data.need_password) {
      $("acct-step-code").classList.add("hidden");
      $("acct-step-pass").classList.remove("hidden");
      return;
    }
    $("modal-account").classList.add("hidden");
    await loadAccounts();
  } catch (e) {
    $("acct-error").textContent = e.message;
  }
};

$("btn-verify-pass").onclick = async () => {
  $("acct-error").textContent = "";
  try {
    await api("/accounts/verify_password", {
      method: "POST",
      body: JSON.stringify({ login_id: LOGIN_ID, password: $("acct-pass").value }),
    });
    $("modal-account").classList.add("hidden");
    await loadAccounts();
  } catch (e) {
    $("acct-error").textContent = e.message;
  }
};

// ------------------------------------------------------------- user modal
function renderUserAccountOptions() {
  const box = $("user-accounts");
  box.innerHTML = "";
  if (!ACCOUNTS.length) {
    box.innerHTML = `<span class="sub">No accounts available to assign.</span>`;
    return;
  }
  ACCOUNTS.forEach((acc) => {
    const label = document.createElement("label");
    label.className = "check";
    label.innerHTML = `
      <input type="checkbox" value="${acc.id}" />
      <span>${acc.label || acc.phone}</span>
    `;
    box.appendChild(label);
  });
}

$("btn-add-user").onclick = () => {
  $("user-error").textContent = "";
  $("user-name").value = "";
  $("user-pass").value = "";
  $("user-slots").value = "2";
  renderUserAccountOptions();
  $("modal-user").classList.remove("hidden");
};
$("btn-close-user").onclick = () => $("modal-user").classList.add("hidden");

$("btn-save-user").onclick = async () => {
  $("user-error").textContent = "";
  const accountIds = Array.from(
    $("user-accounts").querySelectorAll("input:checked")
  ).map((c) => c.value);
  try {
    await api("/users", {
      method: "POST",
      body: JSON.stringify({
        username: $("user-name").value.trim(),
        password: $("user-pass").value,
        slots: parseInt($("user-slots").value, 10) || 2,
        account_ids: accountIds,
      }),
    });
    $("modal-user").classList.add("hidden");
    await loadUsers();
  } catch (e) {
    $("user-error").textContent = e.message;
  }
};

// ------------------------------------ clouds follow the pointer (all clouds)
(() => {
  let mx = 0, my = 0, tx = 0, ty = 0;
  window.addEventListener("pointermove", (e) => {
    const cx = window.innerWidth / 2;
    const cy = window.innerHeight / 2;
    tx = Math.max(-1, Math.min(1, (e.clientX - cx) / cx));
    ty = Math.max(-1, Math.min(1, (e.clientY - cy) / cy));
  });
  const root = document.documentElement;
  (function follow() {
    // fast lerp so the cloud reacts immediately (no slow trailing)
    mx += (tx - mx) * 0.22;
    my += (ty - my) * 0.22;
    root.style.setProperty("--mx", mx.toFixed(3));
    root.style.setProperty("--my", my.toFixed(3));
    requestAnimationFrame(follow);
  })();
})();

// ------------------------------------- clouds cover their eyes on password
function syncCover() {
  const active = document.activeElement;
  // NOTE: check the pw-field marker, not just type="password" — the eye
  // toggle flips the type to text, but the field is still password-related
  // and the cloud must keep looking away while it is focused.
  const covering =
    active && active.tagName === "INPUT" &&
    (active.type === "password" || active.dataset.pwToggle === "1");
  document.body.classList.toggle("covering", !!covering);
}
document.addEventListener("focusin", syncCover);
document.addEventListener("focusout", syncCover);
// safety net: eye-toggle clicks, autofill and programmatic focus don't
// reliably fire focus events in every environment, so re-check on a timer
setInterval(syncCover, 120);

// ---------------------------------------------------------------- cloud hints
const HINTS = {
  setup: [
    "Get your free API ID & Hash at my.telegram.org, then create your vault.",
    "Your vault is encrypted with your admin password.",
    "Everything stays on this machine — nothing leaves it.",
  ],
  login: [
    "Log in as admin to restore all your accounts.",
    "Regular users can sign in only while the admin is unlocked.",
    "Forget the password and the vault can't be opened. Keep it safe.",
  ],
  dash: [
    "Click 'Add account' to log in once — no codes after that.",
    "Assign 1–2 accounts to keep a regular user's view clean.",
    "A glowing dot means the account is connected.",
    "The Sync tab keeps encrypted backups and carries the vault to other devices.",
    "Open the Chats tab to read and reply — sending is admin-only for now.",
  ],
};
const HINT_TEXTS = document.querySelectorAll(".cloud-hint-text");
const HINT_BOXES = document.querySelectorAll(".cloud-hint");
let hintIndex = 0;
function currentView() {
  const v = document.querySelector(".view.active");
  return v ? v.id.replace("view-", "") : "setup";
}
function cycleHint() {
  const list = HINTS[currentView()] || [];
  if (!list.length) {
    HINT_BOXES.forEach((b) => b.classList.remove("show"));
    return;
  }
  const text = list[hintIndex % list.length];
  hintIndex++;
  HINT_TEXTS.forEach((el) => (el.textContent = text));
  HINT_BOXES.forEach((b) => b.classList.add("show"));
}
setTimeout(cycleHint, 900);
setInterval(cycleHint, 9000);

// --------------------------------------------------------------- theme toggle
const themeBtn = $("theme-toggle");
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  themeBtn.textContent = theme === "light" ? "☀" : "☾";
  try { localStorage.setItem("foggy-theme", theme); } catch (e) { /* ignore */ }
}
let savedTheme = "dark";
try { savedTheme = localStorage.getItem("foggy-theme") || "dark"; } catch (e) { /* ignore */ }
applyTheme(savedTheme);
themeBtn.onclick = () => {
  const now = document.documentElement.getAttribute("data-theme") || "dark";
  const next = now === "light" ? "dark" : "light";
  // restart the coin-flip spin, swap the icon mid-spin, then clean up
  themeBtn.classList.remove("spin");
  void themeBtn.offsetWidth;
  themeBtn.classList.add("spin");
  setTimeout(() => applyTheme(next), 140);
  setTimeout(() => themeBtn.classList.remove("spin"), 650);
};

// --------------------------------------------------------- show password toggles
const EYE_OPEN = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><line x1="4" y1="4" x2="20" y2="20"/></svg>';

function addPasswordToggles() {
  document.querySelectorAll('input[type="password"]').forEach((input) => {
    if (input.dataset.pwToggle) return;
    input.dataset.pwToggle = "1";

    const wrap = document.createElement("div");
    wrap.className = "pw-field";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.setAttribute("aria-label", "Show password");
    btn.innerHTML = EYE_OPEN;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.innerHTML = show ? EYE_OFF : EYE_OPEN;
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      input.focus();
    });
    wrap.appendChild(btn);
  });
}

addPasswordToggles();

// --------------------------------------- tiny glowing starfield background
(function makeStars() {
  const bg = document.querySelector(".bg");
  if (!bg || bg.querySelector(".stars")) return;
  const layer = document.createElement("div");
  layer.className = "stars";
  layer.setAttribute("aria-hidden", "true");
  const area = window.innerWidth * window.innerHeight;
  const count = Math.min(120, Math.max(45, Math.round(area / 16000)));
  for (let i = 0; i < count; i++) {
    const s = document.createElement("span");
    s.className = "star";
    const r = Math.random();
    const size = r < 0.12 ? 3 : r < 0.45 ? 2 : 1;
    s.style.width = size + "px";
    s.style.height = size + "px";
    s.style.left = (Math.random() * 100).toFixed(2) + "%";
    s.style.top = (Math.random() * 100).toFixed(2) + "%";
    s.style.setProperty("--tw", (2.4 + Math.random() * 3.6).toFixed(2) + "s");
    s.style.animationDelay = (-Math.random() * 6).toFixed(2) + "s";
    layer.appendChild(s);
  }
  bg.appendChild(layer);

  // every half second, 1–3 random stars flare up briefly, then fade back
  setInterval(() => {
    const n = layer.children.length;
    if (!n) return;
    const picks = 1 + Math.floor(Math.random() * 3);
    for (let i = 0; i < picks; i++) {
      const s = layer.children[Math.floor(Math.random() * n)];
      if (!s || s.classList.contains("flare")) continue;
      s.classList.add("flare");
      setTimeout(() => s.classList.remove("flare"), 700 + Math.random() * 900);
    }
  }, 500);
})();

boot();
