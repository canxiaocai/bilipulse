/**
 * BiliPulse // SIGNAL STUDIO — Frontend Telemetry Controller
 * Integrates architectural DOM (@shell), kinetic CSS (@motion-css),
 * oscilloscope charts & motion (@viz), and synthetic stream (@controller).
 */

import {
  renderTelemetryChart,
  clearChartsInGroup,
} from "./charts.js?v=20261118z";
import {
  animateNumber,
  runViewTransition,
  staggerEnter,
  positionSlidingIndicator,
  prefersReducedMotion,
} from "./motion.js?v=20261118z";
import {
  createDemoDataset,
  tickDemoDataset,
  getDemoDashboard,
  getDemoVideoHistory,
  getDemoAccounts,
  switchDemoAccount,
  reorderDemoAccounts,
  buildFallbackCoverSvg,
  buildDemoAvatarSvg,
} from "./demo-data.js?v=20261118z";

const THEME_STORAGE_KEY = "bilipulse_theme";
const DEMO_PULSE_INTERVAL_MS = 3500;
const REAL_POLL_INTERVAL_MS = 30000;
const LEADERBOARD_PAGE_SIZE = 50;

const state = {
  hours: 24,
  theme: "dark",
  backendUid: "",
  activeUid: "",
  accounts: [],
  backendSettings: null,
  demoMode: false,
  demoLivePulse: true,
  demoDataset: null,
  dashboard: null,
  videos: [],
  onlineLimit: LEADERBOARD_PAGE_SIZE,
  playsLimit: LEADERBOARD_PAGE_SIZE,
  isManualCrawling: false,
  dashboardReqSeq: 0,
  accountModalMode: "add",
  accountModalUid: "",
  deleteTargetUid: "",
  adminAuth: {
    enabled: false,
    authenticated: true,
    source: "none",
  },
  pendingAdminAction: null,
  isSidebarMobile: false,
  isSidebarOpen: false,
  modalBvid: null,
  modalTitle: "",
  modalHours: 24,
  modalReqSeq: 0,
  modalPoints: null,
  clockTimer: null,
  demoTimer: null,
  pollTimer: null,
  onlineRowCache: new Map(),
  playsRowCache: new Map(),
  compareFollowersCache: new Map(),
  compareOnlineCache: new Map(),
  activeTab: "tab-overview",
  compareSortMode: "metric",
  accountReorderSeq: 0,
};

/* ---------- Escaping, URL Safety & Formatters ---------- */

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(url) {
  const raw = String(url ?? "").trim();
  if (!raw) return "";
  if (raw.startsWith("//")) {
    return `https:${raw}`;
  }
  if (raw.startsWith("https://") || raw.startsWith("http://") || raw.startsWith("/")) {
    return raw;
  }
  if (raw.startsWith("data:image/svg+xml")) {
    return raw;
  }
  return "";
}

function fmtNumber(val) {
  if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) {
    return "--";
  }
  return Math.round(Number(val)).toLocaleString("zh-CN");
}

function fmtCompact(val) {
  const num = Number(val);
  if (val === null || val === undefined || val === "" || Number.isNaN(num)) return "--";
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`;
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(1)}万`;
  return Math.round(num).toLocaleString("zh-CN");
}

function fmtDelta(delta, unit = "") {
  if (delta === null || delta === undefined || Number.isNaN(Number(delta))) {
    return `<span class="delta-flat">● 基准建立中</span>`;
  }
  const num = Math.round(Number(delta));
  if (num > 0) {
    return `<span class="delta-up">▲ +${fmtNumber(num)}${escapeHtml(unit)}</span> <span class="muted">较上次</span>`;
  }
  if (num < 0) {
    return `<span class="delta-down">▼ ${fmtNumber(num)}${escapeHtml(unit)}</span> <span class="muted">较上次</span>`;
  }
  return `<span class="delta-flat">● 0${escapeHtml(unit)} 持平</span> <span class="muted">较上次</span>`;
}

function fmtTime(isoStr) {
  if (!isoStr) return "--";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return String(isoStr);
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function fmtShortTime(isoStr) {
  if (!isoStr) return "--:--";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return "--:--";
  return d.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtEpochDate(sec) {
  const n = Number(sec || 0);
  if (!n || Number.isNaN(n)) return "--";
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

function fmtDuration(sec) {
  const total = Number(sec || 0);
  if (!total || Number.isNaN(total) || total <= 0) return "--:--";
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = Math.floor(total % 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function resetNumericReadout(el, placeholder = "--") {
  if (!el) return;
  if (el._rafId) {
    cancelAnimationFrame(el._rafId);
    el._rafId = 0;
  }
  el.classList.remove("is-rolling", "pulse-up", "pulse-down");
  delete el.dataset.numValue;
  el.textContent = placeholder;
}

/* ---------- HUD Toast Stack ---------- */

function showToast(message, type = "ok") {
  const stack = document.getElementById("toastStack");
  if (!stack) return;

  const isErr = type === "err" || type === "error";
  const isWarn = type === "warn";
  const toneClass = isErr ? "err error is-err" : isWarn ? "warn is-warn" : "ok is-ok";
  const codeText = isErr ? "错误" : isWarn ? "提示" : "完成";

  const toast = document.createElement("div");
  toast.className = `hud-toast toast ${toneClass}`;
  toast.innerHTML = `<span class="mono" style="opacity:0.78;margin-right:8px">${codeText}</span><span>${escapeHtml(message)}</span>`;

  stack.appendChild(toast);

  const dismiss = () => {
    if (!toast.isConnected) return;
    toast.classList.add("is-leaving", "leaving");
    setTimeout(() => toast.remove(), 260);
  };

  setTimeout(dismiss, 3800);
}

/* ---------- Network Client ---------- */

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await resp.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    if (resp.status === 401 && !String(url).includes("/api/admin/verify")) {
      state.adminAuth = {
        ...state.adminAuth,
        enabled: true,
        authenticated: false,
      };
      syncAdminAuthControls();
    }
    const detail =
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : data.message || `HTTP ${resp.status}`;
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return data;
}

/* ---------- DOM Reordering & Visually Hidden Status Helper (modern-web-guidance: dynamic-sort) ---------- */

function createVisuallyHiddenStatus(id) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement("p");
  el.id = id;
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  Object.assign(el.style, {
    position: "absolute",
    width: "1px",
    height: "1px",
    margin: "-1px",
    padding: "0",
    border: "0",
    overflow: "hidden",
    clipPath: "inset(50%)",
    whiteSpace: "nowrap",
  });
  document.body.appendChild(el);
  return el;
}

/**
 * Reorders existing child nodes inside `container` using `Element.prototype.moveBefore`
 * (preserving focus, image decode state, and CSS transitions) with fallback to `append`.
 */
function reorderChildren(container, orderedNodes) {
  if (!container) return;
  const canMoveBefore =
    typeof Element !== "undefined" && Object.hasOwn(Element.prototype, "moveBefore");
  const move = canMoveBefore
    ? (node) => {
        if (node.parentNode === container) {
          container.moveBefore(node, null);
        } else {
          container.appendChild(node);
        }
      }
    : (node) => container.append(node);

  for (const node of orderedNodes) {
    move(node);
  }

  // Prune any stale element children not in the target set
  const keepSet = new Set(orderedNodes);
  for (const child of Array.from(container.children)) {
    if (!keepSet.has(child)) {
      child.remove();
    }
  }
}

/* ---------- Theme & View Transitions ---------- */

function getStoredTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Ignore storage access errors
  }
  return "dark";
}

function applyTheme(theme, persist = true) {
  const normalized = theme === "light" ? "light" : "dark";
  state.theme = normalized;
  document.documentElement.dataset.theme = normalized;

  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, normalized);
    } catch {
      // Ignore storage access errors
    }
  }

  const sunIcon = document.getElementById("themeIconSun");
  const moonIcon = document.getElementById("themeIconMoon");
  const label = document.getElementById("themeLabel");
  const toggleBtn = document.getElementById("btnThemeToggle");

  if (sunIcon) sunIcon.classList.toggle("hidden", normalized === "dark");
  if (moonIcon) moonIcon.classList.toggle("hidden", normalized === "light");
  if (label) label.textContent = normalized === "dark" ? "暗夜" : "浅色";
  if (toggleBtn) {
    toggleBtn.setAttribute("aria-pressed", String(normalized === "dark"));
    toggleBtn.title = normalized === "dark" ? "切换到浅色主题" : "切换到暗夜主题";
  }
}

function toggleTheme() {
  const btn = document.getElementById("btnThemeToggle");
  const nextTheme = state.theme === "dark" ? "light" : "dark";

  runViewTransition(
    () => {
      applyTheme(nextTheme, true);
      if (state.dashboard) {
        renderOverviewCharts(state.dashboard, false);
      }
      if (state.modalBvid && state.modalPoints) {
        const modal = document.getElementById("videoModal");
        if (modal && modal.open) {
          renderModalCharts(state.modalPoints, false);
        }
      }
    },
    { originEl: btn, type: "theme" }
  );
}

/* ---------- Top Laser Progress ---------- */

function updateLiveClockAndLaser() {
  const nowMs = Date.now();

  const laserBar = document.getElementById("crawlLaserBar");
  const laserFill = document.getElementById("crawlLaserFill");

  const sched = state.dashboard?.scheduler;
  const isCrawling = Boolean(state.isManualCrawling || sched?.is_crawling);

  if (laserBar) {
    laserBar.classList.toggle("is-crawling", isCrawling);
  }

  if (isCrawling) {
    if (laserFill) laserFill.style.width = "100%";
    return;
  }

  if (state.demoMode) {
    const intervalMs = Math.max(60000, Number(sched?.interval_minutes || 5) * 60 * 1000);
    let nextMs = Date.parse(sched?.next_run_time || "");
    if (Number.isNaN(nextMs) || nextMs <= nowMs) {
      nextMs = nowMs + intervalMs;
      if (sched) sched.next_run_time = new Date(nextMs).toISOString();
    }
    const remMs = Math.max(0, nextMs - nowMs);
    const pct = Math.min(100, Math.max(0, ((intervalMs - remMs) / intervalMs) * 100));
    if (laserFill) laserFill.style.width = `${pct.toFixed(2)}%`;
    return;
  }

  const hasEnabledAccounts = Number(sched?.enabled_accounts_count || 0) > 0 || Boolean(state.backendUid);
  if (!hasEnabledAccounts || !sched || !sched.enabled || !sched.running || !sched.next_run_time) {
    if (laserFill) laserFill.style.width = "0%";
    return;
  }

  const intervalMs = Math.max(60000, Number(sched.interval_minutes || 5) * 60 * 1000);
  const nextMs = Date.parse(sched.next_run_time);
  if (Number.isNaN(nextMs)) {
    if (laserFill) laserFill.style.width = "0%";
    return;
  }

  const remMs = Math.max(0, nextMs - nowMs);
  const pct = Math.min(100, Math.max(0, ((intervalMs - remMs) / intervalMs) * 100));
  if (laserFill) laserFill.style.width = `${pct.toFixed(2)}%`;
}

/* ---------- Sliding Indicators ---------- */

function syncModalRangeControls() {
  const group = document.getElementById("modalRangeGroup");
  if (!group) return;
  const targetHours = Number(state.modalHours ?? 24);
  const btns = Array.from(group.querySelectorAll(".range-btn"));
  let activeBtn = null;

  for (const btn of btns) {
    const isMatch = Number(btn.dataset.hours) === targetHours;
    btn.classList.toggle("active", isMatch);
    btn.setAttribute("aria-pressed", String(isMatch));
    if (isMatch) activeBtn = btn;
  }

  if (!activeBtn && btns.length > 0) {
    activeBtn = btns[0];
    activeBtn.classList.add("active");
    activeBtn.setAttribute("aria-pressed", "true");
  }

  if (!("focusGroup" in HTMLElement.prototype) && !group.contains(document.activeElement)) {
    btns.forEach((b) => {
      b.tabIndex = b === activeBtn ? 0 : -1;
    });
  }

  const modal = document.getElementById("videoModal");
  const modalBar = document.getElementById("modalRangeActiveBar");
  if (modal?.open && modalBar && activeBtn) {
    positionSlidingIndicator(modalBar, activeBtn, "x");
  }
}

function syncSlidingIndicators() {
  const rangeBar = document.getElementById("rangeActiveBar");
  const activeRange = document.querySelector("#rangeGroup .range-btn.active");
  if (rangeBar && activeRange) {
    positionSlidingIndicator(rangeBar, activeRange, "x");
  }
  const modal = document.getElementById("videoModal");
  if (modal && modal.open) {
    const modalBar = document.getElementById("modalRangeActiveBar");
    const activeModalRange = document.querySelector(
      '#modalRangeGroup .range-btn[aria-pressed="true"]'
    );
    if (modalBar && activeModalRange) {
      positionSlidingIndicator(modalBar, activeModalRange, "x");
    }
  }
}

/* ---------- Demo Mode & Live Stream Lifecycle ---------- */

function syncBannersAndDemoControls() {
  const setupBanner = document.getElementById("setupBanner");
  const demoBanner = document.getElementById("demoBanner");
  const demoPulseStatus = document.getElementById("demoPulseStatus");
  const btnLivePulse = document.getElementById("btnLivePulse");
  const btnLoadDemo = document.getElementById("btnLoadDemo");

  const isUnbound = !state.backendUid;

  // Show #setupBanner whenever backend has no real UID bound (even during auto-preview demo)
  if (setupBanner) {
    setupBanner.classList.toggle("hidden", !isUnbound);
  }
  if (btnLoadDemo) {
    btnLoadDemo.textContent = state.demoMode ? "重置示例波形" : "加载示例数据";
  }

  if (demoBanner) {
    demoBanner.classList.toggle("hidden", !state.demoMode);
  }
  if (demoPulseStatus && state.demoMode) {
    demoPulseStatus.textContent = state.demoLivePulse
      ? "本地示例数据流运行中 · 每 3.5 秒实时采样（不写入数据库）"
      : "本地示例数据流已冻结 · 点击顶部「实时脉冲」恢复动态采样";
  }

  if (btnLivePulse) {
    btnLivePulse.classList.toggle("hidden", !state.demoMode);
    btnLivePulse.classList.toggle("is-active", Boolean(state.demoMode && state.demoLivePulse));
    btnLivePulse.setAttribute("aria-pressed", String(Boolean(state.demoMode && state.demoLivePulse)));
    const textSpan = btnLivePulse.querySelector("span:last-child");
    if (textSpan) {
      textSpan.textContent = state.demoLivePulse ? "实时脉冲 · 运行中" : "实时脉冲 · 已暂停";
    }
  }
}

function startDemoPulseLoop() {
  stopDemoPulseLoop();
  state.demoTimer = setInterval(() => {
    if (!state.demoMode || !state.demoLivePulse || document.hidden) return;
    tickDemoDataset(state);
    const demoData = getDemoDashboard(state, state.hours);
    renderDashboard(demoData, { animateCharts: false, isLiveTick: true });

    if (state.modalBvid) {
      const modal = document.getElementById("videoModal");
      if (modal && modal.open) {
        refreshOpenDemoModal(state.modalBvid);
      }
    }
  }, DEMO_PULSE_INTERVAL_MS);
}

function stopDemoPulseLoop() {
  if (state.demoTimer) {
    clearInterval(state.demoTimer);
    state.demoTimer = null;
  }
}

function clearRankCaches({ resetLimits = false } = {}) {
  state.onlineRowCache.clear();
  state.playsRowCache.clear();
  state.compareFollowersCache.clear();
  state.compareOnlineCache.clear();
  if (resetLimits) {
    state.onlineLimit = LEADERBOARD_PAGE_SIZE;
    state.playsLimit = LEADERBOARD_PAGE_SIZE;
  }
}

function activateDemoMode({ showNotification = true } = {}) {
  state.demoMode = true;
  state.demoLivePulse = true;
  state.demoDataset = createDemoDataset();
  clearRankCaches({ resetLimits: true });

  startDemoPulseLoop();
  syncBannersAndDemoControls();

  const demoData = getDemoDashboard(state, state.hours);
  renderDashboard(demoData, { animateCharts: true, isLiveTick: false });

  if (showNotification) {
    showToast("已接入本地示例数据（30天多尺度仿真）");
  }
}

function toggleDemoLivePulse() {
  if (!state.demoMode) return;
  state.demoLivePulse = !state.demoLivePulse;
  syncBannersAndDemoControls();
  updateLiveClockAndLaser();
  showToast(state.demoLivePulse ? "实时脉冲推流已恢复" : "实时脉冲已暂停，波形定格");
}

async function exitDemoMode() {
  state.demoMode = false;
  state.demoLivePulse = false;
  state.demoDataset = null;
  stopDemoPulseLoop();
  clearRankCaches({ resetLimits: true });
  clearChartsInGroup("overview");

  syncBannersAndDemoControls();
  await loadDashboard({ animateCharts: true });
  showToast(
    state.backendUid
      ? "已退出示例流，切换回真实监控总线"
      : "已退出示例预览，当前显示未绑定原始空状态"
  );
}

/* ---------- Left Sidebar: Multi-Account List & Responsive Drawer ---------- */

function getAccountDotClass(acc, crawlingUid) {
  if (acc.is_crawling || (crawlingUid && crawlingUid === acc.uid)) {
    return "scheduler-crawling";
  }
  if (!acc.enabled) {
    return "scheduler-paused";
  }
  if (acc.last_crawl_status === "error") {
    return "scheduler-error";
  }
  return "scheduler-running";
}

const ACCOUNT_REORDER_HINT_IDLE = "空格排序";
const ACCOUNT_REORDER_HINT_GRABBED = "↑↓移动 · 空格确认 · Esc取消";

const accountDrag = {
  active: false,
  pointerId: null,
  item: null,
  handle: null,
  startY: 0,
  lastClientX: 0,
  lastClientY: 0,
  startScrollTop: 0,
  referenceNode: null,
  initialNextSibling: null,
  generation: 0,
  rafId: 0,
  lastFrameTime: 0,
  pendingSidebarSync: false,
  cleanupDocListeners: null,
};

const accountKb = {
  active: false,
  item: null,
  handle: null,
  originalNextSibling: null,
  originalUids: [],
  isMovingNode: false,
  announceTimer: 0,
};

function getAccountRows(listEl) {
  if (!listEl) return [];
  return Array.from(listEl.children).filter(
    (el) => el instanceof HTMLElement && el.classList.contains("account-nav-row")
  );
}

function getOrderedAccountUids(listEl) {
  return getAccountRows(listEl)
    .map((row) => row.dataset.uid || "")
    .filter(Boolean);
}

function getAccountDisplayName(row) {
  return row?.querySelector(".account-meta-name")?.textContent?.trim() || row?.dataset.uid || "账号";
}

function announceAccountReorder(message, delayMs = 0) {
  const statusEl = createVisuallyHiddenStatus("accountReorderStatus");
  if (accountKb.announceTimer) {
    clearTimeout(accountKb.announceTimer);
    accountKb.announceTimer = 0;
  }
  statusEl.textContent = "";
  if (!message) return;
  if (delayMs > 0) {
    accountKb.announceTimer = setTimeout(() => {
      accountKb.announceTimer = 0;
      statusEl.textContent = message;
    }, delayMs);
  } else {
    requestAnimationFrame(() => {
      statusEl.textContent = message;
    });
  }
}

function moveAccountNodeBefore(container, item, referenceNode) {
  if (typeof Element !== "undefined" && Object.hasOwn(Element.prototype, "moveBefore")) {
    container.moveBefore(item, referenceNode || null);
    return false;
  }
  if (referenceNode) {
    referenceNode.before(item);
  } else {
    container.append(item);
  }
  return true;
}

function createAccountRowActions(uid, displayName) {
  const actions = document.createElement("div");
  actions.className = "account-row-actions";
  actions.innerHTML = `
    <button type="button" class="account-icon-btn" data-account-action="edit" data-uid="${escapeHtml(uid)}" title="配置账号 ${escapeHtml(displayName)}" aria-label="配置账号 ${escapeHtml(displayName)}">
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.08a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
      </svg>
    </button>
    <button type="button" class="account-icon-btn is-danger" data-account-action="delete" data-uid="${escapeHtml(uid)}" title="移除账号 ${escapeHtml(displayName)}" aria-label="移除账号 ${escapeHtml(displayName)}">
      <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    </button>
  `;
  return actions;
}

function createAccountRowElement(uid) {
  const row = document.createElement("div");
  row.className = "account-nav-row";
  row.dataset.uid = uid;
  row.innerHTML = `
    <button type="button" class="account-drag-handle" aria-pressed="false" aria-label="拖动调整账号顺序">
      <svg viewBox="0 0 12 18" width="11" height="16" fill="currentColor" aria-hidden="true">
        <circle cx="3.5" cy="3.5" r="1.25" />
        <circle cx="8.5" cy="3.5" r="1.25" />
        <circle cx="3.5" cy="9" r="1.25" />
        <circle cx="8.5" cy="9" r="1.25" />
        <circle cx="3.5" cy="14.5" r="1.25" />
        <circle cx="8.5" cy="14.5" r="1.25" />
      </svg>
    </button>
    <button type="button" class="account-switch-btn" data-account-switch="${escapeHtml(uid)}" aria-current="false">
      <div class="account-avatar-box">
        <img referrerpolicy="no-referrer" alt="" />
      </div>
      <div class="account-meta">
        <div class="account-meta-top">
          <span class="account-meta-name"></span>
          <span class="account-demo-badge mono hidden">示例</span>
          <span class="signal-dot"></span>
        </div>
        <span class="account-reorder-hint mono" aria-hidden="true">${ACCOUNT_REORDER_HINT_IDLE}</span>
      </div>
    </button>
  `;
  const imgEl = row.querySelector(".account-avatar-box img");
  if (imgEl) {
    attachCoverFallback(imgEl, "UP");
  }
  return row;
}

function updateAccountRowElement(row, acc, { isActive, isDemo, crawlingUid, canReorder }) {
  const uid = String(acc.uid || "");
  const displayName = acc.account_name || `账号 ${uid}`;
  const avatarSrc = safeUrl(acc.account_face) || buildDemoAvatarSvg();
  const dotClass = getAccountDotClass(acc, crawlingUid);

  row.dataset.uid = uid;
  row.classList.toggle("is-active", isActive);

  const handle = row.querySelector(".account-drag-handle");
  if (handle) {
    handle.hidden = !canReorder;
    const handleLabel = `调整 ${displayName} 的排序`;
    if (handle.getAttribute("aria-label") !== handleLabel) {
      handle.setAttribute("aria-label", handleLabel);
    }
    if (!canReorder) {
      handle.setAttribute("aria-pressed", "false");
    }
  }

  const switchBtn = row.querySelector(".account-switch-btn");
  if (switchBtn) {
    switchBtn.dataset.accountSwitch = uid;
    switchBtn.setAttribute("aria-current", isActive ? "true" : "false");
  }

  const imgEl = row.querySelector(".account-avatar-box img");
  if (imgEl && imgEl.dataset.rawSrc !== avatarSrc) {
    imgEl.dataset.rawSrc = avatarSrc;
    imgEl.src = avatarSrc;
  }

  const nameEl = row.querySelector(".account-meta-name");
  if (nameEl && nameEl.textContent !== displayName) {
    nameEl.textContent = displayName;
  }

  const demoTagEl = row.querySelector(".account-demo-badge");
  if (demoTagEl) {
    demoTagEl.classList.toggle("hidden", !isDemo);
  }

  const dotEl = row.querySelector(".signal-dot");
  if (dotEl) {
    dotEl.className = `signal-dot ${dotClass}`;
    dotEl.title = acc.enabled ? "监控中" : "已暂停";
  }

  let actionsEl = row.querySelector(".account-row-actions");
  if (isDemo) {
    if (actionsEl) actionsEl.remove();
  } else {
    if (!actionsEl) {
      actionsEl = createAccountRowActions(uid, displayName);
      row.appendChild(actionsEl);
    } else {
      const editBtn = actionsEl.querySelector('[data-account-action="edit"]');
      const delBtn = actionsEl.querySelector('[data-account-action="delete"]');
      if (editBtn) {
        editBtn.dataset.uid = uid;
        editBtn.title = `配置账号 ${displayName}`;
        editBtn.setAttribute("aria-label", `配置账号 ${displayName}`);
      }
      if (delBtn) {
        delBtn.dataset.uid = uid;
        delBtn.title = `移除账号 ${displayName}`;
        delBtn.setAttribute("aria-label", `移除账号 ${displayName}`);
      }
    }
  }
}

function renderAccountSidebar() {
  const listEl = document.getElementById("accountRailList");
  const countEl = document.getElementById("sidebarAccountCount");
  if (!listEl) return;

  const isDemo = state.demoMode;
  const accounts = isDemo ? getDemoAccounts(state) : state.accounts || [];
  const activeUid = isDemo
    ? state.demoDataset?.activeUid || accounts[0]?.uid || ""
    : state.activeUid || state.backendUid || "";
  const crawlingUid = state.dashboard?.scheduler?.crawling_uid || null;

  if (countEl) {
    countEl.textContent = String(accounts.length);
  }

  if (accountDrag.active || accountKb.active) {
    accountDrag.pendingSidebarSync = true;
    return;
  }

  if (accounts.length === 0) {
    listEl.innerHTML = `<div class="account-rail-empty mono">暂无监控账号<br /><span class="muted">点击上方按钮添加 B站 UID</span></div>`;
    return;
  }

  const existingByUid = new Map();
  for (const row of getAccountRows(listEl)) {
    if (row.dataset.uid) {
      existingByUid.set(row.dataset.uid, row);
    }
  }

  const canReorder = accounts.length >= 2;
  const orderedRows = [];
  for (const acc of accounts) {
    const uid = String(acc.uid || "");
    if (!uid) continue;
    let row = existingByUid.get(uid);
    if (!row) {
      row = createAccountRowElement(uid);
    }
    updateAccountRowElement(row, acc, {
      isActive: uid === activeUid,
      isDemo,
      crawlingUid,
      canReorder,
    });
    orderedRows.push(row);
  }

  reorderChildren(listEl, orderedRows);
}

/* ---------- Account Drag-and-Drop & Keyboard Reordering ---------- */

function getFixedContainingBlockOffset(listEl) {
  let node = listEl?.parentElement;
  while (node && node !== document.documentElement) {
    const style = window.getComputedStyle(node);
    const hasTransform = style.transform && style.transform !== "none";
    const hasFilter = style.filter && style.filter !== "none";
    const hasPerspective = style.perspective && style.perspective !== "none";
    const hasContain = style.contain && /\b(paint|layout|strict|content)\b/.test(style.contain);
    const hasWillChange =
      style.willChange && /\b(transform|filter|perspective)\b/.test(style.willChange);
    if (hasTransform || hasFilter || hasPerspective || hasContain || hasWillChange) {
      const rect = node.getBoundingClientRect();
      return {
        left: rect.left + (node.clientLeft || 0),
        top: rect.top + (node.clientTop || 0),
      };
    }
    node = node.parentElement;
  }
  return { left: 0, top: 0 };
}

function clearDropMarker(listEl) {
  if (!listEl) return;
  delete listEl.dataset.drop;
  listEl.style.removeProperty("--drop-x");
  listEl.style.removeProperty("--drop-y");
  listEl.style.removeProperty("--drop-w");
  listEl.style.removeProperty("--drop-h");
}

function clearSiblingShifts(listEl) {
  for (const row of getAccountRows(listEl)) {
    row.classList.remove("is-sibling-shifting");
    if (!row.classList.contains("is-dragging")) {
      row.style.translate = "";
    }
  }
}

function computeDropReferenceNode(listEl, draggedItem, clientY) {
  const listRect = listEl.getBoundingClientRect();
  const clampedY = Math.max(listRect.top, Math.min(listRect.bottom, clientY));
  const pointerContentY =
    clampedY - listRect.top - (listEl.clientTop || 0) + listEl.scrollTop;

  for (const row of getAccountRows(listEl)) {
    if (row === draggedItem) continue;
    const midpoint = row.offsetTop + row.offsetHeight / 2;
    if (midpoint > pointerContentY) {
      return row;
    }
  }
  return null;
}

function updateDropMarker(listEl, draggedItem, referenceNode) {
  if (!listEl || !draggedItem) return;
  const rows = getAccountRows(listEl);
  const origIdx = rows.indexOf(draggedItem);
  if (origIdx === -1 || rows.length < 2) {
    clearDropMarker(listEl);
    clearSiblingShifts(listEl);
    return;
  }

  let insertIdx = referenceNode ? rows.indexOf(referenceNode) : rows.length;
  if (insertIdx === -1) insertIdx = rows.length;

  const itemHeight = draggedItem.offsetHeight || 52;
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    if (row === draggedItem) continue;
    row.classList.add("is-sibling-shifting");
    let shift = 0;
    if (insertIdx < origIdx && i >= insertIdx && i < origIdx) {
      shift = itemHeight;
    } else if (insertIdx > origIdx + 1 && i > origIdx && i < insertIdx) {
      shift = -itemHeight;
    }
    row.style.translate = shift === 0 ? "" : `0px ${shift}px`;
  }

  if (insertIdx === origIdx || insertIdx === origIdx + 1) {
    clearDropMarker(listEl);
    return;
  }

  let slotTop = 0;
  let anchorRow = null;
  if (insertIdx < origIdx) {
    anchorRow = rows[insertIdx];
    slotTop = anchorRow.offsetTop;
  } else {
    anchorRow = rows[insertIdx - 1];
    slotTop = anchorRow.offsetTop + anchorRow.offsetHeight - itemHeight;
  }

  const listRect = listEl.getBoundingClientRect();
  const anchorRect = anchorRow.getBoundingClientRect();
  const viewportTop =
    listRect.top + (listEl.clientTop || 0) - listEl.scrollTop + slotTop;
  const viewportBottom = viewportTop + itemHeight;
  const visTop = Math.max(listRect.top, viewportTop);
  const visBottom = Math.min(listRect.bottom, viewportBottom);

  if (visBottom - visTop <= 2) {
    clearDropMarker(listEl);
    return;
  }

  const cbOffset = getFixedContainingBlockOffset(listEl);

  listEl.dataset.drop = "vertical";
  listEl.style.setProperty("--drop-x", `${Math.round(anchorRect.left - cbOffset.left)}px`);
  listEl.style.setProperty("--drop-y", `${Math.round(visTop - cbOffset.top)}px`);
  listEl.style.setProperty("--drop-w", `${Math.round(anchorRow.offsetWidth)}px`);
  listEl.style.setProperty("--drop-h", `${Math.round(visBottom - visTop)}px`);
}

function captureRowRects(listEl) {
  const rects = new Map();
  for (const row of getAccountRows(listEl)) {
    rects.set(row, row.getBoundingClientRect());
  }
  return rects;
}

function animateAccountFlip(listEl, beforeRects, movedItem, generation) {
  if (prefersReducedMotion()) return;
  for (const row of getAccountRows(listEl)) {
    const before = beforeRects.get(row);
    if (!before) continue;
    const after = row.getBoundingClientRect();
    const dx = before.left - after.left;
    const dy = before.top - after.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;

    if (typeof row.animate === "function") {
      const fromFrame = row === movedItem
        ? { translate: `${dx}px ${dy}px`, scale: "1.008" }
        : { translate: `${dx}px ${dy}px` };
      const toFrame = row === movedItem
        ? { translate: "0px 0px", scale: "1" }
        : { translate: "0px 0px" };

      if (row === movedItem) {
        row.classList.add("is-settling");
      }
      const anim = row.animate([fromFrame, toFrame], {
        duration: 220,
        easing: "cubic-bezier(0.22, 1.2, 0.36, 1)",
      });
      if (row === movedItem) {
        anim.finished
          .catch(() => {})
          .finally(() => {
            if (accountDrag.generation === generation) {
              row.classList.remove("is-settling");
            }
          });
      }
    }
  }
}

async function persistAccountOrder(listEl, orderedUids) {
  if (!orderedUids || orderedUids.length < 2) return;

  if (state.demoMode) {
    reorderDemoAccounts(state, orderedUids);
    renderAccountSidebar();
    renderCompareBoard();
    return;
  }

  const prevAccounts = [...(state.accounts || [])];
  const byUid = new Map(prevAccounts.map((a) => [String(a.uid), a]));
  const optimistic = [];
  const seen = new Set();
  for (let i = 0; i < orderedUids.length; i += 1) {
    const uid = orderedUids[i];
    const acc = byUid.get(uid);
    if (acc && !seen.has(uid)) {
      seen.add(uid);
      optimistic.push({ ...acc, sort_order: i });
    }
  }
  for (const acc of prevAccounts) {
    const uid = String(acc.uid);
    if (!seen.has(uid)) {
      optimistic.push({ ...acc, sort_order: optimistic.length });
    }
  }
  state.accounts = optimistic;

  const seq = ++state.accountReorderSeq;
  try {
    const res = await apiFetch("/api/accounts/reorder", {
      method: "PUT",
      body: JSON.stringify({ uids: orderedUids }),
    });
    if (seq !== state.accountReorderSeq) return;
    if (Array.isArray(res.accounts)) {
      state.accounts = res.accounts;
    }
    if (!accountDrag.active && !accountKb.active) {
      renderAccountSidebar();
      renderCompareBoard();
    }
  } catch (err) {
    if (seq !== state.accountReorderSeq) return;
    state.accounts = prevAccounts;
    renderAccountSidebar();
    showToast(`调整账号顺序失败: ${err.message}`, "err");
  }
}

function setupAccountRailReorder() {
  const listEl = document.getElementById("accountRailList");
  if (!listEl || listEl.dataset.reorderBound === "1") return;
  listEl.dataset.reorderBound = "1";

  const stopAutoScroll = () => {
    if (accountDrag.rafId) {
      cancelAnimationFrame(accountDrag.rafId);
      accountDrag.rafId = 0;
    }
    accountDrag.lastFrameTime = 0;
  };

  const syncDraggedItemTranslate = () => {
    if (!accountDrag.active || !accountDrag.item) return;
    const scrollDelta = listEl.scrollTop - accountDrag.startScrollTop;
    const dy = accountDrag.lastClientY - accountDrag.startY + scrollDelta;
    accountDrag.item.style.translate = `0px ${dy}px`;
  };

  const updatePointerDropSpot = () => {
    if (!accountDrag.active || !accountDrag.item) return;
    accountDrag.referenceNode = computeDropReferenceNode(
      listEl,
      accountDrag.item,
      accountDrag.lastClientY
    );
    updateDropMarker(listEl, accountDrag.item, accountDrag.referenceNode);
  };

  const startAutoScrollLoop = () => {
    stopAutoScroll();
    const tick = (now) => {
      if (!accountDrag.active || !accountDrag.item) return;
      const dt = accountDrag.lastFrameTime
        ? Math.min(48, Math.max(4, now - accountDrag.lastFrameTime))
        : 16.67;
      accountDrag.lastFrameTime = now;

      if (listEl.scrollHeight > listEl.clientHeight) {
        const listRect = listEl.getBoundingClientRect();
        const zone = Math.min(accountDrag.item.offsetHeight || 52, listEl.clientHeight / 3);
        const y = accountDrag.lastClientY;
        let velocity = 0;

        if (y < listRect.top + zone) {
          const ratio = Math.min(2.5, (listRect.top + zone - y) / Math.max(1, zone));
          velocity = -Math.max(1.5, ratio * 11);
        } else if (y > listRect.bottom - zone) {
          const ratio = Math.min(2.5, (y - (listRect.bottom - zone)) / Math.max(1, zone));
          velocity = Math.max(1.5, ratio * 11);
        }

        if (velocity !== 0) {
          const prevScroll = listEl.scrollTop;
          listEl.scrollTop += velocity * (dt / 16.67);
          if (listEl.scrollTop !== prevScroll) {
            syncDraggedItemTranslate();
            updatePointerDropSpot();
          }
        }
      }

      accountDrag.rafId = requestAnimationFrame(tick);
    };
    accountDrag.rafId = requestAnimationFrame(tick);
  };

  const cleanupActiveDrag = () => {
    stopAutoScroll();
    clearDropMarker(listEl);
    document.documentElement.classList.remove("is-account-dragging");
    if (accountDrag.item) {
      accountDrag.item.classList.remove("is-dragging");
    }
    if (typeof accountDrag.cleanupDocListeners === "function") {
      accountDrag.cleanupDocListeners();
      accountDrag.cleanupDocListeners = null;
    }
    if (
      accountDrag.handle &&
      accountDrag.pointerId !== null &&
      typeof accountDrag.handle.hasPointerCapture === "function"
    ) {
      try {
        if (accountDrag.handle.hasPointerCapture(accountDrag.pointerId)) {
          accountDrag.handle.releasePointerCapture(accountDrag.pointerId);
        }
      } catch {
        // Ignore if capture already released
      }
    }
    accountDrag.active = false;
    accountDrag.pointerId = null;
    accountDrag.item = null;
    accountDrag.handle = null;
    accountDrag.referenceNode = null;
    accountDrag.initialNextSibling = null;
  };

  const finishPointerDrag = ({ cancel = false } = {}) => {
    if (!accountDrag.active || !accountDrag.item) return;
    const item = accountDrag.item;
    const targetRef = accountDrag.referenceNode;
    const gen = accountDrag.generation;

    const beforeRects = captureRowRects(listEl);
    const didChange =
      !cancel && targetRef !== item && targetRef !== item.nextElementSibling;

    cleanupActiveDrag();

    for (const row of getAccountRows(listEl)) {
      row.classList.remove("is-sibling-shifting");
      row.getAnimations?.().forEach((a) => a.finish());
      row.style.translate = "";
    }

    if (didChange) {
      moveAccountNodeBefore(listEl, item, targetRef);
    }
    if (didChange || cancel) {
      item.scrollIntoView({ block: "nearest", inline: "nearest" });
    }

    animateAccountFlip(listEl, beforeRects, item, gen);

    if (cancel) {
      announceAccountReorder(`已取消调整 ${getAccountDisplayName(item)} 的顺序`);
    } else if (didChange) {
      const orderedUids = getOrderedAccountUids(listEl);
      const pos = orderedUids.indexOf(item.dataset.uid || "") + 1;
      announceAccountReorder(
        `${getAccountDisplayName(item)} 已移动到第 ${pos} 位，共 ${orderedUids.length} 项`
      );
      persistAccountOrder(listEl, orderedUids);
    }

    if (accountDrag.pendingSidebarSync) {
      accountDrag.pendingSidebarSync = false;
      renderAccountSidebar();
    }
  };

  listEl.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || accountDrag.active) return;
    const handle = ev.target.closest(".account-drag-handle");
    if (!handle || handle.hidden) return;
    if (!state.demoMode && state.adminAuth.enabled && !state.adminAuth.authenticated) {
      ev.preventDefault();
      ensureAdminAuthorized("调整账号排序");
      return;
    }
    const item = handle.closest(".account-nav-row");
    if (!item || item.parentElement !== listEl) return;
    if (getAccountRows(listEl).length < 2) return;

    if (accountKb.active) {
      finishKeyboardGrab({ cancel: false, announce: false });
    }

    ev.preventDefault();

    for (const row of getAccountRows(listEl)) {
      row.getAnimations?.().forEach((a) => a.finish());
    }

    const gen = ++accountDrag.generation;
    accountDrag.active = true;
    accountDrag.pointerId = ev.pointerId;
    accountDrag.item = item;
    accountDrag.handle = handle;
    accountDrag.startY = ev.clientY;
    accountDrag.lastClientX = ev.clientX;
    accountDrag.lastClientY = ev.clientY;
    accountDrag.startScrollTop = listEl.scrollTop;
    accountDrag.referenceNode = item.nextElementSibling;
    accountDrag.initialNextSibling = item.nextElementSibling;

    document.documentElement.classList.add("is-account-dragging");
    item.classList.remove("is-settling");
    item.classList.add("is-dragging");

    try {
      handle.setPointerCapture(ev.pointerId);
    } catch {
      // Ignore environments without pointer capture support
    }

    const onPointerMove = (moveEv) => {
      if (!accountDrag.active || moveEv.pointerId !== accountDrag.pointerId) return;
      accountDrag.lastClientX = moveEv.clientX;
      accountDrag.lastClientY = moveEv.clientY;
      syncDraggedItemTranslate();
      updatePointerDropSpot();
    };

    const onPointerUp = (upEv) => {
      if (!accountDrag.active || upEv.pointerId !== accountDrag.pointerId) return;
      accountDrag.lastClientX = upEv.clientX;
      accountDrag.lastClientY = upEv.clientY;
      updatePointerDropSpot();
      finishPointerDrag({ cancel: false });
    };

    const onPointerCancel = (cancelEv) => {
      if (!accountDrag.active || cancelEv.pointerId !== accountDrag.pointerId) return;
      finishPointerDrag({ cancel: true });
    };

    const onLostCapture = (capEv) => {
      if (!accountDrag.active || capEv.pointerId !== accountDrag.pointerId) return;
      finishPointerDrag({ cancel: false });
    };

    const onKeyDown = (keyEv) => {
      if (!accountDrag.active || accountDrag.generation !== gen) return;
      if (keyEv.key === "Escape") {
        keyEv.preventDefault();
        keyEv.stopPropagation();
        finishPointerDrag({ cancel: true });
      }
    };

    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerCancel);
    document.addEventListener("keydown", onKeyDown, true);
    handle.addEventListener("lostpointercapture", onLostCapture);

    accountDrag.cleanupDocListeners = () => {
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerup", onPointerUp);
      document.removeEventListener("pointercancel", onPointerCancel);
      document.removeEventListener("keydown", onKeyDown, true);
      handle.removeEventListener("lostpointercapture", onLostCapture);
    };

    startAutoScrollLoop();
  });

  function setKeyboardHintText(item, text) {
    const hint = item?.querySelector(".account-reorder-hint");
    if (hint) hint.textContent = text;
  }

  function finishKeyboardGrab({ cancel = false, announce = true } = {}) {
    if (!accountKb.active || !accountKb.item) return;
    const item = accountKb.item;
    const handle = accountKb.handle;
    const origNext = accountKb.originalNextSibling;
    const origUids = accountKb.originalUids;

    if (cancel && origNext !== item && origNext !== item.nextElementSibling) {
      const beforeRects = captureRowRects(listEl);
      accountKb.isMovingNode = true;
      moveAccountNodeBefore(listEl, item, origNext);
      item.scrollIntoView({ block: "nearest", inline: "nearest" });
      handle?.focus({ preventScroll: true });
      accountKb.isMovingNode = false;
      animateAccountFlip(listEl, beforeRects, item, accountDrag.generation);
    }

    item.classList.remove("is-kb-grabbed");
    handle?.setAttribute("aria-pressed", "false");
    setKeyboardHintText(item, ACCOUNT_REORDER_HINT_IDLE);

    accountKb.active = false;
    accountKb.item = null;
    accountKb.handle = null;
    accountKb.originalNextSibling = null;
    accountKb.originalUids = [];

    const finalUids = getOrderedAccountUids(listEl);
    const changed =
      !cancel &&
      origUids.length === finalUids.length &&
      origUids.some((uid, idx) => uid !== finalUids[idx]);

    if (announce) {
      const name = getAccountDisplayName(item);
      if (cancel) {
        announceAccountReorder(`已取消调整 ${name} 的顺序`);
      } else if (changed) {
        const pos = finalUids.indexOf(item.dataset.uid || "") + 1;
        announceAccountReorder(`已放置 ${name} 到第 ${pos} 位，共 ${finalUids.length} 项`);
      } else {
        announceAccountReorder(`已放置 ${name}，顺序未变更`);
      }
    }

    if (changed) {
      persistAccountOrder(listEl, finalUids);
    }

    if (accountDrag.pendingSidebarSync) {
      accountDrag.pendingSidebarSync = false;
      renderAccountSidebar();
    }
  }

  listEl.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return;
    const handle = ev.target.closest(".account-drag-handle");
    if (!handle || handle.hidden) return;
    const item = handle.closest(".account-nav-row");
    if (!item || item.parentElement !== listEl) return;

    if (ev.key === " " || ev.key === "Enter") {
      if (getAccountRows(listEl).length < 2) return;
      ev.preventDefault();
      if (!state.demoMode && state.adminAuth.enabled && !state.adminAuth.authenticated) {
        ensureAdminAuthorized("调整账号排序");
        return;
      }
      if (accountKb.active && accountKb.item === item) {
        finishKeyboardGrab({ cancel: false, announce: true });
      } else {
        if (accountKb.active) {
          finishKeyboardGrab({ cancel: false, announce: false });
        }
        accountKb.active = true;
        accountKb.item = item;
        accountKb.handle = handle;
        accountKb.originalNextSibling = item.nextElementSibling;
        accountKb.originalUids = getOrderedAccountUids(listEl);

        handle.setAttribute("aria-pressed", "true");
        item.classList.add("is-kb-grabbed");
        setKeyboardHintText(item, ACCOUNT_REORDER_HINT_GRABBED);
        item.scrollIntoView({ block: "nearest", inline: "nearest" });

        const uids = accountKb.originalUids;
        const pos = uids.indexOf(item.dataset.uid || "") + 1;
        announceAccountReorder(
          `已抓起 ${getAccountDisplayName(item)}，当前第 ${pos} 位，共 ${uids.length} 项。按上下方向键移动，空格或回车放置，Esc 取消。`
        );
      }
      return;
    }

    if (!accountKb.active || accountKb.item !== item) return;

    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      finishKeyboardGrab({ cancel: true, announce: true });
      return;
    }

    if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
      ev.preventDefault();
      const rows = getAccountRows(listEl);
      const currentIdx = rows.indexOf(item);
      if (currentIdx === -1) return;

      let refNode = null;
      if (ev.key === "ArrowUp") {
        if (currentIdx === 0) return;
        refNode = rows[currentIdx - 1];
      } else {
        if (currentIdx >= rows.length - 1) return;
        refNode = rows[currentIdx + 2] || null;
      }

      for (const row of rows) {
        row.getAnimations?.().forEach((a) => a.finish());
      }
      const beforeRects = captureRowRects(listEl);

      accountKb.isMovingNode = true;
      const usedFallback = moveAccountNodeBefore(listEl, item, refNode);
      item.scrollIntoView({ block: "nearest", inline: "nearest" });
      handle.focus({ preventScroll: true });
      accountKb.isMovingNode = false;

      animateAccountFlip(listEl, beforeRects, item, accountDrag.generation);

      const nextUids = getOrderedAccountUids(listEl);
      const pos = nextUids.indexOf(item.dataset.uid || "") + 1;
      announceAccountReorder(
        `${getAccountDisplayName(item)} 已移至第 ${pos} 位，共 ${nextUids.length} 项`,
        usedFallback ? 480 : 40
      );
    }
  });

  listEl.addEventListener("focusout", (ev) => {
    if (!accountKb.active || accountKb.isMovingNode) return;
    if (ev.target === accountKb.handle) {
      finishKeyboardGrab({ cancel: false, announce: true });
    }
  });

  listEl.addEventListener("focusin", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    if (!target.matches(":focus-visible")) return;
    const row = target.closest(".account-nav-row");
    if (!row) return;
    if (typeof row.getAnimations === "function" && row.getAnimations().length > 0) return;
    row.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
}

let sidebarDrawerCtrl = null;

function setupResponsiveSidebar() {
  const sidebar = document.getElementById("accountSidebar");
  const stage = document.getElementById("studioStage");
  const openBtn = document.getElementById("btnOpenSidebar");
  const closeBtn = document.getElementById("btnCloseSidebar");
  const scrim = document.getElementById("sidebarScrim");
  if (!sidebar || !stage || !openBtn) return null;

  const mq = window.matchMedia("(max-width: 959.98px)");
  const supportsPopover = typeof sidebar.showPopover === "function";

  const closeDrawer = ({ restoreFocus = true } = {}) => {
    if (!state.isSidebarOpen) return;
    state.isSidebarOpen = false;
    if (supportsPopover && sidebar.matches(":popover-open")) {
      try {
        sidebar.hidePopover();
      } catch {
        // Ignore if already hidden
      }
    }
    sidebar.classList.remove("is-drawer-open");
    scrim?.classList.remove("is-open");
    sidebar.removeAttribute("aria-modal");
    stage.inert = false;
    openBtn.setAttribute("aria-expanded", "false");
    if (restoreFocus && state.isSidebarMobile) {
      openBtn.focus();
    }
  };

  const openDrawer = () => {
    if (!state.isSidebarMobile || state.isSidebarOpen) return;
    state.isSidebarOpen = true;
    stage.inert = true;
    sidebar.setAttribute("aria-modal", "true");
    openBtn.setAttribute("aria-expanded", "true");
    if (supportsPopover) {
      try {
        sidebar.showPopover();
      } catch {
        sidebar.classList.add("is-drawer-open");
        scrim?.classList.add("is-open");
      }
    } else {
      sidebar.classList.add("is-drawer-open");
      scrim?.classList.add("is-open");
    }
    closeBtn?.focus({ preventScroll: true });
  };

  const syncBreakpoint = () => {
    const mobile = mq.matches;
    state.isSidebarMobile = mobile;
    if (mobile) {
      sidebar.setAttribute("role", "dialog");
      if (supportsPopover) {
        sidebar.setAttribute("popover", "manual");
      }
    } else {
      closeDrawer({ restoreFocus: false });
      sidebar.removeAttribute("popover");
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
      stage.inert = false;
      openBtn.setAttribute("aria-expanded", "false");
    }
  };

  syncBreakpoint();
  if (typeof mq.addEventListener === "function") {
    mq.addEventListener("change", syncBreakpoint);
  } else if (typeof mq.addListener === "function") {
    mq.addListener(syncBreakpoint);
  }

  openBtn.addEventListener("click", openDrawer);
  closeBtn?.addEventListener("click", () => closeDrawer({ restoreFocus: true }));
  scrim?.addEventListener("click", () => closeDrawer({ restoreFocus: true }));

  // Dismiss popover drawer on backdrop click (popover light-dismiss is off for popover="manual")
  document.addEventListener("pointerdown", (ev) => {
    if (!state.isSidebarMobile || !state.isSidebarOpen) return;
    if (sidebar.contains(ev.target) || openBtn.contains(ev.target)) return;
    closeDrawer({ restoreFocus: true });
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && state.isSidebarMobile && state.isSidebarOpen) {
      ev.preventDefault();
      closeDrawer({ restoreFocus: true });
    }
  });

  return { openDrawer, closeDrawer };
}

async function switchAccount(uid) {
  const targetUid = String(uid || "").trim();
  if (!targetUid) return;

  if (state.isSidebarMobile && state.isSidebarOpen) {
    sidebarDrawerCtrl?.closeDrawer({ restoreFocus: false });
  }

  if (state.demoMode) {
    if (state.demoDataset?.activeUid === targetUid) return;
    switchDemoAccount(state, targetUid);
    clearRankCaches({ resetLimits: true });
    clearChartsInGroup("overview");
    const demoData = getDemoDashboard(state, state.hours);
    renderDashboard(demoData, { animateCharts: true, isLiveTick: false });
    return;
  }

  if (targetUid === state.activeUid) return;

  state.activeUid = targetUid;
  state.backendUid = targetUid;
  clearRankCaches({ resetLimits: true });
  clearChartsInGroup("overview");
  renderAccountSidebar();

  const reqId = ++state.dashboardReqSeq;
  try {
    if (!state.adminAuth.enabled || state.adminAuth.authenticated) {
      await apiFetch(`/api/accounts/${encodeURIComponent(targetUid)}/activate`, {
        method: "POST",
      });
    }
    if (reqId !== state.dashboardReqSeq) return;
    await loadDashboard({ animateCharts: true, uid: targetUid, reqId });
  } catch (err) {
    if (err?.status === 401) {
      await loadDashboard({ animateCharts: true, uid: targetUid, reqId });
      return;
    }
    showToast(`切换账号失败: ${err.message}`, "err");
  }
}

/* ---------- Image Cover Fallback ---------- */

function resolveCoverSrc(pic, fallbackLabel) {
  const valid = safeUrl(pic);
  return valid || buildFallbackCoverSvg(fallbackLabel);
}

function attachCoverFallback(imgEl, fallbackLabel) {
  if (!imgEl || imgEl.dataset.fallbackBound === "1") return;
  imgEl.dataset.fallbackBound = "1";
  imgEl.addEventListener("error", () => {
    const fallbackUri = buildFallbackCoverSvg(fallbackLabel);
    if (imgEl.src !== fallbackUri) {
      imgEl.src = fallbackUri;
    }
  });
}

function attachAvatarFallback(imgEl) {
  if (!imgEl || imgEl.dataset.fallbackBound === "1") return;
  imgEl.dataset.fallbackBound = "1";
  imgEl.addEventListener("error", () => {
    const fallbackUri = buildDemoAvatarSvg();
    if (imgEl.src !== fallbackUri) {
      imgEl.src = fallbackUri;
    }
  });
}

/* ---------- Charts ---------- */

function renderOverviewCharts(data, animate = true) {
  const snaps = data?.snapshots || [];

  const followerPoints = [];
  const onlinePoints = [];

  for (const item of snaps) {
    if (item.follower_count !== null && item.follower_count !== undefined) {
      const fVal = Number(item.follower_count);
      if (Number.isFinite(fVal)) {
        followerPoints.push({
          ts: item.collected_at,
          time: item.collected_at,
          value: fVal,
        });
      }
    }
    if (item.online_viewers_total !== null && item.online_viewers_total !== undefined) {
      const oVal = Number(item.online_viewers_total);
      if (Number.isFinite(oVal)) {
        onlinePoints.push({
          ts: item.collected_at,
          time: item.collected_at,
          value: oVal,
        });
      }
    }
  }

  const chartFollowersEl = document.getElementById("chartFollowers");
  const chartOnlineEl = document.getElementById("chartOnline");

  if (chartFollowersEl) {
    renderTelemetryChart(chartFollowersEl, followerPoints, {
      channelColor: "var(--sig-pink)",
      fillId: "followers",
      label: "粉丝总量",
      formatValue: (v) => fmtNumber(v),
      animate,
      syncGroup: "overview",
      emptyText: "暂无粉丝量采样波形",
    });
  }

  if (chartOnlineEl) {
    renderTelemetryChart(chartOnlineEl, onlinePoints, {
      channelColor: "var(--sig-cyan)",
      fillId: "online",
      label: "全视频在看",
      formatValue: (v) => `${fmtNumber(v)} 人`,
      animate,
      syncGroup: "overview",
      emptyText: "暂无实时在看采样波形",
    });
  }
}

/* ---------- Split Dual Video Leaderboards (Online & Total Plays) ---------- */

const LEADERBOARD_SPECS = [
  {
    key: "online",
    gridId: "onlineVideosGrid",
    countId: "onlineBoardCount",
    expandWrapId: "onlineBoardExpandWrap",
    expandBtnId: "btnExpandOnline",
    limitKey: "onlineLimit",
    cacheKey: "onlineRowCache",
    getPrimary: (v) => Math.max(0, Number(v.last_online_count || 0)),
    getSecondary: (v) => Math.max(0, Number(v.play_count || 0)),
    formatPrimary: (val) => fmtNumber(val),
    primaryUnit: "人正在看",
    formatSubMeta: (v) => `播放 ${fmtCompact(v.play_count)}`,
  },
  {
    key: "plays",
    gridId: "playsVideosGrid",
    countId: "playsBoardCount",
    expandWrapId: "playsBoardExpandWrap",
    expandBtnId: "btnExpandPlays",
    limitKey: "playsLimit",
    cacheKey: "playsRowCache",
    getPrimary: (v) => Math.max(0, Number(v.play_count || 0)),
    getSecondary: (v) => Math.max(0, Number(v.last_online_count || 0)),
    formatPrimary: (val) => fmtCompact(val),
    primaryUnit: "次播放",
    formatSubMeta: (v) => `${fmtNumber(v.last_online_count || 0)} 人正在看`,
  },
];

function formatEngagementRate(count, playCount, hasStats) {
  const plays = Number(playCount || 0);
  const val = Number(count || 0);
  if (!Number.isFinite(plays) || plays <= 0 || (!hasStats && val === 0)) {
    return { text: "--", empty: true };
  }
  const pct = (Math.max(0, val) / plays) * 100;
  return { text: `${pct.toFixed(2)}%`, empty: false };
}

function createOrUpdateRankRow(video, rank, maxMetric, spec) {
  const bvid = String(video.bvid || "");
  const cache = state[spec.cacheKey];
  let row = cache.get(bvid);
  const metricVal = spec.getPrimary(video);
  const ratio = maxMetric > 0 && metricVal > 0 ? Math.min(1, Math.max(0.03, metricVal / maxMetric)) : 0;
  const rankCode = String(rank).padStart(2, "0");
  const coverSrc = resolveCoverSrc(video.pic, bvid);

  if (!row) {
    row = document.createElement("article");
    row.className = "rank-row";
    row.setAttribute("data-stagger", "");
    row.dataset.bvid = bvid;
    row.tabIndex = 0;
    row.setAttribute("role", "button");

    const bvidEsc = escapeHtml(bvid);
    const titleEsc = escapeHtml(video.title || bvid);

    row.innerHTML = `
      <span class="rank-num mono"></span>
      <div class="rank-thumb">
        <img src="${escapeHtml(coverSrc)}" referrerpolicy="no-referrer" loading="lazy" alt="${titleEsc}" />
      </div>
      <div class="rank-info">
        <div class="rank-info-top">
          <div class="rank-info-main">
            <div class="rank-title"></div>
            <div class="rank-meta mono">
              <a class="bvid-link bvid-pill mono" href="https://www.bilibili.com/video/${encodeURIComponent(bvid)}" target="_blank" rel="noopener noreferrer">${bvidEsc}</a>
              <span class="rank-sub-metric"></span>
              <span class="rank-dur muted"></span>
            </div>
          </div>
          <div class="rank-rates mono" aria-label="互动率">
            <span class="rank-rate-item rank-rate-coin">
              <span class="rank-rate-label">投币率</span>
              <span class="rank-rate-val"></span>
            </span>
            <span class="rank-rate-item rank-rate-like">
              <span class="rank-rate-label">点赞率</span>
              <span class="rank-rate-val"></span>
            </span>
          </div>
        </div>
        <div class="rank-heat-track" aria-hidden="true">
          <div class="rank-heat-fill"></div>
        </div>
      </div>
      <div class="rank-online mono">
        <span class="rank-online-val"></span>
        <span class="rank-online-unit muted">${escapeHtml(spec.primaryUnit)}</span>
      </div>
    `;

    attachCoverFallback(row.querySelector("img"), bvid);
    cache.set(bvid, row);
  }

  row.dataset.title = video.title || bvid;
  row.setAttribute("aria-label", `查看 ${video.title || bvid} 实时在线历史波形`);
  row.classList.toggle("rank-1", rank === 1);
  row.classList.toggle("rank-2", rank === 2);
  row.classList.toggle("rank-3", rank === 3);
  row.classList.toggle("is-top", rank <= 3);

  const numEl = row.querySelector(".rank-num");
  if (numEl) numEl.textContent = rankCode;

  const imgEl = row.querySelector(".rank-thumb img");
  if (imgEl && imgEl.getAttribute("src") !== coverSrc && !imgEl.src.startsWith("data:image/svg+xml")) {
    imgEl.src = coverSrc;
  }

  const titleEl = row.querySelector(".rank-title");
  if (titleEl) {
    titleEl.textContent = video.title || bvid;
    titleEl.title = video.title || bvid;
  }

  const subMetricEl = row.querySelector(".rank-sub-metric");
  if (subMetricEl) subMetricEl.textContent = spec.formatSubMeta(video);

  const durEl = row.querySelector(".rank-dur");
  if (durEl) durEl.textContent = `${Number(video.cid_count || 1)}集 · ${fmtDuration(video.duration)}`;

  const playCountVal = Math.max(0, Number(video.play_count || 0));
  const coinCountVal = Math.max(0, Number(video.coin_count || 0));
  const likeCountVal = Math.max(0, Number(video.like_count || 0));
  const hasStats =
    Boolean(video.stats_synced_at) || coinCountVal > 0 || likeCountVal > 0;
  const coinRate = formatEngagementRate(coinCountVal, playCountVal, hasStats);
  const likeRate = formatEngagementRate(likeCountVal, playCountVal, hasStats);

  const coinRateEl = row.querySelector(".rank-rate-coin .rank-rate-val");
  if (coinRateEl) {
    coinRateEl.textContent = coinRate.text;
    coinRateEl.classList.toggle("is-empty", coinRate.empty);
  }
  const coinItemEl = row.querySelector(".rank-rate-coin");
  if (coinItemEl) {
    coinItemEl.title = coinRate.empty
      ? "投币率：暂无统计数据"
      : `投币率 ${coinRate.text}（投币 ${fmtNumber(coinCountVal)} / 播放 ${fmtNumber(playCountVal)}）`;
  }

  const likeRateEl = row.querySelector(".rank-rate-like .rank-rate-val");
  if (likeRateEl) {
    likeRateEl.textContent = likeRate.text;
    likeRateEl.classList.toggle("is-empty", likeRate.empty);
  }
  const likeItemEl = row.querySelector(".rank-rate-like");
  if (likeItemEl) {
    likeItemEl.title = likeRate.empty
      ? "点赞率：暂无统计数据"
      : `点赞率 ${likeRate.text}（点赞 ${fmtNumber(likeCountVal)} / 播放 ${fmtNumber(playCountVal)}）`;
  }

  const heatFill = row.querySelector(".rank-heat-fill");
  if (heatFill) {
    heatFill.style.transform = `scaleX(${ratio.toFixed(4)})`;
  }

  const primaryValEl = row.querySelector(".rank-online-val");
  if (primaryValEl) primaryValEl.textContent = spec.formatPrimary(metricVal);

  return row;
}

function renderLeaderboardColumn(videos, spec) {
  const grid = document.getElementById(spec.gridId);
  if (!grid) return;

  const sorted = [...(videos || [])].sort((a, b) => {
    const primaryDiff = spec.getPrimary(b) - spec.getPrimary(a);
    return primaryDiff !== 0 ? primaryDiff : spec.getSecondary(b) - spec.getSecondary(a);
  });

  const limit = Math.max(LEADERBOARD_PAGE_SIZE, Number(state[spec.limitKey] || LEADERBOARD_PAGE_SIZE));
  const visible = sorted.slice(0, limit);

  const countChip = document.getElementById(spec.countId);
  if (countChip) {
    countChip.textContent =
      sorted.length > visible.length
        ? `显示 ${visible.length} / ${sorted.length}`
        : `${sorted.length} 个视频`;
  }

  const expandWrap = document.getElementById(spec.expandWrapId);
  const expandBtn = document.getElementById(spec.expandBtnId);
  const remaining = sorted.length - visible.length;
  if (expandWrap && expandBtn) {
    if (remaining > 0) {
      const nextStep = Math.min(LEADERBOARD_PAGE_SIZE, remaining);
      expandBtn.textContent = `展开更多 (+${nextStep} 条 · 剩余 ${remaining} 条)`;
      expandWrap.classList.remove("hidden");
    } else {
      expandWrap.classList.add("hidden");
    }
  }

  if (visible.length === 0) {
    state[spec.cacheKey].clear();
    grid.innerHTML = `<div class="chart-empty mono">暂无视频数据，请配置账号编号采集或开启示例数据</div>`;
    return;
  }

  const maxMetric = Math.max(1, ...visible.map((v) => spec.getPrimary(v)));
  const orderedNodes = visible.map((v, idx) => createOrUpdateRankRow(v, idx + 1, maxMetric, spec));
  reorderChildren(grid, orderedNodes);
}

function renderLeaderboards(videos) {
  for (const spec of LEADERBOARD_SPECS) {
    renderLeaderboardColumn(videos, spec);
  }
}

/* ---------- Multi-Account Horizontal Progress Comparison Board ---------- */

const COMPARE_PANE_SPECS = [
  {
    key: "followers",
    listId: "compareFollowersList",
    axisId: "compareFollowersAxis",
    peakChipId: "compareFollowersPeakChip",
    cacheKey: "compareFollowersCache",
    getVal: (acc) =>
      acc.latest_follower_count !== null && acc.latest_follower_count !== undefined
        ? Math.max(0, Number(acc.latest_follower_count))
        : null,
    getDelta: (acc) =>
      acc.follower_delta !== null && acc.follower_delta !== undefined
        ? Number(acc.follower_delta)
        : null,
    formatVal: (v) => fmtNumber(v),
    formatCompactVal: (v) => fmtCompact(v),
    unitLabel: "粉丝",
    peakPrefix: "最高",
  },
  {
    key: "online",
    listId: "compareOnlineList",
    axisId: "compareOnlineAxis",
    peakChipId: "compareOnlinePeakChip",
    cacheKey: "compareOnlineCache",
    getVal: (acc) =>
      acc.latest_online_viewers_total !== null && acc.latest_online_viewers_total !== undefined
        ? Math.max(0, Number(acc.latest_online_viewers_total))
        : null,
    getDelta: (acc) =>
      acc.online_delta !== null && acc.online_delta !== undefined
        ? Number(acc.online_delta)
        : null,
    formatVal: (v) => `${fmtNumber(v)} 人`,
    formatCompactVal: (v) => `${fmtCompact(v)} 人`,
    unitLabel: "实时在线",
    peakPrefix: "峰值",
  },
];

function computeNiceAxisMax(maxVal) {
  const num = Number(maxVal || 0);
  if (!Number.isFinite(num) || num <= 0) return 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(num)));
  const norm = num / magnitude;
  const steps = [1.2, 1.6, 2, 2.4, 2.8, 3.2, 4, 4.8, 6, 8, 10];
  const step = steps.find((s) => s >= norm - 1e-9) || 10;
  return Math.max(4, Math.round(step * magnitude));
}

function formatAxisTick(val) {
  if (!Number.isFinite(val) || val <= 0) return "0";
  return fmtCompact(val);
}

function formatCompareDelta(delta) {
  if (delta === null || delta === undefined || Number.isNaN(Number(delta))) {
    return { text: "", cls: "is-flat" };
  }
  const n = Math.round(Number(delta));
  if (n > 0) return { text: `▲ +${fmtNumber(n)}`, cls: "is-up" };
  if (n < 0) return { text: `▼ ${fmtNumber(n)}`, cls: "is-down" };
  return { text: "● 0", cls: "is-flat" };
}

function createCompareBarItem(uid) {
  const li = document.createElement("li");
  li.className = "compare-bar-item";
  li.setAttribute("data-stagger", "");
  li.dataset.uid = uid;
  li.innerHTML = `
    <button type="button" class="compare-bar-row" data-compare-uid="${escapeHtml(uid)}">
      <div class="compare-row-identity">
        <span class="compare-rank mono">01</span>
        <div class="compare-avatar">
          <img referrerpolicy="no-referrer" alt="" />
        </div>
      </div>
      <div class="compare-row-main">
        <div class="compare-row-header">
          <div class="compare-row-name-wrap">
            <span class="signal-dot"></span>
            <span class="compare-row-name"></span>
            <span class="compare-row-tag mono hidden"></span>
          </div>
          <div class="compare-row-stats mono">
            <span class="compare-row-delta hidden"></span>
            <span class="compare-row-value"></span>
          </div>
        </div>
        <div class="compare-progress-track">
          <progress class="compare-progress" value="0" max="4"></progress>
        </div>
      </div>
    </button>
  `;
  const imgEl = li.querySelector(".compare-avatar img");
  if (imgEl) {
    attachAvatarFallback(imgEl);
  }
  return li;
}

function updateCompareBarItem(li, acc, { rank, axisMax, isActive, isDemo, crawlingUid, spec }) {
  const uid = String(acc.uid || "");
  const displayName = acc.account_name || `账号 ${uid}`;
  const avatarSrc = safeUrl(acc.account_face) || buildDemoAvatarSvg();
  const dotClass = getAccountDotClass(acc, crawlingUid);
  const val = spec.getVal(acc);
  const delta = spec.getDelta(acc);
  const deltaInfo = formatCompareDelta(delta);

  li.dataset.uid = uid;
  const rowBtn = li.querySelector(".compare-bar-row");
  if (rowBtn) {
    rowBtn.dataset.compareUid = uid;
    rowBtn.classList.toggle("is-active", isActive);
    rowBtn.classList.toggle("rank-1", rank === 1);
    rowBtn.classList.toggle("rank-2", rank === 2);
    rowBtn.classList.toggle("rank-3", rank === 3);
    const valDesc = val !== null ? spec.formatVal(val) : "待采集";
    rowBtn.setAttribute("aria-label", `${displayName}，${spec.unitLabel} ${valDesc}，点击查看单账号详情`);
  }

  const rankEl = li.querySelector(".compare-rank");
  if (rankEl) rankEl.textContent = String(rank).padStart(2, "0");

  const imgEl = li.querySelector(".compare-avatar img");
  if (imgEl && imgEl.dataset.rawSrc !== avatarSrc) {
    imgEl.dataset.rawSrc = avatarSrc;
    imgEl.src = avatarSrc;
  }

  const nameEl = li.querySelector(".compare-row-name");
  if (nameEl && nameEl.textContent !== displayName) {
    nameEl.textContent = displayName;
    nameEl.title = displayName;
  }

  const dotEl = li.querySelector(".signal-dot");
  if (dotEl) {
    dotEl.className = `signal-dot ${dotClass}`;
    dotEl.title = acc.enabled ? "监控中" : "已暂停";
  }

  const tagEl = li.querySelector(".compare-row-tag");
  if (tagEl) {
    if (isDemo) {
      tagEl.textContent = "示例";
      tagEl.className = "compare-row-tag mono is-demo";
    } else if (!acc.enabled) {
      tagEl.textContent = "已暂停";
      tagEl.className = "compare-row-tag mono";
    } else {
      tagEl.textContent = "";
      tagEl.className = "compare-row-tag mono hidden";
    }
  }

  const deltaEl = li.querySelector(".compare-row-delta");
  if (deltaEl) {
    if (deltaInfo.text) {
      deltaEl.textContent = deltaInfo.text;
      deltaEl.className = `compare-row-delta ${deltaInfo.cls}`;
    } else {
      deltaEl.textContent = "";
      deltaEl.className = "compare-row-delta hidden";
    }
  }

  const valEl = li.querySelector(".compare-row-value");
  if (valEl) {
    if (val !== null) {
      valEl.textContent = spec.formatVal(val);
      valEl.classList.remove("is-pending");
    } else {
      valEl.textContent = "待采集";
      valEl.classList.add("is-pending");
    }
  }

  const progressEl = li.querySelector("progress.compare-progress");
  if (progressEl) {
    progressEl.max = axisMax;
    progressEl.value = val !== null ? Math.min(axisMax, val) : 0;
    progressEl.setAttribute(
      "aria-label",
      `${displayName} ${spec.unitLabel} ${val !== null ? spec.formatVal(val) : "待采集"}`
    );
  }
}

function renderComparePane(accounts, { activeUid, isDemo, crawlingUid }, spec) {
  const listEl = document.getElementById(spec.listId);
  const axisEl = document.getElementById(spec.axisId);
  const peakChip = document.getElementById(spec.peakChipId);
  if (!listEl) return;

  const sorted =
    state.compareSortMode === "order"
      ? [...accounts]
      : [...accounts].sort((a, b) => {
          const av = spec.getVal(a);
          const bv = spec.getVal(b);
          if (av === null && bv === null) return 0;
          if (av === null) return 1;
          if (bv === null) return -1;
          return bv - av;
        });

  const numericVals = sorted.map((a) => spec.getVal(a)).filter((v) => v !== null);
  const highestVal = numericVals.length > 0 ? Math.max(...numericVals) : null;
  const axisMax = computeNiceAxisMax(highestVal ?? 0);

  if (peakChip) {
    peakChip.textContent =
      highestVal !== null
        ? `${spec.peakPrefix} ${spec.formatCompactVal(highestVal)}`
        : `${spec.peakPrefix} --`;
  }

  if (axisEl) {
    const tickSpans = axisEl.querySelectorAll(".compare-axis-ticks > span");
    const ratios = [0, 0.25, 0.5, 0.75, 1];
    tickSpans.forEach((span, idx) => {
      const r = ratios[idx] ?? 1;
      span.textContent = formatAxisTick(axisMax * r);
    });
  }

  const cache = state[spec.cacheKey];
  if (sorted.length === 0) {
    cache.clear();
    listEl.innerHTML = `<li class="chart-empty mono">暂无监控账号数据，请点击左侧「添加监控账号」或加载示例数据</li>`;
    return;
  }

  const orderedNodes = [];
  for (let i = 0; i < sorted.length; i += 1) {
    const acc = sorted[i];
    const uid = String(acc.uid || "");
    if (!uid) continue;
    let item = cache.get(uid);
    if (!item) {
      item = createCompareBarItem(uid);
      cache.set(uid, item);
    }
    updateCompareBarItem(item, acc, {
      rank: i + 1,
      axisMax,
      isActive: uid === activeUid,
      isDemo,
      crawlingUid,
      spec,
    });
    orderedNodes.push(item);
  }

  reorderChildren(listEl, orderedNodes);
}

function renderCompareBoard({ stagger = false } = {}) {
  const isDemo = state.demoMode;
  const accounts = isDemo ? getDemoAccounts(state) : state.accounts || [];
  const activeUid = isDemo
    ? state.demoDataset?.activeUid || accounts[0]?.uid || ""
    : state.activeUid || state.backendUid || "";
  const crawlingUid = state.dashboard?.scheduler?.crawling_uid || null;

  const tabCountEl = document.getElementById("compareTabCount");
  if (tabCountEl) {
    tabCountEl.textContent = String(accounts.length);
  }

  const totalAccEl = document.getElementById("compareTotalAccounts");
  if (totalAccEl) {
    totalAccEl.textContent = `${accounts.length} 个`;
  }

  const followerVals = accounts
    .map((a) => a.latest_follower_count)
    .filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
  const onlineVals = accounts
    .map((a) => a.latest_online_viewers_total)
    .filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));

  const totalFollowersEl = document.getElementById("compareTotalFollowers");
  if (totalFollowersEl) {
    totalFollowersEl.textContent =
      followerVals.length > 0
        ? fmtNumber(followerVals.reduce((sum, v) => sum + Number(v), 0))
        : "--";
  }

  const totalOnlineEl = document.getElementById("compareTotalOnline");
  if (totalOnlineEl) {
    totalOnlineEl.textContent =
      onlineVals.length > 0
        ? `${fmtNumber(onlineVals.reduce((sum, v) => sum + Number(v), 0))} 人`
        : "--";
  }

  for (const spec of COMPARE_PANE_SPECS) {
    renderComparePane(accounts, { activeUid, isDemo, crawlingUid }, spec);
  }

  if (stagger) {
    const comparePanel = document.getElementById("tab-compare");
    if (comparePanel) staggerEnter(comparePanel, ".compare-bar-item");
  }
}

let mainTabSwitchToken = 0;

function switchMainTab(targetId, { animate = true, focusTab = false } = {}) {
  const nextId = targetId === "tab-compare" ? "tab-compare" : "tab-overview";
  const tabList = document.getElementById("viewTabList");
  const tabs = tabList ? Array.from(tabList.querySelectorAll('[role="tab"]')) : [];
  const overviewPanel = document.getElementById("tab-overview");
  const comparePanel = document.getElementById("tab-compare");
  const targetTab = tabs.find((t) => t.dataset.tabTarget === nextId) || null;
  const switchToken = ++mainTabSwitchToken;
  const willStageCompareStagger =
    animate &&
    nextId === "tab-compare" &&
    typeof document.startViewTransition === "function" &&
    !prefersReducedMotion();

  if (state.isSidebarMobile && state.isSidebarOpen && !focusTab) {
    sidebarDrawerCtrl?.closeDrawer({ restoreFocus: false });
  }

  const applyTabState = () => {
    state.activeTab = nextId;
    for (const tab of tabs) {
      const selected = tab.dataset.tabTarget === nextId;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }

    if (overviewPanel) {
      const showOverview = nextId === "tab-overview";
      overviewPanel.classList.toggle("active", showOverview);
      if (showOverview) {
        overviewPanel.removeAttribute("hidden");
      } else {
        overviewPanel.setAttribute("hidden", "until-found");
      }
    }

    if (comparePanel) {
      const showCompare = nextId === "tab-compare";
      comparePanel.classList.toggle("active", showCompare);
      if (showCompare) {
        comparePanel.removeAttribute("hidden");
        // Keep compare rows hidden during the tab View Transition so they do not
        // flash at opacity: 1 right before staggerEnter resets them to opacity: 0.
        comparePanel.classList.toggle("is-stagger-pending", willStageCompareStagger);
        renderCompareBoard({ stagger: false });
      } else {
        comparePanel.classList.remove("is-stagger-pending");
        comparePanel.setAttribute("hidden", "until-found");
      }
    }
  };

  if (animate) {
    runViewTransition(applyTabState, { originEl: targetTab, type: "tab" }).then(() => {
      if (switchToken !== mainTabSwitchToken) return;
      if (nextId === "tab-compare") {
        renderCompareBoard({ stagger: true });
        comparePanel?.classList.remove("is-stagger-pending");
      } else if (state.dashboard) {
        requestAnimationFrame(() => renderOverviewCharts(state.dashboard, false));
      }
    });
  } else {
    applyTabState();
    comparePanel?.classList.remove("is-stagger-pending");
    if (nextId === "tab-compare") {
      renderCompareBoard({ stagger: false });
    } else if (state.dashboard) {
      requestAnimationFrame(() => renderOverviewCharts(state.dashboard, false));
    }
  }

  if (focusTab && targetTab) {
    targetTab.focus();
  }
}

/* ---------- Main Dashboard Renderer ---------- */

function syncAccountsFromDashboard(data) {
  if (!data || state.demoMode) return;
  if (Array.isArray(data.accounts) && data.accounts.length > 0) {
    state.accounts = data.accounts;
  } else if (data.settings?.uid) {
    state.accounts = [
      {
        uid: String(data.settings.uid),
        account_name: data.settings.account_name || "",
        account_face: data.settings.account_face || "",
        has_cookie: Boolean(data.settings.has_cookie),
        enabled: data.settings.enabled !== false,
        is_crawling: Boolean(data.scheduler?.is_crawling),
      },
    ];
  } else if (Array.isArray(data.accounts)) {
    state.accounts = data.accounts;
  }
}

function applyFollowerDailyCell(valueEl, dateEl, delta, dateStr, emptyHint) {
  if (dateEl) {
    const shortDate =
      typeof dateStr === "string" && dateStr.length >= 10 ? dateStr.slice(5) : dateStr || "--";
    dateEl.textContent = shortDate;
    dateEl.title = dateStr ? `北京时间 ${dateStr}` : "";
  }
  if (!valueEl) return;

  valueEl.classList.remove("is-up", "is-down", "is-flat", "is-empty");
  if (delta === null || delta === undefined || Number.isNaN(Number(delta))) {
    valueEl.textContent = "--";
    valueEl.classList.add("is-empty");
    valueEl.title = emptyHint;
    return;
  }

  const num = Math.round(Number(delta));
  if (num > 0) {
    valueEl.textContent = `▲ +${fmtNumber(num)}`;
    valueEl.classList.add("is-up");
  } else if (num < 0) {
    valueEl.textContent = `▼ ${fmtNumber(num)}`;
    valueEl.classList.add("is-down");
  } else {
    valueEl.textContent = "0";
    valueEl.classList.add("is-flat");
  }
  valueEl.title = dateStr
    ? `北京时间 ${dateStr} 粉丝变化: ${valueEl.textContent}`
    : `粉丝变化: ${valueEl.textContent}`;
}

function renderFollowerDailyStats(daily) {
  const yValEl = document.getElementById("kpiFollowersYesterday");
  const yDateEl = document.getElementById("kpiFollowersYesterdayDate");
  const tValEl = document.getElementById("kpiFollowersToday");
  const tDateEl = document.getElementById("kpiFollowersTodayDate");

  applyFollowerDailyCell(
    yValEl,
    yDateEl,
    daily?.yesterday,
    daily?.yesterday_date,
    "昨日暂无足够采样数据"
  );
  applyFollowerDailyCell(
    tValEl,
    tDateEl,
    daily?.today,
    daily?.today_date,
    "今日暂无足够采样数据"
  );
}

function renderDashboard(data, { animateCharts = true, isLiveTick = false } = {}) {
  if (!data) return;
  if (data.admin_auth) {
    syncAdminAuthState(data.admin_auth);
  }
  state.dashboard = data;
  state.videos = data.top_videos || [];
  syncAccountsFromDashboard(data);

  syncBannersAndDemoControls();
  renderAccountSidebar();

  const s = data.settings || {};
  const accountPill = document.getElementById("accountPill");
  if (accountPill) {
    if (s.uid) {
      const faceUrl = safeUrl(s.account_face) || (state.demoMode ? buildDemoAvatarSvg() : "");
      const avatarHtml = faceUrl
        ? `<img src="${escapeHtml(faceUrl)}" referrerpolicy="no-referrer" alt="" />`
        : "";
      const name = s.account_name || `账号 ${s.uid}`;
      const simChip = state.demoMode ? `<span class="status-chip mono">示例</span>` : "";
      accountPill.innerHTML = `
        ${avatarHtml}
        <strong>${escapeHtml(name)}</strong>
        <a class="mono" href="https://space.bilibili.com/${encodeURIComponent(s.uid)}" target="_blank" rel="noopener noreferrer">编号:${escapeHtml(s.uid)}</a>
        ${simChip}
      `;
      const avImg = accountPill.querySelector("img");
      if (avImg) {
        avImg.addEventListener(
          "error",
          () => {
            avImg.src = buildDemoAvatarSvg();
          },
          { once: true }
        );
      }
    } else {
      accountPill.innerHTML = `<span class="muted mono">尚未绑定监控账号</span>`;
    }
  }

  updateLiveClockAndLaser();

  // Unified Card KPIs
  const latest = data.latest;
  const kpiFollowers = document.getElementById("kpiFollowers");
  if (kpiFollowers) {
    if (latest && latest.follower_count !== null && latest.follower_count !== undefined) {
      animateNumber(kpiFollowers, Number(latest.follower_count), {
        duration: isLiveTick ? 520 : 680,
        flash: true,
      });
    } else {
      resetNumericReadout(kpiFollowers, "--");
    }
  }

  renderFollowerDailyStats(data.follower_daily);

  const kpiOnline = document.getElementById("kpiOnline");
  if (kpiOnline) {
    if (latest && latest.online_viewers_total !== null && latest.online_viewers_total !== undefined) {
      animateNumber(kpiOnline, Number(latest.online_viewers_total), {
        duration: isLiveTick ? 520 : 680,
        flash: true,
      });
    } else {
      resetNumericReadout(kpiOnline, "--");
    }
  }

  renderOverviewCharts(data, animateCharts);
  renderLeaderboards(state.videos);
  renderCompareBoard();
}

/* ---------- Native <dialog id="videoModal"> Centered Multi-Channel Modal ---------- */

const STANDARD_BUCKET_SECONDS = [
  300,
  600,
  900,
  1800,
  3600,
  7200,
  14400,
  21600,
  43200,
  86400,
];

function parseModalPointMs(rawTs, rawEpochSec) {
  if (rawEpochSec !== null && rawEpochSec !== undefined && Number.isFinite(Number(rawEpochSec))) {
    const sec = Number(rawEpochSec);
    if (sec > 0) return sec * 1000;
  }
  if (!rawTs) return Number.NaN;
  const str = String(rawTs).trim();
  if (!str) return Number.NaN;
  let ms = Date.parse(str);
  if (Number.isNaN(ms) && str.includes(" ")) {
    ms = Date.parse(str.replace(" ", "T"));
  }
  return ms;
}

function fmtSignedDelta(val) {
  if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) {
    return "--";
  }
  const num = Math.round(Number(val));
  if (num > 0) return `+${fmtNumber(num)}`;
  return fmtNumber(num);
}

function formatBucketDurationLabel(bucketSec) {
  const sec = Math.max(60, Math.round(Number(bucketSec || 300)));
  if (sec < 3600) {
    return `${Math.max(1, Math.round(sec / 60))}分钟`;
  }
  if (sec < 86400) {
    const hrs = sec / 3600;
    return Number.isInteger(hrs) ? `${hrs}小时` : `${hrs.toFixed(1)}小时`;
  }
  const days = sec / 86400;
  return Number.isInteger(days) ? `${days}天` : `${days.toFixed(1)}天`;
}

function formatBucketRangeHud(startMs, endMs, bucketLabel) {
  const d0 = new Date(startMs);
  const d1 = new Date(endMs);
  if (Number.isNaN(d0.getTime()) || Number.isNaN(d1.getTime())) {
    return bucketLabel ? `区间 (${bucketLabel})` : "--:--";
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  const md0 = `${pad2(d0.getMonth() + 1)}-${pad2(d0.getDate())}`;
  const md1 = `${pad2(d1.getMonth() + 1)}-${pad2(d1.getDate())}`;
  const hm0 = `${pad2(d0.getHours())}:${pad2(d0.getMinutes())}`;
  const hm1 = `${pad2(d1.getHours())}:${pad2(d1.getMinutes())}`;
  const rangeStr =
    md0 === md1 ? `${md0} ${hm0} → ${hm1}` : `${md0} ${hm0} → ${md1} ${hm1}`;
  return bucketLabel ? `${rangeStr} · ${bucketLabel}` : rangeStr;
}

function medianOfNumbers(values) {
  if (!Array.isArray(values) || values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

function chooseBaseBucketSec(spanSec, requestedHours) {
  const h = Number(requestedHours || 0);
  const effectiveSec = h > 0 ? Math.min(spanSec, h * 3600 * 1.05) : spanSec;
  if (effectiveSec <= 2.5 * 3600) return 300;
  if (effectiveSec <= 8 * 3600) return 900;
  if (effectiveSec <= 30 * 3600) return 1800;
  if (effectiveSec <= 8 * 86400) return 4 * 3600;
  if (effectiveSec <= 45 * 86400) return 86400;
  return Math.max(2, Math.ceil(effectiveSec / (40 * 86400))) * 86400;
}

function snapBucketSecAtLeast(targetSec) {
  for (const std of STANDARD_BUCKET_SECONDS) {
    if (std >= targetSec) return std;
  }
  return Math.max(2, Math.ceil(targetSec / 86400)) * 86400;
}

function interpolateCumulativeAt(pts, targetTsMs) {
  const n = pts.length;
  if (n === 0) return 0;
  if (n === 1 || targetTsMs <= pts[0].tsMs) return pts[0].value;
  if (targetTsMs >= pts[n - 1].tsMs) return pts[n - 1].value;

  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].tsMs <= targetTsMs) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const t0 = pts[lo].tsMs;
  const t1 = pts[hi].tsMs;
  if (t1 <= t0 || targetTsMs === t0) return pts[lo].value;
  if (targetTsMs === t1) return pts[hi].value;
  const ratio = (targetTsMs - t0) / (t1 - t0);
  return pts[lo].value + (pts[hi].value - pts[lo].value) * ratio;
}

function buildIntervalDeltaSeries(rawCumPoints, requestedHours = 24) {
  if (!Array.isArray(rawCumPoints) || rawCumPoints.length < 2) {
    return { points: [], bucketSec: 0, bucketLabel: "", totalDelta: null };
  }

  const sorted = [];
  for (const p of rawCumPoints) {
    const tsMs = Number(p.tsMs);
    const val = Number(p.value);
    if (!Number.isFinite(tsMs) || !Number.isFinite(val)) continue;
    sorted.push({ tsMs, value: val });
  }
  if (sorted.length < 2) {
    return { points: [], bucketSec: 0, bucketLabel: "", totalDelta: null };
  }
  sorted.sort((a, b) => a.tsMs - b.tsMs);

  // Deduplicate identical timestamps, keeping latest reading
  const pts = [];
  for (const item of sorted) {
    if (pts.length > 0 && pts[pts.length - 1].tsMs === item.tsMs) {
      pts[pts.length - 1] = item;
    } else {
      pts.push(item);
    }
  }
  if (pts.length < 2) {
    return { points: [], bucketSec: 0, bucketLabel: "", totalDelta: null };
  }

  const tStart = pts[0].tsMs;
  const tEnd = pts[pts.length - 1].tsMs;
  const spanMs = tEnd - tStart;
  const spanSec = spanMs / 1000;
  const totalDelta = Math.round(pts[pts.length - 1].value - pts[0].value);
  if (spanSec <= 0) {
    return { points: [], bucketSec: 0, bucketLabel: "", totalDelta };
  }

  let bucketSec = chooseBaseBucketSec(spanSec, requestedHours);

  // Respect natural sample spacing (ignoring sub-minute demo live micro-ticks)
  const sampleGapsSec = [];
  for (let i = 1; i < pts.length; i += 1) {
    const gapSec = (pts[i].tsMs - pts[i - 1].tsMs) / 1000;
    if (gapSec >= 30) {
      sampleGapsSec.push(gapSec);
    }
  }
  if (sampleGapsSec.length > 0) {
    const medGap = medianOfNumbers(sampleGapsSec);
    if (medGap > bucketSec * 1.15) {
      const snappedGap = snapBucketSecAtLeast(medGap * 0.9);
      bucketSec = Math.max(bucketSec, Math.min(snappedGap, Math.max(bucketSec, spanSec)));
    }
  }

  // Adapt upward when cumulative series was batch-synced on a coarser cadence (e.g. 30m/60m steps)
  const changeGapsSec = [];
  const absJumps = [];
  let zeroSteps = 0;
  let lastChangeTs = pts[0].tsMs;
  let lastChangeVal = pts[0].value;
  for (let i = 1; i < pts.length; i += 1) {
    const gapFromPrev = (pts[i].tsMs - pts[i - 1].tsMs) / 1000;
    if (gapFromPrev < 30) continue;
    const diff = pts[i].value - lastChangeVal;
    if (diff === 0) {
      zeroSteps += 1;
    } else {
      const cGap = (pts[i].tsMs - lastChangeTs) / 1000;
      if (cGap >= 30) {
        changeGapsSec.push(cGap);
        absJumps.push(Math.abs(diff));
      }
      lastChangeTs = pts[i].tsMs;
      lastChangeVal = pts[i].value;
    }
  }

  if (
    changeGapsSec.length >= 2 &&
    zeroSteps >= changeGapsSec.length &&
    medianOfNumbers(absJumps) >= 5
  ) {
    const medChangeGap = medianOfNumbers(changeGapsSec);
    if (medChangeGap > bucketSec * 1.6) {
      const candidate = snapBucketSecAtLeast(medChangeGap * 0.85);
      if (candidate <= spanSec / 1.8) {
        bucketSec = Math.max(bucketSec, candidate);
      }
    }
  }

  const bucketMs = bucketSec * 1000;
  const bucketLabel = formatBucketDurationLabel(bucketSec);

  if (spanMs <= bucketMs * 1.15) {
    const actualLabel = formatBucketDurationLabel(Math.max(60, Math.round(spanSec)));
    return {
      points: [
        {
          ts: new Date(tEnd).toISOString(),
          tsMs: tEnd,
          value: totalDelta,
          hudTime: formatBucketRangeHud(tStart, tEnd, actualLabel),
        },
      ],
      bucketSec: Math.max(60, Math.round(spanSec)),
      bucketLabel: actualLabel,
      totalDelta,
    };
  }

  const bucketCount = Math.max(2, Math.floor((spanMs + bucketMs * 0.15) / bucketMs));
  const points = [];
  for (let k = bucketCount; k >= 1; k -= 1) {
    const b0 = Math.max(tStart, tEnd - k * bucketMs);
    const b1 = tEnd - (k - 1) * bucketMs;
    const v0 = Math.round(interpolateCumulativeAt(pts, b0));
    const v1 = Math.round(interpolateCumulativeAt(pts, b1));
    const delta = v1 - v0;
    points.push({
      ts: new Date(b1).toISOString(),
      tsMs: b1,
      value: delta,
      hudTime: formatBucketRangeHud(b0, b1, bucketLabel),
    });
  }

  return { points, bucketSec, bucketLabel, totalDelta };
}

function buildModalSeriesFromHistory(history, hours = state.modalHours) {
  const onlinePoints = [];
  const playPoints = [];
  const likePoints = [];
  for (const h of history || []) {
    const tsMs = parseModalPointMs(h.collected_at, h.collected_at_ts);
    onlinePoints.push({
      ts: h.collected_at,
      tsMs,
      time: h.collected_at,
      value: Number(h.online_count || 0),
      raw: h.online_raw,
    });
    if (h.play_count !== null && h.play_count !== undefined && Number.isFinite(Number(h.play_count))) {
      playPoints.push({
        ts: h.collected_at,
        tsMs,
        time: h.collected_at,
        value: Number(h.play_count),
      });
    }
    if (h.like_count !== null && h.like_count !== undefined && Number.isFinite(Number(h.like_count))) {
      likePoints.push({
        ts: h.collected_at,
        tsMs,
        time: h.collected_at,
        value: Number(h.like_count),
      });
    }
  }

  const playDeltaSeries = buildIntervalDeltaSeries(playPoints, hours);
  const likeDeltaSeries = buildIntervalDeltaSeries(likePoints, hours);

  return {
    onlinePoints,
    playPoints,
    likePoints,
    playDeltaPoints: playDeltaSeries.points,
    playDeltaBucketLabel: playDeltaSeries.bucketLabel,
    likeDeltaPoints: likeDeltaSeries.points,
    likeDeltaBucketLabel: likeDeltaSeries.bucketLabel,
  };
}

function renderModalCharts(series, animate = true) {
  const onlineEl = document.getElementById("modalVideoChart");
  const playEl = document.getElementById("modalVideoPlayChart");
  const playDeltaEl = document.getElementById("modalVideoPlayDeltaChart");
  const likeDeltaEl = document.getElementById("modalVideoLikeDeltaChart");
  const onlinePoints = series?.onlinePoints || [];
  const playPoints = series?.playPoints || [];
  const likePoints = series?.likePoints || [];
  const playDeltaPoints = series?.playDeltaPoints || [];
  const likeDeltaPoints = series?.likeDeltaPoints || [];

  if (onlineEl) {
    renderTelemetryChart(onlineEl, onlinePoints, {
      channelColor: "var(--sig-cyan)",
      fillId: "modal-video-online",
      label: "实时在线",
      formatValue: (v) => `${fmtNumber(v)} 人`,
      animate,
      syncGroup: "video-modal",
      emptyText: "该时间窗口暂无在线人数采样点",
    });
  }

  if (playEl) {
    renderTelemetryChart(playEl, playPoints, {
      channelColor: "var(--sig-pink)",
      fillId: "modal-video-plays",
      label: "累计播放",
      formatValue: (v) => fmtNumber(v),
      animate,
      syncGroup: "video-modal",
      emptyText: "该时间窗口暂无播放量采样点",
    });
  }

  if (playDeltaEl) {
    renderTelemetryChart(playDeltaEl, playDeltaPoints, {
      channelColor: "var(--sig-emerald)",
      fillId: "modal-video-play-delta",
      label: series?.playDeltaBucketLabel
        ? `播放增量 (${series.playDeltaBucketLabel})`
        : "播放增量",
      formatValue: (v) => fmtSignedDelta(v),
      animate,
      syncGroup: "video-modal",
      emptyText:
        playPoints.length === 1
          ? "需至少 2 个采样点以计算播放区间增量"
          : "该时间窗口暂无播放增量数据",
    });
  }

  if (likeDeltaEl) {
    renderTelemetryChart(likeDeltaEl, likeDeltaPoints, {
      channelColor: "var(--sig-violet)",
      fillId: "modal-video-like-delta",
      label: series?.likeDeltaBucketLabel
        ? `点赞增量 (${series.likeDeltaBucketLabel})`
        : "点赞增量",
      formatValue: (v) => fmtSignedDelta(v),
      animate,
      syncGroup: "video-modal",
      emptyText:
        likePoints.length === 1
          ? "已记录首笔基准，下次采样后生成点赞增量曲线"
          : "等待至少 2 次点赞采样后生成增量曲线",
    });
  }
}

function applySignedModalDeltaReadout(el, deltaVal) {
  if (!el) return;
  if (Number.isFinite(deltaVal)) {
    const rounded = Math.round(deltaVal);
    el.textContent = fmtSignedDelta(rounded);
    el.classList.toggle("is-up", rounded > 0);
    el.classList.toggle("is-down", rounded < 0);
  } else {
    el.textContent = "--";
    el.classList.remove("is-up", "is-down");
  }
}

function updateModalStatsFromVideoAndPoints(bvid, series) {
  const video = (state.videos || []).find((v) => v.bvid === bvid);
  const currentEl = document.getElementById("modalCurrentOnline");
  const peakEl = document.getElementById("modalPeakOnline");
  const playEl = document.getElementById("modalPlayCount");
  const playDeltaEl = document.getElementById("modalPlayDelta");
  const likeEl = document.getElementById("modalLikeCount");
  const likeDeltaEl = document.getElementById("modalLikeDelta");
  const onlineChipEl = document.getElementById("modalOnlineChip");
  const playChipEl = document.getElementById("modalPlayChip");
  const playDeltaChipEl = document.getElementById("modalPlayDeltaChip");
  const likeDeltaChipEl = document.getElementById("modalLikeDeltaChip");
  const playDeltaKickerEl = document.getElementById("modalPlayDeltaKicker");
  const likeDeltaKickerEl = document.getElementById("modalLikeDeltaKicker");

  const onlinePoints = series?.onlinePoints || [];
  const playPoints = series?.playPoints || [];
  const likePoints = series?.likePoints || [];
  const playDeltaPoints = series?.playDeltaPoints || [];
  const likeDeltaPoints = series?.likeDeltaPoints || [];

  const vals = onlinePoints.map((p) => Number(p.value)).filter((n) => Number.isFinite(n));
  const latestPtVal = vals.length > 0 ? vals[vals.length - 1] : null;
  const currentOnline =
    video?.last_online_count !== undefined && video?.last_online_count !== null
      ? Number(video.last_online_count)
      : latestPtVal;
  const peakOnline = vals.length > 0 ? Math.max(...vals) : null;

  const latestPlayVal =
    video?.play_count !== undefined && video?.play_count !== null
      ? Number(video.play_count)
      : playPoints.length > 0
        ? Number(playPoints[playPoints.length - 1].value)
        : null;

  const playDelta =
    playPoints.length >= 2
      ? Number(playPoints[playPoints.length - 1].value) - Number(playPoints[0].value)
      : null;

  const hasVideoLikeStat =
    Boolean(video?.stats_synced_at) || Number(video?.like_count || 0) > 0;
  const latestLikeVal = hasVideoLikeStat
    ? Number(video.like_count)
    : likePoints.length > 0
      ? Number(likePoints[likePoints.length - 1].value)
      : null;

  const likeDelta =
    likePoints.length >= 2
      ? Number(likePoints[likePoints.length - 1].value) - Number(likePoints[0].value)
      : null;

  if (currentEl) {
    currentEl.textContent = Number.isFinite(currentOnline) ? `${fmtNumber(currentOnline)} 人` : "--";
  }
  if (peakEl) {
    peakEl.textContent = Number.isFinite(peakOnline) ? `${fmtNumber(peakOnline)} 人` : "--";
  }
  if (playEl) {
    playEl.textContent = Number.isFinite(latestPlayVal) ? fmtNumber(latestPlayVal) : "--";
  }
  applySignedModalDeltaReadout(playDeltaEl, playDelta);

  if (likeEl) {
    likeEl.textContent = Number.isFinite(latestLikeVal) ? fmtNumber(latestLikeVal) : "--";
  }
  applySignedModalDeltaReadout(likeDeltaEl, likeDelta);

  if (onlineChipEl) {
    onlineChipEl.textContent = Number.isFinite(currentOnline) ? `${fmtNumber(currentOnline)} 人` : "--";
  }
  if (playChipEl) {
    playChipEl.textContent = Number.isFinite(latestPlayVal) ? fmtNumber(latestPlayVal) : "--";
  }

  const playBucketLabel = series?.playDeltaBucketLabel || "";
  if (playDeltaKickerEl) {
    playDeltaKickerEl.textContent = playBucketLabel
      ? `分时增量 · 每 ${playBucketLabel}`
      : "分时增量";
  }
  if (playDeltaChipEl) {
    if (playDeltaPoints.length > 0) {
      const latestBucketPlay = playDeltaPoints[playDeltaPoints.length - 1].value;
      playDeltaChipEl.textContent = playBucketLabel
        ? `${fmtSignedDelta(latestBucketPlay)} / ${playBucketLabel}`
        : fmtSignedDelta(latestBucketPlay);
    } else {
      playDeltaChipEl.textContent = "--";
    }
  }

  const likeBucketLabel = series?.likeDeltaBucketLabel || "";
  if (likeDeltaKickerEl) {
    likeDeltaKickerEl.textContent = likeBucketLabel
      ? `分时增量 · 每 ${likeBucketLabel}`
      : "分时增量";
  }
  if (likeDeltaChipEl) {
    if (likeDeltaPoints.length > 0) {
      const latestBucketLike = likeDeltaPoints[likeDeltaPoints.length - 1].value;
      likeDeltaChipEl.textContent = likeBucketLabel
        ? `${fmtSignedDelta(latestBucketLike)} / ${likeBucketLabel}`
        : fmtSignedDelta(latestBucketLike);
    } else {
      likeDeltaChipEl.textContent = "--";
    }
  }
}

function setModalChartsPlaceholder(message) {
  for (const id of [
    "modalVideoChart",
    "modalVideoPlayChart",
    "modalVideoPlayDeltaChart",
    "modalVideoLikeDeltaChart",
  ]) {
    const el = document.getElementById(id);
    if (el) {
      el.innerHTML = `<div class="chart-empty mono">${escapeHtml(message)}</div>`;
    }
  }
}

function refreshOpenDemoModal(bvid) {
  const history = getDemoVideoHistory(state, bvid, { hours: state.modalHours });
  const series = buildModalSeriesFromHistory(history, state.modalHours);
  state.modalPoints = series;
  updateModalStatsFromVideoAndPoints(bvid, series);
  renderModalCharts(series, false);
}

async function loadVideoModalSeries(bvid, hours, { animate = true } = {}) {
  const chartEl = document.getElementById("modalVideoChart");
  const playChartEl = document.getElementById("modalVideoPlayChart");
  if (!bvid || !chartEl || !playChartEl) return;

  const reqId = ++state.modalReqSeq;
  const targetHours = Number(hours ?? 24);

  if (state.demoMode) {
    const history = getDemoVideoHistory(state, bvid, { hours: targetHours });
    if (state.modalBvid !== bvid || reqId !== state.modalReqSeq) return;
    const series = buildModalSeriesFromHistory(history, targetHours);
    state.modalPoints = series;
    updateModalStatsFromVideoAndPoints(bvid, series);
    requestAnimationFrame(() => {
      if (state.modalBvid === bvid && reqId === state.modalReqSeq) {
        renderModalCharts(series, animate);
      }
    });
    return;
  }

  clearChartsInGroup("video-modal");
  updateModalStatsFromVideoAndPoints(bvid, null);
  setModalChartsPlaceholder("正在拉取视频趋势与区间增量采样序列...");

  try {
    const res = await apiFetch(
      `/api/videos/${encodeURIComponent(bvid)}/history?hours=${encodeURIComponent(targetHours)}`
    );
    if (state.modalBvid !== bvid || reqId !== state.modalReqSeq) return;
    const series = buildModalSeriesFromHistory(res.history || [], targetHours);
    state.modalPoints = series;
    updateModalStatsFromVideoAndPoints(bvid, series);
    requestAnimationFrame(() => {
      if (state.modalBvid === bvid && reqId === state.modalReqSeq) {
        renderModalCharts(series, animate);
      }
    });
  } catch (err) {
    if (state.modalBvid !== bvid || reqId !== state.modalReqSeq) return;
    setModalChartsPlaceholder(`波形拉取失败: ${err.message}`);
  }
}

async function openVideoModal(bvid, fallbackTitle = "") {
  if (!bvid) return;
  const modal = document.getElementById("videoModal");
  const titleEl = document.getElementById("modalVideoTitle");
  const metaEl = document.getElementById("modalVideoMeta");
  const linkEl = document.getElementById("modalBiliLink");
  const coverWrap = document.getElementById("modalCoverWrap");
  const coverImg = document.getElementById("modalVideoCover");
  const chartEl = document.getElementById("modalVideoChart");
  const playChartEl = document.getElementById("modalVideoPlayChart");
  if (!modal || !chartEl || !playChartEl) return;

  const video = (state.videos || []).find((v) => v.bvid === bvid);
  const resolvedTitle = video?.title || fallbackTitle || bvid;

  state.modalBvid = bvid;
  state.modalTitle = resolvedTitle;
  state.modalHours = Number(state.hours ?? 24);
  state.modalPoints = null;

  if (titleEl) titleEl.textContent = resolvedTitle;
  if (metaEl) {
    const partInfo = video ? `${Number(video.cid_count || 1)}集 · ${fmtDuration(video.duration)}` : "单视频监控";
    metaEl.textContent = `${bvid} · ${partInfo}${state.demoMode ? " · 示例数据" : ""}`;
  }
  if (linkEl) {
    linkEl.href = `https://www.bilibili.com/video/${encodeURIComponent(bvid)}`;
  }
  if (coverWrap && coverImg) {
    coverImg.src = resolveCoverSrc(video?.pic, bvid);
    attachCoverFallback(coverImg, bvid);
    coverWrap.classList.remove("hidden");
  }

  clearChartsInGroup("video-modal");
  updateModalStatsFromVideoAndPoints(bvid, null);
  setModalChartsPlaceholder("正在拉取视频趋势与区间增量采样序列...");

  if (!modal.open) {
    if (typeof modal.showModal === "function") {
      try {
        modal.showModal();
      } catch {
        modal.setAttribute("open", "");
      }
    } else {
      modal.setAttribute("open", "");
    }
  }

  syncModalRangeControls();
  requestAnimationFrame(() => syncModalRangeControls());
  await loadVideoModalSeries(bvid, state.modalHours, { animate: true });
}

function closeVideoModal() {
  state.modalReqSeq += 1;
  const modal = document.getElementById("videoModal");
  if (modal && modal.open) {
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.removeAttribute("open");
    }
  }
  clearChartsInGroup("video-modal");
  state.modalBvid = null;
  state.modalPoints = null;
}

function syncAdminAuthState(adminAuth) {
  if (!adminAuth || typeof adminAuth !== "object") return;
  state.adminAuth = {
    enabled: Boolean(adminAuth.enabled),
    authenticated: adminAuth.authenticated !== false,
    source: String(adminAuth.source || "none"),
  };
  syncAdminAuthControls();
}

function setPasswordFieldVisibility(inputEl, toggleBtn, visible) {
  if (!inputEl || !toggleBtn) return;
  inputEl.type = visible ? "text" : "password";
  toggleBtn.setAttribute("aria-pressed", visible ? "true" : "false");
  toggleBtn.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  toggleBtn.querySelector(".icon-eye")?.classList.toggle("hidden", visible);
  toggleBtn.querySelector(".icon-eye-off")?.classList.toggle("hidden", !visible);
}

function setupPasswordToggleButton(btnId, inputId) {
  const btn = document.getElementById(btnId);
  const input = document.getElementById(inputId);
  if (!btn || !input || btn.dataset.toggleBound === "1") return;
  btn.dataset.toggleBound = "1";
  btn.addEventListener("click", () => {
    const nextVisible = input.type === "password";
    setPasswordFieldVisibility(input, btn, nextVisible);
  });
}

function syncAdminAuthControls() {
  const { enabled, authenticated, source } = state.adminAuth;

  // Left sidebar lock status button
  const sidebarBtn = document.getElementById("btnSidebarLockAdmin");
  const sidebarLabel = document.getElementById("sidebarLockLabel");
  const iconLocked = document.getElementById("sidebarLockIconLocked");
  const iconUnlocked = document.getElementById("sidebarLockIconUnlocked");

  if (sidebarBtn) {
    sidebarBtn.classList.toggle("hidden", !enabled);
    sidebarBtn.classList.toggle("is-unlocked", enabled && authenticated);
    sidebarBtn.classList.toggle("is-locked", enabled && !authenticated);
    sidebarBtn.setAttribute(
      "title",
      authenticated ? "点击立即锁定管理员配置权限" : "点击验证管理员密码"
    );
  }
  if (sidebarLabel) {
    sidebarLabel.textContent = authenticated ? "已解锁 · 锁定" : "需密码验证";
  }
  if (iconLocked) iconLocked.classList.toggle("hidden", authenticated);
  if (iconUnlocked) iconUnlocked.classList.toggle("hidden", !authenticated);

  // Settings drawer admin password controls
  const badge = document.getElementById("adminPasswordStatusBadge");
  const pwInput = document.getElementById("inputAdminPassword");
  const pwToggle = document.getElementById("btnToggleSettingsAdminPassword");
  const clearBtn = document.getElementById("btnClearAdminPassword");
  const lockBtn = document.getElementById("btnLockAdmin");

  if (badge) {
    badge.classList.remove("set", "status-success", "status-warn", "warn");
    if (source === "env") {
      badge.textContent = "环境变量已启用";
      badge.classList.add("set", "status-success");
    } else if (source === "db") {
      badge.textContent = "已启用密码保护";
      badge.classList.add("set", "status-success");
    } else {
      badge.textContent = "未设置";
    }
  }

  if (pwInput) {
    const managedByEnv = source === "env";
    pwInput.disabled = managedByEnv;
    if (managedByEnv) {
      pwInput.value = "";
      pwInput.placeholder = "当前由环境变量 BILI_ADMIN_PASSWORD 托管";
    } else {
      pwInput.placeholder =
        source === "db"
          ? "留空保持当前密码；输入新密码以修改"
          : "输入新密码以启用；留空则不设密码";
    }
  }
  if (pwToggle) {
    pwToggle.disabled = source === "env";
  }
  if (clearBtn) {
    clearBtn.classList.toggle("hidden", source !== "db");
  }
  if (lockBtn) {
    lockBtn.classList.toggle("hidden", !(enabled && authenticated));
  }
}

function openAdminAuthModal() {
  const modal = document.getElementById("adminAuthModal");
  const input = document.getElementById("adminAuthInput");
  const toggleBtn = document.getElementById("btnToggleAdminAuthPassword");
  const errBox = document.getElementById("adminAuthErrorBox");
  if (!modal) return;

  if (input) {
    input.value = "";
    input.setCustomValidity("");
    input.setAttribute("aria-invalid", "false");
  }
  setPasswordFieldVisibility(input, toggleBtn, false);
  if (errBox) {
    errBox.classList.add("hidden");
    errBox.textContent = "";
  }

  showDialogModal(modal);
  requestAnimationFrame(() => input?.focus());
}

function closeAdminAuthModal({ clearPending = true } = {}) {
  if (clearPending) {
    state.pendingAdminAction = null;
  }
  closeDialogModal(document.getElementById("adminAuthModal"));
}

function ensureAdminAuthorized(actionLabel, onAuthorized = null) {
  if (!state.adminAuth.enabled || state.adminAuth.authenticated) {
    if (typeof onAuthorized === "function") {
      onAuthorized();
    }
    return true;
  }
  state.pendingAdminAction = {
    label: actionLabel || "管理配置",
    callback: typeof onAuthorized === "function" ? onAuthorized : null,
  };
  openAdminAuthModal(state.pendingAdminAction.label);
  return false;
}

async function lockAdminSession({ closeSettings = false } = {}) {
  try {
    const res = await apiFetch("/api/admin/logout", { method: "POST" });
    syncAdminAuthState(res);
    if (closeSettings) {
      closeSettingsModal();
    }
    showToast("已锁定管理权限，下次配置需重新验证密码");
  } catch (err) {
    showToast(`锁定失败: ${err.message}`, "err");
  }
}

/* ---------- Settings Slide-Over Modal & Form ---------- */

function openSettingsModal({ focusUid = false } = {}) {
  const modal = document.getElementById("settingsModal");
  if (!modal) return;
  loadSettings();
  if (!modal.open) {
    if (typeof modal.showModal === "function") {
      try {
        modal.showModal();
      } catch {
        modal.setAttribute("open", "");
      }
    } else {
      modal.setAttribute("open", "");
    }
  }
  if (focusUid) {
    requestAnimationFrame(() => document.getElementById("inputUid")?.focus());
  }
}

function closeSettingsModal() {
  const modal = document.getElementById("settingsModal");
  if (modal && modal.open) {
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.removeAttribute("open");
    }
  }
}

function showDialogModal(modal) {
  if (!modal) return;
  if (!modal.open) {
    if (typeof modal.showModal === "function") {
      try {
        modal.showModal();
      } catch {
        modal.setAttribute("open", "");
      }
    } else {
      modal.setAttribute("open", "");
    }
  }
}

function closeDialogModal(modal) {
  if (modal && modal.open) {
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.removeAttribute("open");
    }
  }
}

function populateSettingsForm(settings) {
  if (!settings) return;
  state.backendSettings = settings;
  state.backendUid = String(settings.uid || "").trim();
  if (state.backendUid) {
    state.activeUid = state.backendUid;
  }

  const uidEl = document.getElementById("inputUid");
  const intervalEl = document.getElementById("inputInterval");
  const syncEl = document.getElementById("inputVideoSync");
  const delayEl = document.getElementById("inputDelay");
  const enabledEl = document.getElementById("inputEnabled");
  const cookieEl = document.getElementById("inputCookie");
  const adminPwEl = document.getElementById("inputAdminPassword");
  const adminPwToggle = document.getElementById("btnToggleSettingsAdminPassword");
  const badge = document.getElementById("cookieStatusBadge");

  if (uidEl) {
    uidEl.value = settings.uid || "";
    uidEl.setCustomValidity("");
    uidEl.setAttribute("aria-invalid", "false");
  }
  if (intervalEl) intervalEl.value = settings.interval_minutes ?? 5;
  if (syncEl) syncEl.value = settings.video_sync_interval_minutes ?? 30;
  if (delayEl) delayEl.value = settings.request_delay_ms ?? 200;
  if (enabledEl) enabledEl.checked = Boolean(settings.enabled);
  if (cookieEl) cookieEl.value = "";
  if (adminPwEl) {
    adminPwEl.value = "";
    adminPwEl.setCustomValidity("");
    adminPwEl.setAttribute("aria-invalid", "false");
  }
  setPasswordFieldVisibility(adminPwEl, adminPwToggle, false);

  if (settings.admin_auth) {
    syncAdminAuthState(settings.admin_auth);
  } else {
    syncAdminAuthControls();
  }

  if (badge) {
    if (settings.has_cookie) {
      badge.textContent = `已配置 · ${settings.cookie_preview}`;
      badge.classList.add("set", "status-success");
    } else {
      badge.textContent = "未配置";
      badge.classList.remove("set", "status-success");
    }
  }
}

/* ---------- Account Add/Edit & Delete Confirmation Modals ---------- */

function openAccountModal({ mode = "add", uid = "" } = {}) {
  const modal = document.getElementById("accountModal");
  const kicker = document.getElementById("accountModalKicker");
  const title = document.getElementById("accountModalTitle");
  const uidEl = document.getElementById("accountInputUid");
  const enabledEl = document.getElementById("accountInputEnabled");
  const saveBtn = document.getElementById("btnAccountSave");
  const testBox = document.getElementById("accountTestResultBox");
  if (!modal) return;

  state.accountModalMode = mode === "edit" ? "edit" : "add";
  state.accountModalUid = String(uid || "").trim();

  if (testBox) {
    testBox.className = "test-result hidden";
    testBox.textContent = "";
  }

  const targetAcc =
    state.accountModalMode === "edit"
      ? (state.accounts || []).find((a) => a.uid === state.accountModalUid)
      : null;

  if (state.accountModalMode === "edit" && targetAcc) {
    if (kicker) kicker.textContent = "编辑监控频道";
    if (title) title.textContent = `配置账号 · ${targetAcc.account_name || targetAcc.uid}`;
    if (uidEl) {
      uidEl.value = targetAcc.uid;
      uidEl.readOnly = true;
      uidEl.setCustomValidity("");
      uidEl.setAttribute("aria-invalid", "false");
    }
    if (enabledEl) enabledEl.checked = Boolean(targetAcc.enabled);
    if (saveBtn) saveBtn.textContent = "保存配置";
  } else {
    state.accountModalMode = "add";
    state.accountModalUid = "";
    if (kicker) kicker.textContent = "新增监控频道";
    if (title) title.textContent = "添加监控账号";
    if (uidEl) {
      uidEl.value = "";
      uidEl.readOnly = false;
      uidEl.setCustomValidity("");
      uidEl.setAttribute("aria-invalid", "false");
    }
    if (enabledEl) enabledEl.checked = true;
    if (saveBtn) saveBtn.textContent = "添加并监控";
  }

  showDialogModal(modal);
  requestAnimationFrame(() => {
    if (state.accountModalMode === "add") {
      uidEl?.focus();
    } else {
      enabledEl?.focus();
    }
  });
}

function closeAccountModal() {
  closeDialogModal(document.getElementById("accountModal"));
}

function openDeleteAccountModal(uid) {
  const modal = document.getElementById("confirmDeleteModal");
  const msgEl = document.getElementById("confirmDeleteMessage");
  const checkEl = document.getElementById("deleteDataCheckbox");
  if (!modal) return;

  const targetAcc = (state.accounts || []).find((a) => a.uid === uid);
  state.deleteTargetUid = uid;

  if (msgEl) {
    const label = targetAcc?.account_name
      ? `${targetAcc.account_name} (UID: ${uid})`
      : `账号 UID: ${uid}`;
    msgEl.textContent = `确定要从左侧监控列表中移除「${label}」吗？`;
  }
  if (checkEl) checkEl.checked = false;

  showDialogModal(modal);
}

function closeDeleteAccountModal() {
  closeDialogModal(document.getElementById("confirmDeleteModal"));
  state.deleteTargetUid = "";
}

/* ---------- Accessible Form Validation Wiring (modern-web-guidance: forms, accessible-error-announcement) ---------- */

function wireAccessibleForm(formId, fieldErrorSpecs, techInputIds = []) {
  const form = document.getElementById(formId);
  if (!form) return null;

  const errorAnnouncer = createVisuallyHiddenStatus("formErrorAnnouncer");
  let announceTimer = 0;
  const dirtyFields = new WeakMap();
  const flaggedFields = new WeakSet();
  const announcedFields = new WeakSet();

  techInputIds.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.setAttribute("autocorrect", "off");
    el.setAttribute("autocapitalize", "none");
    el.setAttribute("spellcheck", "false");
  });

  const specByElement = new Map();

  for (const spec of fieldErrorSpecs) {
    const el = document.getElementById(spec.id);
    if (!el) continue;
    if (spec.pattern && !el.hasAttribute("pattern")) {
      el.setAttribute("pattern", spec.pattern);
    }
    el.dataset.error = spec.errId;
    el.setAttribute("aria-invalid", "false");

    let errNode = document.getElementById(spec.errId);
    if (!errNode && el.parentElement) {
      errNode = document.createElement("div");
      errNode.id = spec.errId;
      errNode.className = "field-error mono hidden";
      errNode.style.color = "var(--sig-danger)";
      errNode.style.fontSize = "12px";
      el.parentElement.appendChild(errNode);
    }
    specByElement.set(el, { ...spec, errNode });
  }

  // Link error id in aria-describedby ONLY while shown so hidden text isn't read prematurely
  const linkError = (el, shown) => {
    const errorId = el.dataset.error;
    if (!errorId) return;
    const ids = (el.getAttribute("aria-describedby") || "")
      .split(/\s+/)
      .filter((id) => id && id !== errorId);
    if (shown) ids.push(errorId);
    if (ids.length > 0) {
      el.setAttribute("aria-describedby", ids.join(" "));
    } else {
      el.removeAttribute("aria-describedby");
    }
  };

  const evaluateCustomConstraint = (el) => {
    const spec = specByElement.get(el);
    if (!spec || !spec.customValidity) return;
    const msg = spec.getMessage(el);
    el.setCustomValidity(msg);
  };

  const syncFieldState = (el, { forceShow = false, announce = false } = {}) => {
    const spec = specByElement.get(el);
    if (!spec) return true;

    evaluateCustomConstraint(el);
    const meta = dirtyFields.get(el) || { edited: false, blurred: false };
    const supportsUserInvalid =
      typeof CSS !== "undefined" &&
      typeof CSS.supports === "function" &&
      CSS.supports("selector(:user-invalid)");

    const shouldReflectError =
      forceShow ||
      (meta.edited && meta.blurred) ||
      (supportsUserInvalid && el.matches(":user-invalid"));

    const isInvalid = !el.validity.valid && shouldReflectError;
    const errMsg = !el.validity.valid ? spec.getMessage(el) || el.validationMessage : "";

    if (isInvalid) {
      el.setAttribute("aria-invalid", "true");
      el.classList.add("user-invalid-fallback");
      if (spec.errNode) {
        spec.errNode.textContent = `输入有误：${errMsg}`;
        spec.errNode.classList.remove("hidden");
      }
      linkError(el, true);
      flaggedFields.add(el);

      if (announce && !announcedFields.has(el)) {
        announcedFields.add(el);
        clearTimeout(announceTimer);
        errorAnnouncer.textContent = "";
        announceTimer = window.setTimeout(() => {
          errorAnnouncer.textContent = errMsg;
        }, 500);
      }
    } else if (flaggedFields.has(el) || el.validity.valid) {
      el.setAttribute("aria-invalid", "false");
      el.classList.remove("user-invalid-fallback");
      if (spec.errNode) {
        spec.errNode.textContent = "";
        spec.errNode.classList.add("hidden");
      }
      linkError(el, false);
      flaggedFields.delete(el);
      announcedFields.delete(el);
    }

    return el.validity.valid;
  };

  form.addEventListener("input", (ev) => {
    const el = ev.target;
    if (!specByElement.has(el)) return;
    const meta = dirtyFields.get(el) || { edited: false, blurred: false };
    meta.edited = true;
    dirtyFields.set(el, meta);

    if (specByElement.get(el)?.customValidity) {
      el.setCustomValidity("");
    }
    evaluateCustomConstraint(el);
    if (el.validity.valid || flaggedFields.has(el)) {
      syncFieldState(el, { forceShow: false, announce: false });
    }
  });

  form.addEventListener(
    "blur",
    (ev) => {
      const el = ev.target;
      if (!specByElement.has(el)) return;
      const meta = dirtyFields.get(el) || { edited: false, blurred: false };
      meta.blurred = true;
      dirtyFields.set(el, meta);
      if (meta.edited) {
        syncFieldState(el, { forceShow: true, announce: true });
      }
    },
    true
  );

  form.addEventListener(
    "invalid",
    (ev) => {
      const el = ev.target;
      if (!specByElement.has(el)) return;
      announcedFields.add(el);
      dirtyFields.set(el, { edited: true, blurred: true });
      syncFieldState(el, { forceShow: true, announce: false });
    },
    true
  );

  return {
    validateAllForSubmit() {
      let firstInvalid = null;
      for (const el of specByElement.keys()) {
        dirtyFields.set(el, { edited: true, blurred: true });
        const valid = syncFieldState(el, { forceShow: true, announce: false });
        if (!valid && !firstInvalid) {
          firstInvalid = el;
        }
      }
      if (firstInvalid) {
        firstInvalid.focus();
        return false;
      }
      return true;
    },
    resetValidation() {
      for (const [el, spec] of specByElement.entries()) {
        dirtyFields.delete(el);
        flaggedFields.delete(el);
        announcedFields.delete(el);
        el.setCustomValidity("");
        el.setAttribute("aria-invalid", "false");
        el.classList.remove("user-invalid-fallback");
        if (spec.errNode) {
          spec.errNode.textContent = "";
          spec.errNode.classList.add("hidden");
        }
        linkError(el, false);
      }
    },
  };
}

function wireAccessibleSettingsForm() {
  const uidMsg = (el) => {
    const val = el.value.trim();
    if (!val) return "请填写 B站纯数字账号编号（例如：100001001）。";
    if (!/^\d+$/.test(val)) return "账号编号必须为纯数字，请移除空格或非数字字符。";
    return "";
  };

  return wireAccessibleForm(
    "settingsForm",
    [
      {
        id: "inputUid",
        errId: "err-inputUid",
        pattern: "\\d+",
        customValidity: true,
        getMessage: uidMsg,
      },
      {
        id: "inputInterval",
        errId: "err-inputInterval",
        getMessage: (el) =>
          el.validity.valid ? "" : "采集间隔需在 1 ~ 1440 分钟之间。",
      },
      {
        id: "inputVideoSync",
        errId: "err-inputVideoSync",
        getMessage: (el) =>
          el.validity.valid ? "" : "视频同步周期需在 5 ~ 1440 分钟之间。",
      },
      {
        id: "inputDelay",
        errId: "err-inputDelay",
        getMessage: (el) =>
          el.validity.valid ? "" : "单视频请求间隔需在 50 ~ 5000 毫秒之间。",
      },
    ],
    ["inputUid", "inputCookie"]
  );
}

function wireAccessibleAccountForm() {
  return wireAccessibleForm(
    "accountForm",
    [
      {
        id: "accountInputUid",
        errId: "err-accountInputUid",
        pattern: "\\d+",
        customValidity: true,
        getMessage: (el) => {
          const val = el.value.trim();
          if (!val) return "请填写 B站纯数字账号编号（例如：100001001）。";
          if (!/^\d+$/.test(val)) return "账号编号必须为纯数字，请移除空格或非数字字符。";
          return "";
        },
      },
    ],
    ["accountInputUid"]
  );
}

/* ---------- Data Loaders & Mutations ---------- */

async function loadDashboard({ animateCharts = false, uid = null, reqId = null } = {}) {
  if (state.demoMode) {
    const demoData = getDemoDashboard(state, state.hours);
    renderDashboard(demoData, { animateCharts, isLiveTick: false });
    return demoData;
  }

  const targetUid = uid !== null ? uid : state.activeUid;
  const activeReqId = reqId !== null ? reqId : ++state.dashboardReqSeq;
  const uidQuery = targetUid ? `&uid=${encodeURIComponent(targetUid)}` : "";

  try {
    const data = await apiFetch(`/api/dashboard?hours=${state.hours}${uidQuery}`);
    if (activeReqId !== state.dashboardReqSeq) return null;

    if (data?.admin_auth) {
      syncAdminAuthState(data.admin_auth);
    }
    if (data?.settings) {
      state.backendSettings = data.settings;
      state.backendUid = String(data.settings.uid || "").trim();
      if (state.backendUid) {
        state.activeUid = state.backendUid;
      }
    }
    renderDashboard(data, { animateCharts, isLiveTick: false });
    return data;
  } catch (err) {
    showToast(`加载遥测面板失败: ${err.message}`, "err");
    return null;
  }
}

async function loadSettings() {
  try {
    const targetUid = state.activeUid || state.backendUid;
    const url = targetUid
      ? `/api/settings?uid=${encodeURIComponent(targetUid)}`
      : "/api/settings";
    const s = await apiFetch(url);
    populateSettingsForm(s);
    syncBannersAndDemoControls();
  } catch (err) {
    if (err?.status === 401) {
      closeSettingsModal();
      ensureAdminAuthorized("监控设置", () => openSettingsModal({ focusUid: false }));
      return;
    }
    showToast(`读取系统配置失败: ${err.message}`, "err");
  }
}

async function triggerCrawl(forceSyncVideos = false) {
  if (state.demoMode && !state.backendUid) {
    showToast("当前处于未绑定示例模式，请先添加真实账号编号", "warn");
    ensureAdminAuthorized("添加监控账号", () => openAccountModal({ mode: "add" }));
    return;
  }
  if (state.demoMode && state.backendUid) {
    await exitDemoMode();
  }

  const targetUid = state.activeUid || state.backendUid;
  const btnSync = document.getElementById("btnSyncVideos");
  state.isManualCrawling = true;
  if (btnSync) btnSync.disabled = true;
  updateLiveClockAndLaser();
  renderAccountSidebar();

  showToast(
    forceSyncVideos
      ? "正在全量同步投稿列表并采集实时在看人数..."
      : "正在执行即时遥测采样..."
  );

  try {
    const uidParam = targetUid ? `&uid=${encodeURIComponent(targetUid)}` : "";
    const res = await apiFetch(
      `/api/crawl/now?force_sync_videos=${forceSyncVideos}${uidParam}`,
      { method: "POST" }
    );
    showToast(
      `采样完成 · 粉丝 ${fmtNumber(res.follower_count)} · 实时在看 ${fmtNumber(res.online_viewers_total)} 人`
    );
    await loadDashboard({ animateCharts: false });
  } catch (err) {
    showToast(`采样未执行: ${err.message}`, "err");
  } finally {
    state.isManualCrawling = false;
    if (btnSync) btnSync.disabled = false;
    updateLiveClockAndLaser();
    renderAccountSidebar();
  }
}

async function runConnectionTest({ uidInputId, cookieInputId = "", boxId, btnId }) {
  const uidEl = document.getElementById(uidInputId);
  const uid = uidEl?.value.trim() || "";
  const cookieVal = cookieInputId
    ? document.getElementById(cookieInputId)?.value.trim() || ""
    : "";
  const box = document.getElementById(boxId);
  const btnTest = document.getElementById(btnId);

  if (!uid || !/^\d+$/.test(uid)) {
    showToast("请先输入有效的纯数字 B站账号编号", "err");
    uidEl?.focus();
    return;
  }
  if (!box) return;

  if (btnTest) btnTest.disabled = true;
  box.className = "test-result mono";
  box.textContent = "正在测试连接 B站接口...";
  box.classList.remove("hidden");

  try {
    const payload = { uid };
    if (cookieVal) payload.cookie = cookieVal;
    const res = await apiFetch("/api/test-connection", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      box.className = "test-result ok is-ok";
      const sampleList =
        Array.isArray(res.sample_videos) && res.sample_videos.length > 0
          ? `<div class="muted" style="margin-top:6px">近期投稿示例：${res.sample_videos
              .map((v) => `<span class="mono">[${escapeHtml(v.bvid)}]</span> ${escapeHtml(v.title)}`)
              .join(" / ")}</div>`
          : "";

      box.innerHTML = `
        <div><span class="status-chip status-success success mono">连接成功</span> <strong>${escapeHtml(res.name || `账号 ${res.uid}`)}</strong> <span class="mono muted">(编号: ${escapeHtml(res.uid)})</span></div>
        <div class="mono" style="margin-top:6px">粉丝：<strong>${fmtNumber(res.follower)}</strong> · 关注：${fmtNumber(res.following)} · 投稿视频：<strong>${fmtNumber(res.video_total)}</strong></div>
        ${sampleList}
      `;
    } else {
      box.className = "test-result fail err is-err";
      box.textContent = `连接失败：${res.error || "未知响应异常"}`;
    }
  } catch (err) {
    box.className = "test-result fail err is-err";
    box.textContent = `测试请求失败：${err.message}`;
  } finally {
    if (btnTest) btnTest.disabled = false;
  }
}

/* ---------- Light-Dismiss Fallback for <dialog closedby="any"> (modern-web-guidance: dialog) ---------- */

function installDialogLightDismissFallback(dialog) {
  if (!dialog || "closedBy" in HTMLDialogElement.prototype) return;
  const outside = (ev) => {
    const r = dialog.getBoundingClientRect();
    return (
      ev.clientX < r.left ||
      ev.clientX > r.right ||
      ev.clientY < r.top ||
      ev.clientY > r.bottom
    );
  };
  let pressedOutside = false;
  dialog.addEventListener("pointerdown", (ev) => {
    pressedOutside = outside(ev);
  });
  dialog.addEventListener("pointerup", (ev) => {
    if (pressedOutside && outside(ev)) {
      dialog.close();
    }
    pressedOutside = false;
  });
}

/* ---------- Roving-Tabindex Fallback for focusgroup="toolbar wrap" (modern-web-guidance: toolbar) ---------- */

function installToolbarRovingFocusFallback(bar, itemSelector) {
  if (!bar || "focusGroup" in HTMLElement.prototype) return;
  const getItems = () => Array.from(bar.querySelectorAll(itemSelector));
  const initialItems = getItems();
  const activeIdx = Math.max(
    0,
    initialItems.findIndex(
      (b) => b.getAttribute("aria-pressed") === "true" || b.classList.contains("active")
    )
  );
  initialItems.forEach((b, i) => {
    b.tabIndex = i === activeIdx ? 0 : -1;
  });

  bar.addEventListener("focusin", ({ target }) => {
    const items = getItems();
    if (items.includes(target)) {
      items.forEach((b) => {
        b.tabIndex = b === target ? 0 : -1;
      });
    }
  });

  bar.addEventListener("keydown", (ev) => {
    const items = getItems();
    const idx = items.indexOf(ev.target);
    if (idx === -1 || items.length === 0) return;

    const vertical = bar.getAttribute("aria-orientation") === "vertical";
    const rtl = getComputedStyle(bar).direction === "rtl";
    const steps = vertical
      ? { ArrowDown: 1, ArrowUp: -1 }
      : { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1 };

    let nextIdx = -1;
    if (Object.hasOwn(steps, ev.key)) {
      nextIdx = (idx + steps[ev.key] + items.length) % items.length;
    } else if (ev.key === "Home") {
      nextIdx = 0;
    } else if (ev.key === "End") {
      nextIdx = items.length - 1;
    }

    if (nextIdx !== -1) {
      ev.preventDefault();
      items[nextIdx].focus();
    }
  });
}

/* ---------- Event Binding ---------- */

function bindEvents(settingsValidator, accountValidator) {
  // Theme & Action Controls
  document.getElementById("btnThemeToggle")?.addEventListener("click", toggleTheme);
  document.getElementById("btnLivePulse")?.addEventListener("click", toggleDemoLivePulse);
  document.getElementById("btnLoadDemo")?.addEventListener("click", () =>
    activateDemoMode({ showNotification: true })
  );
  document.getElementById("btnExitDemo")?.addEventListener("click", exitDemoMode);
  document.getElementById("btnOpenSettings")?.addEventListener("click", () =>
    ensureAdminAuthorized("监控设置", () => openSettingsModal({ focusUid: false }))
  );
  document.getElementById("btnGoSettings")?.addEventListener("click", () =>
    ensureAdminAuthorized("添加监控账号", () => {
      accountValidator?.resetValidation();
      openAccountModal({ mode: "add" });
    })
  );
  document.getElementById("btnAddAccount")?.addEventListener("click", () => {
    ensureAdminAuthorized("添加监控账号", () => {
      accountValidator?.resetValidation();
      openAccountModal({ mode: "add" });
    });
  });
  document.getElementById("btnSidebarLockAdmin")?.addEventListener("click", () => {
    if (!state.adminAuth.enabled) return;
    if (state.adminAuth.authenticated) {
      lockAdminSession({ closeSettings: false });
    } else {
      ensureAdminAuthorized("解锁管理权限");
    }
  });
  document.getElementById("btnLockAdmin")?.addEventListener("click", () => {
    lockAdminSession({ closeSettings: true });
  });
  setupPasswordToggleButton("btnToggleAdminAuthPassword", "adminAuthInput");
  setupPasswordToggleButton("btnToggleSettingsAdminPassword", "inputAdminPassword");
  document.getElementById("btnSyncVideos")?.addEventListener("click", () => triggerCrawl(true));

  // Left Sidebar Account Delegation (switch / edit / delete) & Drag Reorder
  setupAccountRailReorder();
  document.getElementById("accountRailList")?.addEventListener("click", (ev) => {
    if (ev.target.closest(".account-drag-handle")) return;
    const actionBtn = ev.target.closest("[data-account-action]");
    if (actionBtn) {
      const uid = actionBtn.dataset.uid || "";
      const action = actionBtn.dataset.accountAction;
      if (action === "edit" && uid) {
        ensureAdminAuthorized("编辑监控账号", () => {
          accountValidator?.resetValidation();
          openAccountModal({ mode: "edit", uid });
        });
      } else if (action === "delete" && uid) {
        ensureAdminAuthorized("移除监控账号", () => openDeleteAccountModal(uid));
      }
      return;
    }

    const switchBtn = ev.target.closest("[data-account-switch]");
    if (switchBtn && switchBtn.dataset.accountSwitch) {
      if (state.activeTab !== "tab-overview") {
        switchMainTab("tab-overview", { animate: false });
      }
      switchAccount(switchBtn.dataset.accountSwitch);
    }
  });

  // View Tablist Switching (WAI-ARIA Tabs: Click, Arrow Keys & beforematch for hidden="until-found")
  const viewTabList = document.getElementById("viewTabList");
  if (viewTabList) {
    const getViewTabs = () => Array.from(viewTabList.querySelectorAll('[role="tab"]'));

    viewTabList.addEventListener("click", (ev) => {
      const tab = ev.target.closest('[role="tab"]');
      if (!tab || !tab.dataset.tabTarget) return;
      if (state.activeTab !== tab.dataset.tabTarget) {
        switchMainTab(tab.dataset.tabTarget, { animate: true });
      }
    });

    viewTabList.addEventListener("keydown", (ev) => {
      const tabs = getViewTabs();
      const idx = tabs.indexOf(ev.target);
      if (idx === -1 || tabs.length === 0) return;

      let nextIdx = -1;
      if (ev.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
      else if (ev.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
      else if (ev.key === "Home") nextIdx = 0;
      else if (ev.key === "End") nextIdx = tabs.length - 1;

      if (nextIdx !== -1) {
        ev.preventDefault();
        const nextTab = tabs[nextIdx];
        if (nextTab?.dataset.tabTarget) {
          switchMainTab(nextTab.dataset.tabTarget, { animate: false, focusTab: true });
        }
      }
    });
  }

  for (const panelId of ["tab-overview", "tab-compare"]) {
    document.getElementById(panelId)?.addEventListener("beforematch", () => {
      if (state.activeTab !== panelId) {
        switchMainTab(panelId, { animate: false });
      }
    });
  }

  // Multi-Account Comparison Board Sort & Drill-Down
  document.getElementById("compareSortGroup")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".compare-sort-btn");
    if (!btn) return;
    const mode = btn.dataset.sort === "order" ? "order" : "metric";
    if (state.compareSortMode === mode) return;
    state.compareSortMode = mode;
    document.querySelectorAll("#compareSortGroup .compare-sort-btn").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
    renderCompareBoard();
  });

  document.getElementById("tab-compare")?.addEventListener("click", (ev) => {
    const rowBtn = ev.target.closest("[data-compare-uid]");
    if (!rowBtn || !rowBtn.dataset.compareUid) return;
    const uid = rowBtn.dataset.compareUid;
    switchMainTab("tab-overview", { animate: false });
    switchAccount(uid);
  });

  for (const spec of LEADERBOARD_SPECS) {
    document.getElementById(spec.expandBtnId)?.addEventListener("click", () => {
      state[spec.limitKey] =
        Number(state[spec.limitKey] || LEADERBOARD_PAGE_SIZE) + LEADERBOARD_PAGE_SIZE;
      renderLeaderboardColumn(state.videos, spec);
    });
  }

  // Horizon Time-Window Selector
  document.getElementById("rangeGroup")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".range-btn");
    if (!btn) return;
    document.querySelectorAll("#rangeGroup .range-btn").forEach((b) => {
      b.classList.toggle("active", b === btn);
    });
    const rangeBar = document.getElementById("rangeActiveBar");
    if (rangeBar) {
      positionSlidingIndicator(rangeBar, btn, "x");
    }
    state.hours = Number(btn.dataset.hours || 0);
    loadDashboard({ animateCharts: true });
  });

  // Video Modal Time-Window Toolbar (focusgroup fallback + range switch)
  const modalRangeGroup = document.getElementById("modalRangeGroup");
  installToolbarRovingFocusFallback(modalRangeGroup, ".range-btn");
  modalRangeGroup?.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".range-btn");
    if (!btn || btn.getAttribute("aria-disabled") === "true") return;
    const nextHours = Number(btn.dataset.hours || 0);
    if (nextHours === Number(state.modalHours) && state.modalPoints) return;
    state.modalHours = nextHours;
    syncModalRangeControls();
    if (state.modalBvid) {
      loadVideoModalSeries(state.modalBvid, nextHours, { animate: true });
    }
  });

  // Delegated click & keyboard trigger for Video Oscilloscope Drawer
  document.addEventListener("click", (ev) => {
    if (ev.target.closest("a.bvid-link, a.bvid-pill")) return;
    const trigger = ev.target.closest(".rank-row");
    if (trigger && trigger.dataset.bvid) {
      openVideoModal(trigger.dataset.bvid, trigger.dataset.title || "");
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return;
    if (
      (ev.key === "Enter" || ev.key === " ") &&
      ev.target instanceof HTMLElement &&
      ev.target.classList.contains("rank-row")
    ) {
      ev.preventDefault();
      openVideoModal(ev.target.dataset.bvid, ev.target.dataset.title || "");
    }
  });

  // Native <dialog id="videoModal"> Close & Light-Dismiss Fallback
  const videoModal = document.getElementById("videoModal");
  document.getElementById("btnCloseModal")?.addEventListener("click", closeVideoModal);
  videoModal?.addEventListener("close", () => {
    videoModal.returnValue = "";
    state.modalReqSeq += 1;
    clearChartsInGroup("video-modal");
    state.modalBvid = null;
    state.modalPoints = null;
  });
  installDialogLightDismissFallback(videoModal);

  // Native <dialog id="settingsModal"> Close & Light-Dismiss Fallback
  const settingsModal = document.getElementById("settingsModal");
  document.getElementById("btnCloseSettingsModal")?.addEventListener("click", closeSettingsModal);
  settingsModal?.addEventListener("close", () => {
    settingsModal.returnValue = "";
  });
  installDialogLightDismissFallback(settingsModal);

  // Native <dialog id="accountModal"> Close & Light-Dismiss Fallback
  const accountModal = document.getElementById("accountModal");
  document.getElementById("btnCloseAccountModal")?.addEventListener("click", closeAccountModal);
  accountModal?.addEventListener("close", () => {
    accountModal.returnValue = "";
  });
  installDialogLightDismissFallback(accountModal);

  // Native <dialog id="adminAuthModal"> Close, Light-Dismiss & Verify Form
  const adminAuthModal = document.getElementById("adminAuthModal");
  const adminAuthInput = document.getElementById("adminAuthInput");
  const adminAuthErrorBox = document.getElementById("adminAuthErrorBox");
  document.getElementById("btnCloseAdminAuthModal")?.addEventListener("click", () =>
    closeAdminAuthModal({ clearPending: true })
  );
  document.getElementById("btnCancelAdminAuth")?.addEventListener("click", () =>
    closeAdminAuthModal({ clearPending: true })
  );
  adminAuthModal?.addEventListener("close", () => {
    adminAuthModal.returnValue = "";
    if (adminAuthInput) {
      adminAuthInput.value = "";
      adminAuthInput.setCustomValidity("");
      adminAuthInput.setAttribute("aria-invalid", "false");
    }
    if (adminAuthErrorBox) {
      adminAuthErrorBox.classList.add("hidden");
      adminAuthErrorBox.textContent = "";
    }
  });
  installDialogLightDismissFallback(adminAuthModal);

  adminAuthInput?.addEventListener("input", () => {
    adminAuthInput.setCustomValidity("");
    adminAuthInput.setAttribute("aria-invalid", "false");
    if (adminAuthErrorBox) {
      adminAuthErrorBox.classList.add("hidden");
      adminAuthErrorBox.textContent = "";
    }
  });

  document.getElementById("adminAuthForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const password = adminAuthInput?.value ?? "";
    const submitBtn = document.getElementById("btnSubmitAdminAuth");

    if (!password) {
      if (adminAuthInput) {
        adminAuthInput.setCustomValidity("请输入管理员密码");
        adminAuthInput.setAttribute("aria-invalid", "true");
        adminAuthInput.focus();
      }
      if (adminAuthErrorBox) {
        adminAuthErrorBox.textContent = "请输入管理员密码后再继续。";
        adminAuthErrorBox.classList.remove("hidden");
      }
      return;
    }

    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await apiFetch("/api/admin/verify", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      syncAdminAuthState(res);
      const pending = state.pendingAdminAction;
      state.pendingAdminAction = null;
      closeAdminAuthModal({ clearPending: false });
      showToast("管理员身份验证通过");
      if (typeof pending?.callback === "function") {
        pending.callback();
      }
    } catch (err) {
      if (adminAuthInput) {
        adminAuthInput.setAttribute("aria-invalid", "true");
        adminAuthInput.focus();
        adminAuthInput.select();
      }
      if (adminAuthErrorBox) {
        adminAuthErrorBox.textContent = err.message || "管理员密码验证失败";
        adminAuthErrorBox.classList.remove("hidden");
      }
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  // Native <dialog id="confirmDeleteModal"> Close & Actions
  const confirmDeleteModal = document.getElementById("confirmDeleteModal");
  document.getElementById("btnCancelDelete")?.addEventListener("click", closeDeleteAccountModal);
  document.getElementById("btnCancelDeleteFooter")?.addEventListener("click", closeDeleteAccountModal);
  confirmDeleteModal?.addEventListener("close", () => {
    confirmDeleteModal.returnValue = "";
    state.deleteTargetUid = "";
  });
  installDialogLightDismissFallback(confirmDeleteModal);

  document.getElementById("btnConfirmDelete")?.addEventListener("click", async () => {
    const uid = state.deleteTargetUid;
    if (!uid) return;
    const deleteData = Boolean(document.getElementById("deleteDataCheckbox")?.checked);
    const confirmBtn = document.getElementById("btnConfirmDelete");
    if (confirmBtn) confirmBtn.disabled = true;

    try {
      const res = await apiFetch(
        `/api/accounts/${encodeURIComponent(uid)}?delete_data=${deleteData}`,
        { method: "DELETE" }
      );
      state.accounts = res.accounts || [];
      state.activeUid = String(res.active_uid || "").trim();
      state.backendUid = state.activeUid;
      clearRankCaches({ resetLimits: true });
      clearChartsInGroup("overview");
      closeDeleteAccountModal();
      showToast(`已移除监控账号 ${uid}`);

      if (!state.activeUid) {
        activateDemoMode({ showNotification: false });
      } else {
        await loadDashboard({ animateCharts: true });
      }
    } catch (err) {
      showToast(`移除账号失败: ${err.message}`, "err");
    } finally {
      if (confirmBtn) confirmBtn.disabled = false;
    }
  });

  // Connection Tests (Settings + Account Drawer)
  document.getElementById("btnTestConn")?.addEventListener("click", () =>
    runConnectionTest({
      uidInputId: "inputUid",
      cookieInputId: "inputCookie",
      boxId: "testResultBox",
      btnId: "btnTestConn",
    })
  );
  document.getElementById("btnAccountTestConn")?.addEventListener("click", () =>
    runConnectionTest({
      uidInputId: "accountInputUid",
      boxId: "accountTestResultBox",
      btnId: "btnAccountTestConn",
    })
  );

  // Clear Saved Global Cookie (Settings Drawer)
  document.getElementById("btnClearCookie")?.addEventListener("click", async () => {
    const btnClear = document.getElementById("btnClearCookie");
    if (btnClear) btnClear.disabled = true;
    try {
      const updated = await apiFetch("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ cookie: "" }),
      });
      populateSettingsForm(updated);
      showToast("已清空服务端保存的全局账号凭证");
      await loadDashboard({ animateCharts: false });
    } catch (err) {
      showToast(`清空凭证失败: ${err.message}`, "err");
    } finally {
      if (btnClear) btnClear.disabled = false;
    }
  });

  // Clear Admin Password (Settings Drawer)
  document.getElementById("btnClearAdminPassword")?.addEventListener("click", async () => {
    const btnClear = document.getElementById("btnClearAdminPassword");
    if (btnClear) btnClear.disabled = true;
    try {
      const updated = await apiFetch("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ admin_password: "" }),
      });
      populateSettingsForm(updated);
      showToast("已清除管理员密码保护");
      await loadDashboard({ animateCharts: false });
    } catch (err) {
      showToast(`清除管理密码失败: ${err.message}`, "err");
    } finally {
      if (btnClear) btnClear.disabled = false;
    }
  });

  // Save Account Add/Edit Drawer
  document.getElementById("accountForm")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (accountValidator && !accountValidator.validateAllForSubmit()) {
      showToast("请填写有效的纯数字 B站账号编号", "err");
      return;
    }

    const saveBtn = document.getElementById("btnAccountSave");
    const uid = document.getElementById("accountInputUid")?.value.trim() || "";
    const enabledVal = Boolean(document.getElementById("accountInputEnabled")?.checked);

    if (saveBtn) saveBtn.disabled = true;

    try {
      if (state.accountModalMode === "edit" && state.accountModalUid) {
        const payload = { enabled: enabledVal };
        const res = await apiFetch(
          `/api/accounts/${encodeURIComponent(state.accountModalUid)}`,
          { method: "PUT", body: JSON.stringify(payload) }
        );
        state.accounts = res.accounts || state.accounts;
        showToast(`已更新账号 ${state.accountModalUid} 配置`);
        closeAccountModal();
        await loadDashboard({ animateCharts: false });
      } else {
        const res = await apiFetch("/api/accounts", {
          method: "POST",
          body: JSON.stringify({
            uid,
            enabled: enabledVal,
            set_active: true,
          }),
        });
        state.accounts = res.accounts || [];
        state.activeUid = String(res.active_uid || uid).trim();
        state.backendUid = state.activeUid;

        stopDemoPulseLoop();
        state.demoMode = false;
        state.demoLivePulse = false;
        state.demoDataset = null;
        clearRankCaches({ resetLimits: true });
        clearChartsInGroup("overview");

        syncBannersAndDemoControls();
        showToast(`已添加监控账号 ${uid}`);
        closeAccountModal();
        await loadDashboard({ animateCharts: true });

        if (enabledVal) {
          setTimeout(() => {
            if (!state.demoMode) loadDashboard({ animateCharts: false });
          }, 2800);
          setTimeout(() => {
            if (!state.demoMode) loadDashboard({ animateCharts: false });
          }, 7500);
        }
      }
    } catch (err) {
      showToast(`保存账号失败: ${err.message}`, "err");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  });

  // Save Settings Blueprint
  const settingsForm = document.getElementById("settingsForm");
  settingsForm?.setAttribute("novalidate", "");
  settingsForm?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (settingsValidator && !settingsValidator.validateAllForSubmit()) {
      showToast("请修正表单中标红的参数项后再保存", "err");
      return;
    }

    const saveBtn = document.getElementById("btnSaveSettings");
    const prevUid = state.backendUid;
    const payload = {
      uid: document.getElementById("inputUid")?.value.trim() || "",
      interval_minutes: Number(document.getElementById("inputInterval")?.value || 5),
      video_sync_interval_minutes: Number(document.getElementById("inputVideoSync")?.value || 30),
      request_delay_ms: Number(document.getElementById("inputDelay")?.value || 200),
      enabled: Boolean(document.getElementById("inputEnabled")?.checked),
    };
    const cookieVal = document.getElementById("inputCookie")?.value.trim() || "";
    if (cookieVal) {
      payload.cookie = cookieVal;
    }
    const adminPwEl = document.getElementById("inputAdminPassword");
    const rawAdminPw =
      state.adminAuth.source !== "env" && adminPwEl ? adminPwEl.value : "";
    const adminPwChanged = Boolean(rawAdminPw);
    if (rawAdminPw) {
      if (!rawAdminPw.trim()) {
        adminPwEl?.setAttribute("aria-invalid", "true");
        adminPwEl?.focus();
        showToast("管理员密码不能为空白字符", "err");
        return;
      }
      payload.admin_password = rawAdminPw;
    }

    if (saveBtn) saveBtn.disabled = true;

    try {
      const updated = await apiFetch("/api/settings", {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      populateSettingsForm(updated);

      if (updated.uid) {
        stopDemoPulseLoop();
        state.demoMode = false;
        state.demoLivePulse = false;
        state.demoDataset = null;
        clearRankCaches({ resetLimits: updated.uid !== prevUid });
      }

      syncBannersAndDemoControls();
      if (adminPwChanged) {
        showToast("配置已保存 · 已更新管理员密码，下次配置时需验证密码");
      } else {
        showToast(
          updated.uid
            ? `配置已保存 · 已绑定账号 ${updated.uid}`
            : "配置已保存"
        );
      }

      closeSettingsModal();
      await loadDashboard({ animateCharts: true });

      if (updated.uid && updated.uid !== prevUid && updated.enabled) {
        setTimeout(() => {
          if (!state.demoMode) loadDashboard({ animateCharts: false });
        }, 2800);
        setTimeout(() => {
          if (!state.demoMode) loadDashboard({ animateCharts: false });
        }, 7500);
      }
    } catch (err) {
      showToast(`保存配置失败: ${err.message}`, "err");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  });

  // Resize Sync for Sliding Rails
  let resizeRaf = 0;
  window.addEventListener("resize", () => {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => {
      syncSlidingIndicators();
    });
  });
}

/* ---------- Application Boot ---------- */

async function init() {
  applyTheme(getStoredTheme(), false);
  sidebarDrawerCtrl = setupResponsiveSidebar();
  const settingsValidator = wireAccessibleSettingsForm();
  const accountValidator = wireAccessibleAccountForm();
  bindEvents(settingsValidator, accountValidator);

  // Sync sliding indicators & start 1s broadcast clock immediately
  requestAnimationFrame(() => {
    syncSlidingIndicators();
    const overviewPanel = document.getElementById("tab-overview");
    if (overviewPanel) staggerEnter(overviewPanel);
  });
  if (document.fonts && typeof document.fonts.ready?.then === "function") {
    document.fonts.ready.then(() => syncSlidingIndicators()).catch(() => {});
  }

  updateLiveClockAndLaser();
  state.clockTimer = setInterval(updateLiveClockAndLaser, 1000);

  // Fetch initial backend state
  let initialData = null;
  try {
    initialData = await apiFetch(`/api/dashboard?hours=${state.hours}`);
  } catch (err) {
    showToast(`后端连接异常，已启动本地示例数据: ${err.message}`, "warn");
  }

  syncAccountsFromDashboard(initialData);
  if (initialData?.admin_auth) {
    syncAdminAuthState(initialData.admin_auth);
  }
  if (initialData?.settings) {
    populateSettingsForm(initialData.settings);
  }

  if (!state.backendUid) {
    activateDemoMode({ showNotification: false });
  } else if (initialData) {
    renderDashboard(initialData, { animateCharts: true, isLiveTick: false });
  }

  state.pollTimer = setInterval(() => {
    if (!state.demoMode && !document.hidden) {
      loadDashboard({ animateCharts: false });
    }
  }, REAL_POLL_INTERVAL_MS);
}

init();
