/**
 * BiliPulse // SIGNAL STUDIO — Dual-Channel Oscilloscope & Broadcast Spectrum Engine
 * Zero external dependencies. Monotone cubic Hermite splines (Fritsch-Carlson),
 * ResizeObserver crisp re-layout, linked crosshair sync, and sharp 0px / 45deg geometry.
 */

import { prefersReducedMotion } from './motion.js';

/** @type {WeakMap<HTMLElement, object>} */
const containerRegistry = new WeakMap();

/** @type {Map<string, Set<object>>} */
const syncGroups = new Map();

let gradientSeq = 0;

/**
 * Escapes text for safe insertion into SVG/HTML markup.
 * @param {unknown} str
 * @returns {string}
 */
function escapeText(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Formats coordinate numbers concisely to 2 decimal places.
 * @param {number} n
 * @returns {string}
 */
function f2(n) {
  return Number.isFinite(n) ? Number(n.toFixed(2)).toString() : '0';
}

/**
 * Zero-pads a 2-digit integer.
 * @param {number} n
 * @returns {string}
 */
function pad2(n) {
  return String(Math.floor(Math.abs(n))).padStart(2, '0');
}

/**
 * Parses timestamps in Date, Unix seconds/ms, ISO string, or SQLite datetime formats.
 * @param {string | number | Date | null | undefined} ts
 * @returns {number}
 */
function parseTimestampMs(ts) {
  if (ts instanceof Date) {
    return ts.getTime();
  }
  if (typeof ts === 'number') {
    if (!Number.isFinite(ts)) return Number.NaN;
    return ts >= 1e9 && ts < 1e11 ? ts * 1000 : ts;
  }
  if (typeof ts === 'string') {
    const trimmed = ts.trim();
    if (!trimmed) return Number.NaN;
    if (/^-?\d+(\.\d+)?$/.test(trimmed)) {
      const num = Number(trimmed);
      return num >= 1e9 && num < 1e11 ? num * 1000 : num;
    }
    let parsed = Date.parse(trimmed);
    if (Number.isNaN(parsed) && trimmed.includes(' ')) {
      parsed = Date.parse(trimmed.replace(' ', 'T'));
    }
    return parsed;
  }
  return Number.NaN;
}

/**
 * Formats X-axis time ticks: HH:mm for <=24h span, MM-DD HH:mm for multi-day span.
 * @param {number} tsMs
 * @param {boolean} isMultiDay
 * @param {string} [fallbackLabel]
 * @returns {string}
 */
function formatAxisTime(tsMs, isMultiDay, fallbackLabel = '') {
  if (!Number.isFinite(tsMs) || tsMs < 86400000) {
    return fallbackLabel || '--:--';
  }
  const d = new Date(tsMs);
  if (Number.isNaN(d.getTime())) {
    return fallbackLabel || '--:--';
  }
  const hhmm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  if (!isMultiDay) return hhmm;
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hhmm}`;
}

/**
 * Formats full timestamp for the floating oscilloscope HUD readout.
 * @param {number} tsMs
 * @param {unknown} rawTs
 * @returns {string}
 */
function formatHudTimestamp(tsMs, rawTs) {
  if (Number.isFinite(tsMs) && tsMs >= 86400000) {
    const d = new Date(tsMs);
    if (!Number.isNaN(d.getTime())) {
      return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    }
  }
  if (rawTs != null && String(rawTs).trim() !== '') {
    return String(rawTs);
  }
  return '--:--:--';
}

/**
 * Normalizes caller point array and guarantees chronological left-to-right order.
 * @param {Array<object>} points
 */
function normalizePoints(points) {
  if (!Array.isArray(points) || points.length === 0) return [];

  let allValidTime = true;
  const mapped = points.map((item, idx) => {
    const rawPoint = item && typeof item === 'object' ? item : { value: item };
    const rawTs =
      rawPoint.ts ?? rawPoint.collected_at_ts ?? rawPoint.collected_at ?? idx;
    const tsMs = parseTimestampMs(rawTs);
    if (!Number.isFinite(tsMs)) {
      allValidTime = false;
    }
    const numVal = Number(rawPoint.value);
    const value = Number.isFinite(numVal) ? numVal : 0;
    return {
      rawPoint,
      ts: rawPoint.ts ?? rawTs,
      tsMs: Number.isFinite(tsMs) ? tsMs : idx * 60000,
      hasValidTime: Number.isFinite(tsMs),
      value,
      raw: rawPoint.raw !== undefined ? String(rawPoint.raw) : undefined,
      hudTime:
        rawPoint.hudTime !== undefined ? String(rawPoint.hudTime) : undefined,
    };
  });

  if (allValidTime && mapped.length > 1) {
    let needsSort = false;
    for (let i = 1; i < mapped.length; i += 1) {
      if (mapped[i].tsMs < mapped[i - 1].tsMs) {
        needsSort = true;
        break;
      }
    }
    if (needsSort) {
      mapped.sort((a, b) => a.tsMs - b.tsMs);
    }
  }

  return mapped;
}

/**
 * Computes a monotone cubic Hermite spline SVG path (Fritsch-Carlson) so curves
 * stay silky smooth and never overshoot real sample extrema.
 *
 * @param {number[]} xs Strictly increasing X coordinates
 * @param {number[]} ys Corresponding Y coordinates
 * @returns {string}
 */
function buildMonotoneHermitePath(xs, ys) {
  const n = xs.length;
  if (n === 0) return '';
  if (n === 1) return `M ${f2(xs[0])} ${f2(ys[0])}`;
  if (n === 2) return `M ${f2(xs[0])} ${f2(ys[0])} L ${f2(xs[1])} ${f2(ys[1])}`;

  const dxs = new Array(n - 1);
  const slopes = new Array(n - 1);
  for (let i = 0; i < n - 1; i += 1) {
    const dx = Math.max(1e-4, xs[i + 1] - xs[i]);
    dxs[i] = dx;
    slopes[i] = (ys[i + 1] - ys[i]) / dx;
  }

  const tangents = new Array(n);
  tangents[0] = slopes[0];
  tangents[n - 1] = slopes[n - 2];

  for (let i = 1; i < n - 1; i += 1) {
    const s0 = slopes[i - 1];
    const s1 = slopes[i];
    if (s0 * s1 <= 0) {
      tangents[i] = 0;
    } else {
      const dx0 = dxs[i - 1];
      const dx1 = dxs[i];
      const sum = dx0 + dx1;
      tangents[i] = (3 * sum) / ((sum + dx1) / s0 + (sum + dx0) / s1);
    }
  }

  for (let i = 0; i < n - 1; i += 1) {
    const slope = slopes[i];
    if (Math.abs(slope) < 1e-9) {
      tangents[i] = 0;
      tangents[i + 1] = 0;
    } else {
      const alpha = tangents[i] / slope;
      const beta = tangents[i + 1] / slope;
      const hypot = Math.hypot(alpha, beta);
      if (hypot > 3) {
        const tau = 3 / hypot;
        tangents[i] = tau * alpha * slope;
        tangents[i + 1] = tau * beta * slope;
      }
    }
  }

  let path = `M ${f2(xs[0])} ${f2(ys[0])}`;
  for (let i = 0; i < n - 1; i += 1) {
    const dx = dxs[i];
    const cp1x = xs[i] + dx / 3;
    const cp1y = ys[i] + (tangents[i] * dx) / 3;
    const cp2x = xs[i + 1] - dx / 3;
    const cp2y = ys[i + 1] - (tangents[i + 1] * dx) / 3;
    path += ` C ${f2(cp1x)} ${f2(cp1y)}, ${f2(cp2x)} ${f2(cp2y)}, ${f2(xs[i + 1])} ${f2(ys[i + 1])}`;
  }

  return path;
}

/**
 * Binary search for nearest coordinate value in a sorted numeric array.
 * @param {number[]} arr
 * @param {number} target
 * @returns {number}
 */
function findNearestInSorted(arr, target) {
  const n = arr.length;
  if (n <= 1) return 0;
  if (target <= arr[0]) return 0;
  if (target >= arr[n - 1]) return n - 1;

  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= target) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  return Math.abs(arr[lo] - target) <= Math.abs(arr[hi] - target) ? lo : hi;
}

/**
 * Finds matching index in a peer chart by timestamp, falling back to proportional index.
 * @param {Array<{tsMs: number, hasValidTime: boolean}>} peerPoints
 * @param {number} targetTsMs
 * @param {number} sourceRatio
 * @returns {number}
 */
function findPeerSyncIndex(peerPoints, targetTsMs, sourceRatio) {
  const n = peerPoints.length;
  if (n <= 1) return 0;

  if (
    Number.isFinite(targetTsMs) &&
    peerPoints[0].hasValidTime &&
    peerPoints[n - 1].hasValidTime &&
    peerPoints[n - 1].tsMs > peerPoints[0].tsMs
  ) {
    let lo = 0;
    let hi = n - 1;
    if (targetTsMs <= peerPoints[0].tsMs) return 0;
    if (targetTsMs >= peerPoints[n - 1].tsMs) return n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (peerPoints[mid].tsMs <= targetTsMs) {
        lo = mid;
      } else {
        hi = mid;
      }
    }
    return Math.abs(peerPoints[lo].tsMs - targetTsMs) <=
      Math.abs(peerPoints[hi].tsMs - targetTsMs)
      ? lo
      : hi;
  }

  return Math.max(0, Math.min(n - 1, Math.round(sourceRatio * (n - 1))));
}

/**
 * Tears down an internal chart instance cleanly.
 * @param {object} inst
 */
function disposeInstance(inst) {
  if (!inst || inst.disposed) return;
  inst.disposed = true;

  if (inst.resizeObserver) {
    inst.resizeObserver.disconnect();
    inst.resizeObserver = null;
  }
  if (inst.resizeRaf) {
    cancelAnimationFrame(inst.resizeRaf);
    inst.resizeRaf = 0;
  }
  if (inst.animTimer) {
    clearTimeout(inst.animTimer);
    inst.animTimer = 0;
  }
  if (inst.syncGroup && syncGroups.has(inst.syncGroup)) {
    const groupSet = syncGroups.get(inst.syncGroup);
    groupSet.delete(inst);
    if (groupSet.size === 0) {
      syncGroups.delete(inst.syncGroup);
    }
  }
}

/**
 * Disconnects ResizeObservers and removes all chart instances belonging to syncGroup.
 * If syncGroup is omitted/null, clears all registered sync groups cleanly.
 *
 * @param {string | null} [syncGroup]
 */
export function clearChartsInGroup(syncGroup) {
  if (syncGroup == null) {
    for (const groupSet of syncGroups.values()) {
      for (const inst of Array.from(groupSet)) {
        if (inst.container) {
          containerRegistry.delete(inst.container);
        }
        disposeInstance(inst);
      }
    }
    syncGroups.clear();
    return;
  }

  const key = String(syncGroup);
  const groupSet = syncGroups.get(key);
  if (!groupSet) return;

  for (const inst of Array.from(groupSet)) {
    if (inst.container) {
      containerRegistry.delete(inst.container);
    }
    disposeInstance(inst);
  }
  syncGroups.delete(key);
}

/**
 * Renders an interactive Swiss-broadcast oscilloscope SVG telemetry chart inside container.
 *
 * @param {HTMLElement | null} container
 * @param {Array<{ ts: string | number | Date, value: number, raw?: string }>} points
 * @param {object} [options]
 * @param {string} [options.channelColor='var(--sig-cyan)']
 * @param {string} [options.fillId='']
 * @param {string} [options.label='数值']
 * @param {(v: number) => string} [options.formatValue]
 * @param {boolean} [options.animate=true]
 * @param {string | null} [options.syncGroup=null]
 * @param {((payload: object | null) => void) | null} [options.onHover=null]
 * @param {string} [options.emptyText='NO SIGNAL // 暂无采样波形']
 */
export function renderTelemetryChart(container, points, options = {}) {
  if (!container) return;

  const existing = containerRegistry.get(container);
  if (existing) {
    disposeInstance(existing);
    containerRegistry.delete(container);
  }

  const {
    channelColor = 'var(--sig-cyan)',
    fillId = '',
    label = '数值',
    formatValue = (v) => Math.round(v).toLocaleString('zh-CN'),
    animate = true,
    syncGroup = null,
    onHover = null,
    emptyText = '暂无采样波形',
  } = options;

  if (
    typeof window !== 'undefined' &&
    window.getComputedStyle(container).position === 'static'
  ) {
    container.style.position = 'relative';
  }

  const normalized = normalizePoints(points);
  const gradUid = fillId
    ? `${String(fillId).replace(/[^a-zA-Z0-9_-]/g, '')}-${++gradientSeq}`
    : `scope-grad-${++gradientSeq}`;

  const instance = {
    container,
    points: normalized,
    channelColor,
    gradUid,
    label,
    formatValue,
    syncGroup: syncGroup ? String(syncGroup) : null,
    onHover: typeof onHover === 'function' ? onHover : null,
    emptyText,
    width: 0,
    height: 0,
    xs: [],
    ys: [],
    rawMin: 0,
    rawMax: 0,
    plotX0: 0,
    plotX1: 0,
    plotY0: 0,
    plotY1: 0,
    resizeObserver: null,
    resizeRaf: 0,
    animTimer: 0,
    disposed: false,
    activateIndex: () => {},
    clearHover: () => {},
  };

  containerRegistry.set(container, instance);
  if (instance.syncGroup) {
    if (!syncGroups.has(instance.syncGroup)) {
      syncGroups.set(instance.syncGroup, new Set());
    }
    syncGroups.get(instance.syncGroup).add(instance);
  }

  /**
   * Builds/rebuilds the SVG + HUD inside container for the current pixel box.
   * @param {boolean} shouldAnimate
   */
  const draw = (shouldAnimate) => {
    if (instance.disposed) return;

    if (instance.animTimer) {
      clearTimeout(instance.animTimer);
      instance.animTimer = 0;
    }

    const rect = container.getBoundingClientRect();
    const cs =
      typeof window !== 'undefined'
        ? window.getComputedStyle(container)
        : null;
    const cPadL = cs ? parseFloat(cs.paddingLeft) || 0 : 0;
    const cPadR = cs ? parseFloat(cs.paddingRight) || 0 : 0;
    const cPadT = cs ? parseFloat(cs.paddingTop) || 0 : 0;
    const cPadB = cs ? parseFloat(cs.paddingBottom) || 0 : 0;
    const rawW =
      (rect.width || container.clientWidth || 560) - cPadL - cPadR;
    const rawH =
      (rect.height || container.clientHeight || 236) - cPadT - cPadB;
    const width = Math.max(220, Math.round(rawW));
    const height = Math.max(150, Math.round(rawH));
    const boxW = Math.round(width + cPadL + cPadR);
    const boxH = Math.round(height + cPadT + cPadB);
    instance.width = width;
    instance.height = height;

    // Standby empty state: precision oscilloscope graticule + chamfered status plaque
    if (normalized.length === 0) {
      const pL = 18;
      const pR = 18;
      const pT = 18;
      const pB = 22;
      const gX0 = pL;
      const gX1 = width - pR;
      const gY0 = pT;
      const gY1 = height - pB;
      const gW = Math.max(40, gX1 - gX0);
      const gH = Math.max(40, gY1 - gY0);
      const midY = gY0 + gH / 2;
      const midX = gX0 + gW / 2;

      let gridLines = '';
      for (let i = 0; i <= 4; i += 1) {
        const gy = gY0 + (gH * i) / 4;
        gridLines += `<line class="scope-grid-line" x1="${f2(gX0)}" y1="${f2(gy)}" x2="${f2(gX1)}" y2="${f2(gy)}" stroke="var(--line-grid)" stroke-width="1" />`;
      }
      for (let i = 0; i <= 6; i += 1) {
        const gx = gX0 + (gW * i) / 6;
        gridLines += `<line class="scope-grid-line" x1="${f2(gx)}" y1="${f2(gY0)}" x2="${f2(gx)}" y2="${f2(gY1)}" stroke="var(--line-grid)" stroke-width="1" opacity="0.7" />`;
      }

      const boxW = Math.min(gW - 24, Math.max(200, emptyText.length * 11 + 36));
      const boxH = 34;
      const bx0 = midX - boxW / 2;
      const by0 = midY - boxH / 2;
      const bx1 = midX + boxW / 2;
      const by1 = midY + boxH / 2;
      const c = 7;
      const chamferPts = `${f2(bx0)},${f2(by0)} ${f2(bx1 - c)},${f2(by0)} ${f2(bx1)},${f2(by0 + c)} ${f2(bx1)},${f2(by1)} ${f2(bx0 + c)},${f2(by1)} ${f2(bx0)},${f2(by1 - c)}`;

      container.innerHTML = `
        <svg class="scope-svg scope-svg-empty" viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="${escapeText(emptyText)}">
          ${gridLines}
          <line x1="${f2(gX0)}" y1="${f2(midY)}" x2="${f2(gX1)}" y2="${f2(midY)}" stroke="${escapeText(channelColor)}" stroke-width="1" stroke-dasharray="6 4" opacity="0.45" />
          <polygon points="${chamferPts}" fill="var(--bg-elevated)" stroke="var(--line-strong)" stroke-width="1" />
          <text x="${f2(midX)}" y="${f2(midY + 4)}" text-anchor="middle" fill="var(--text-muted)" font-family="var(--font-sans, sans-serif)" font-size="11" letter-spacing="0.08em">${escapeText(emptyText)}</text>
        </svg>
      `;
      instance.activateIndex = () => {};
      instance.clearHover = () => {
        if (instance.onHover) instance.onHover(null);
      };
      return;
    }

    // Compute value bounds & 4 tabular Y-axis ticks
    let rawMin = Infinity;
    let rawMax = -Infinity;
    for (let i = 0; i < normalized.length; i += 1) {
      const v = normalized[i].value;
      if (v < rawMin) rawMin = v;
      if (v > rawMax) rawMax = v;
    }
    instance.rawMin = rawMin;
    instance.rawMax = rawMax;

    let yMin = rawMin;
    let yMax = rawMax;
    if (rawMax === rawMin) {
      const pad =
        rawMin === 0 ? 9 : Math.max(3, Math.ceil(Math.abs(rawMin) * 0.02));
      yMin = rawMin >= 0 ? Math.max(0, rawMin - pad) : rawMin - pad;
      yMax = rawMax + pad;
      if (yMax <= yMin) yMax = yMin + 9;
    } else {
      const span = rawMax - rawMin;
      const pad = span * 0.12;
      yMin =
        rawMin >= 0 && rawMin - pad < 0
          ? 0
          : rawMin - pad;
      yMax = rawMax + pad;
    }
    if (yMax - yMin < 3) {
      yMin = rawMin >= 0 ? Math.max(0, Math.floor(yMin)) : Math.floor(yMin);
      yMax = Math.max(Math.ceil(yMax), yMin + 3);
    }

    const yTicks = [];
    let maxTickLen = 4;
    for (let i = 0; i < 4; i += 1) {
      const ratio = i / 3;
      const val = yMin + (yMax - yMin) * ratio;
      const text = formatValue(val);
      if (text.length > maxTickLen) maxTickLen = text.length;
      yTicks.push({ ratio, val, text });
    }

    const padLeft = Math.min(
      88,
      Math.max(48, Math.round(maxTickLen * 6.8 + 14))
    );
    const padRight = 18;
    const padTop = 26;
    const padBottom = 26;

    const plotX0 = padLeft;
    const plotX1 = Math.max(plotX0 + 48, width - padRight);
    const plotY0 = padTop;
    const plotY1 = Math.max(plotY0 + 48, height - padBottom);
    const plotW = plotX1 - plotX0;
    const plotH = plotY1 - plotY0;

    instance.plotX0 = plotX0;
    instance.plotX1 = plotX1;
    instance.plotY0 = plotY0;
    instance.plotY1 = plotY1;

    const valueToY = (val) => {
      const ratio = (val - yMin) / (yMax - yMin || 1);
      return plotY1 - Math.max(0, Math.min(1, ratio)) * plotH;
    };

    const n = normalized.length;
    const tMin = normalized[0].tsMs;
    const tMax = normalized[n - 1].tsMs;
    const timeSpanMs = Math.max(0, tMax - tMin);
    const isMultiDay = timeSpanMs > 24 * 3600 * 1000 + 60 * 1000;

    const xs = new Array(n);
    const ys = new Array(n);

    if (n === 1) {
      xs[0] = plotX0 + plotW / 2;
      ys[0] = valueToY(normalized[0].value);
    } else {
      const useTimeScale =
        normalized[0].hasValidTime &&
        normalized[n - 1].hasValidTime &&
        timeSpanMs > 0;
      for (let i = 0; i < n; i += 1) {
        const rawX = useTimeScale
          ? plotX0 + ((normalized[i].tsMs - tMin) / timeSpanMs) * plotW
          : plotX0 + (i / (n - 1)) * plotW;
        // Enforce strict monotonic X (+0.02px epsilon) so vertical segments never divide by 0
        xs[i] = i > 0 ? Math.max(rawX, xs[i - 1] + 0.02) : rawX;
        ys[i] = valueToY(normalized[i].value);
      }
      // Re-clamp right edge if epsilon nudged past plotX1
      if (xs[n - 1] > plotX1) {
        const scale = plotW / (xs[n - 1] - xs[0] || 1);
        const xStart = xs[0];
        for (let i = 0; i < n; i += 1) {
          xs[i] = plotX0 + (xs[i] - xStart) * scale;
        }
      }
    }

    instance.xs = xs;
    instance.ys = ys;

    // Build technical millimeter grid + Y/X tick labels
    let gridSvg = '';
    let axisLabelsSvg = '';

    // Minor horizontal graticules (sub-divisions between the 4 major Y ticks)
    for (let i = 0; i < 3; i += 1) {
      const subRatio = (i + 0.5) / 3;
      const subY = plotY1 - subRatio * plotH;
      gridSvg += `<line class="scope-grid-line" x1="${f2(plotX0)}" y1="${f2(subY)}" x2="${f2(plotX1)}" y2="${f2(subY)}" stroke="var(--line-grid)" stroke-width="1" stroke-dasharray="2 6" opacity="0.45" />`;
    }

    // 4 major horizontal grid lines & tabular Y-axis labels
    for (let i = 0; i < yTicks.length; i += 1) {
      const tk = yTicks[i];
      const gy = plotY1 - tk.ratio * plotH;
      gridSvg += `<line class="scope-grid-line" x1="${f2(plotX0)}" y1="${f2(gy)}" x2="${f2(plotX1)}" y2="${f2(gy)}" stroke="var(--line-grid)" stroke-width="1" />`;
      gridSvg += `<line x1="${f2(plotX0 - 4)}" y1="${f2(gy)}" x2="${f2(plotX0)}" y2="${f2(gy)}" stroke="var(--line-strong)" stroke-width="1" />`;
      axisLabelsSvg += `<text x="${f2(plotX0 - 7)}" y="${f2(gy + 3.5)}" text-anchor="end" fill="var(--text-muted)" font-family="var(--font-sans, sans-serif)" font-size="10" style="font-variant-numeric: tabular-nums;">${escapeText(tk.text)}</text>`;
    }

    // X-axis vertical grid lines & time tick labels
    if (n === 1) {
      const cx = xs[0];
      const tLabel = formatAxisTime(
        normalized[0].tsMs,
        false,
        String(normalized[0].ts ?? '')
      );
      gridSvg += `<line class="scope-grid-line" x1="${f2(cx)}" y1="${f2(plotY0)}" x2="${f2(cx)}" y2="${f2(plotY1)}" stroke="var(--line-grid)" stroke-width="1" />`;
      axisLabelsSvg += `<text x="${f2(cx)}" y="${f2(plotY1 + 17)}" text-anchor="middle" fill="var(--text-muted)" font-family="var(--font-sans, sans-serif)" font-size="10" style="font-variant-numeric: tabular-nums;">${escapeText(tLabel)}</text>`;
    } else {
      const xTickCount = isMultiDay
        ? Math.min(4, Math.max(2, Math.floor(plotW / 135) + 1))
        : Math.min(5, Math.max(3, Math.floor(plotW / 100) + 1));

      for (let i = 0; i < xTickCount; i += 1) {
        const ratio = xTickCount === 1 ? 0.5 : i / (xTickCount - 1);
        const gx = plotX0 + ratio * plotW;
        const sampleIdx = Math.round(ratio * (n - 1));
        const tickTs =
          timeSpanMs > 0
            ? tMin + ratio * timeSpanMs
            : normalized[sampleIdx].tsMs;
        const tLabel = formatAxisTime(
          tickTs,
          isMultiDay,
          `#${sampleIdx + 1}`
        );
        const anchor =
          i === 0 ? 'start' : i === xTickCount - 1 ? 'end' : 'middle';

        gridSvg += `<line class="scope-grid-line" x1="${f2(gx)}" y1="${f2(plotY0)}" x2="${f2(gx)}" y2="${f2(plotY1)}" stroke="var(--line-grid)" stroke-width="1" opacity="0.75" />`;
        gridSvg += `<line x1="${f2(gx)}" y1="${f2(plotY1)}" x2="${f2(gx)}" y2="${f2(plotY1 + 4)}" stroke="var(--line-strong)" stroke-width="1" />`;
        axisLabelsSvg += `<text x="${f2(gx)}" y="${f2(plotY1 + 17)}" text-anchor="${anchor}" fill="var(--text-muted)" font-family="var(--font-sans, sans-serif)" font-size="10" style="font-variant-numeric: tabular-nums;">${escapeText(tLabel)}</text>`;
      }
    }

    // Plot boundary hairlines (left & bottom strong axes, plus 0-reference when spanning negative/positive)
    if (yMin < 0 && yMax > 0) {
      const zeroY = valueToY(0);
      gridSvg += `<line x1="${f2(plotX0)}" y1="${f2(zeroY)}" x2="${f2(plotX1)}" y2="${f2(zeroY)}" stroke="var(--line-strong)" stroke-width="1" stroke-dasharray="4 3" opacity="0.8" />`;
    }
    gridSvg += `<line x1="${f2(plotX0)}" y1="${f2(plotY0)}" x2="${f2(plotX0)}" y2="${f2(plotY1)}" stroke="var(--line-strong)" stroke-width="1" />`;
    gridSvg += `<line x1="${f2(plotX0)}" y1="${f2(plotY1)}" x2="${f2(plotX1)}" y2="${f2(plotY1)}" stroke="var(--line-strong)" stroke-width="1" />`;

    // Waveform path & area path
    let curvePath = '';
    let areaPath = '';
    if (n === 1) {
      const refY = ys[0];
      curvePath = `M ${f2(plotX0)} ${f2(refY)} L ${f2(plotX1)} ${f2(refY)}`;
      areaPath = `${curvePath} L ${f2(plotX1)} ${f2(plotY1)} L ${f2(plotX0)} ${f2(plotY1)} Z`;
    } else if (n === 2) {
      curvePath = `M ${f2(xs[0])} ${f2(ys[0])} L ${f2(xs[1])} ${f2(ys[1])}`;
      areaPath = `${curvePath} L ${f2(xs[1])} ${f2(plotY1)} L ${f2(xs[0])} ${f2(plotY1)} Z`;
    } else {
      curvePath = buildMonotoneHermitePath(xs, ys);
      areaPath = `${curvePath} L ${f2(xs[n - 1])} ${f2(plotY1)} L ${f2(xs[0])} ${f2(plotY1)} Z`;
    }

    // Peak (MAX) and Trough (MIN) sharp technical callout pins
    let pinsSvg = '';
    if (n >= 2) {
      let maxIdx = 0;
      let minIdx = 0;
      for (let i = 0; i < n; i += 1) {
        if (normalized[i].value >= normalized[maxIdx].value) maxIdx = i;
        if (normalized[i].value < normalized[minIdx].value) minIdx = i;
      }

      const showMin = n >= 4 && rawMin < rawMax && minIdx !== maxIdx;

      const buildPin = (idx, tag, isMax) => {
        const px = xs[idx];
        const py = ys[idx];
        const valStr = `${tag} ${formatValue(normalized[idx].value)}`;
        const goLeft = px > plotX1 - Math.max(96, valStr.length * 8.5);
        const dir = goLeft ? -1 : 1;
        const anchor = goLeft ? 'end' : 'start';

        let elbowY;
        let textY;
        if (isMax) {
          const tooHigh = py - 14 < 10;
          elbowY = tooHigh ? py + 11 : py - 10;
          textY = elbowY + 3;
        } else {
          const tooLow = py + 15 > plotY1 - 3;
          elbowY = tooLow ? py - 10 : py + 11;
          textY = elbowY + 3;
        }

        const pinColor = isMax ? channelColor : 'var(--text-secondary)';
        const diamond = `${f2(px)},${f2(py - 3)} ${f2(px + 3)},${f2(py)} ${f2(px)},${f2(py + 3)} ${f2(px - 3)},${f2(py)}`;
        return `
          <g class="scope-pin ${isMax ? 'scope-pin-max' : 'scope-pin-min'}">
            <polyline points="${f2(px)},${f2(py)} ${f2(px)},${f2(elbowY)} ${f2(px + dir * 7)},${f2(elbowY)}" fill="none" stroke="${escapeText(pinColor)}" stroke-width="1" />
            <polygon points="${diamond}" fill="var(--bg-elevated)" stroke="${escapeText(pinColor)}" stroke-width="1.2" />
            <text x="${f2(px + dir * 10)}" y="${f2(textY)}" text-anchor="${anchor}" fill="${escapeText(pinColor)}" font-family="var(--font-sans, sans-serif)" font-size="10" font-weight="500" style="font-variant-numeric: tabular-nums;">${escapeText(valStr)}</text>
          </g>
        `;
      };

      pinsSvg += buildPin(maxIdx, '最高', true);
      if (showMin) {
        pinsSvg += buildPin(minIdx, '最低', false);
      }
    }

    const lastX = xs[n - 1];
    const lastY = ys[n - 1];

    container.innerHTML = `
      <svg class="scope-svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" style="display: block; touch-action: pan-y; user-select: none;">
        <defs>
          <linearGradient id="${escapeText(gradUid)}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${escapeText(channelColor)}" stop-opacity="0.32" />
            <stop offset="68%" stop-color="${escapeText(channelColor)}" stop-opacity="0.07" />
            <stop offset="100%" stop-color="${escapeText(channelColor)}" stop-opacity="0" />
          </linearGradient>
        </defs>
        <g class="scope-grid">${gridSvg}</g>
        <path class="scope-area" d="${areaPath}" fill="url(#${escapeText(gradUid)})" />
        <path class="scope-curve" d="${curvePath}" fill="none" stroke="${escapeText(channelColor)}" stroke-width="2" stroke-linecap="butt" stroke-linejoin="round" />
        <g class="scope-pins">${pinsSvg}</g>
        <g class="scope-axes">${axisLabelsSvg}</g>
        <g class="scope-live-endpoint">
          <circle class="scope-beacon-ring" cx="${f2(lastX)}" cy="${f2(lastY)}" r="7.5" fill="none" stroke="${escapeText(channelColor)}" stroke-width="1.4" style="transform-box: fill-box; transform-origin: center;" />
          <circle class="scope-beacon" cx="${f2(lastX)}" cy="${f2(lastY)}" r="3.5" fill="${escapeText(channelColor)}" />
        </g>
        <g class="scope-crosshair" style="opacity: 0; pointer-events: none;">
          <line class="scope-crosshair-v" x1="0" y1="${f2(plotY0)}" x2="0" y2="${f2(plotY1)}" stroke="${escapeText(channelColor)}" stroke-width="1" stroke-dasharray="3 3" />
          <line class="scope-crosshair-h" x1="${f2(plotX0)}" y1="0" x2="${f2(plotX1)}" y2="0" stroke="var(--line-strong)" stroke-width="1" stroke-dasharray="2 4" />
          <circle class="scope-crosshair-halo" cx="0" cy="0" r="7" fill="none" stroke="${escapeText(channelColor)}" stroke-width="1.3" opacity="0.85" />
          <circle class="scope-crosshair-dot" cx="0" cy="0" r="3.2" fill="var(--bg-elevated)" stroke="${escapeText(channelColor)}" stroke-width="2" />
        </g>
      </svg>
      <div class="scope-hud" aria-hidden="true" style="position: absolute; top: 0; left: 0; opacity: 0; visibility: hidden; pointer-events: none; z-index: 12; border-radius: 0;">
        <div class="scope-hud-time mono"></div>
        <div class="scope-hud-row">
          <span class="scope-hud-label"></span>
          <strong class="scope-hud-value mono"></strong>
        </div>
      </div>
    `;

    const svgEl = container.querySelector('.scope-svg');
    const curveEl = container.querySelector('.scope-curve');
    const areaEl = container.querySelector('.scope-area');
    const crosshairGroup = container.querySelector('.scope-crosshair');
    const vLine = container.querySelector('.scope-crosshair-v');
    const hLine = container.querySelector('.scope-crosshair-h');
    const haloEl = container.querySelector('.scope-crosshair-halo');
    const dotEl = container.querySelector('.scope-crosshair-dot');
    const hudEl = container.querySelector('.scope-hud');
    const hudTimeEl = container.querySelector('.scope-hud-time');
    const hudLabelEl = container.querySelector('.scope-hud-label');
    const hudValueEl = container.querySelector('.scope-hud-value');

    // Animated oscilloscope trace draw-in
    if (shouldAnimate && !prefersReducedMotion() && curveEl && areaEl) {
      let pathLen = 0;
      try {
        pathLen = curveEl.getTotalLength();
      } catch (_err) {
        pathLen = 0;
      }

      if (pathLen > 0) {
        curveEl.style.transition = 'none';
        curveEl.style.strokeDasharray = `${pathLen}`;
        curveEl.style.strokeDashoffset = `${pathLen}`;
        areaEl.style.transition = 'none';
        areaEl.style.opacity = '0';

        // Commit initial hidden stroke state synchronously before starting transition
        void curveEl.getBoundingClientRect();

        curveEl.style.transition =
          'stroke-dashoffset 850ms cubic-bezier(0.16, 1, 0.3, 1)';
        curveEl.style.strokeDashoffset = '0';
        areaEl.style.transition =
          'opacity 850ms cubic-bezier(0.16, 1, 0.3, 1) 70ms';
        areaEl.style.opacity = '1';

        instance.animTimer = setTimeout(() => {
          if (instance.disposed) return;
          curveEl.style.transition = '';
          curveEl.style.strokeDasharray = '';
          curveEl.style.strokeDashoffset = '';
          areaEl.style.transition = '';
          instance.animTimer = 0;
        }, 920);
      }
    }

    /**
     * Activates crosshair + chamfered HUD at sample index on this chart instance.
     * @param {number} idx
     */
    instance.activateIndex = (idx) => {
      if (instance.disposed || n === 0) return;
      const safeIdx = Math.max(0, Math.min(n - 1, idx));
      const pt = normalized[safeIdx];
      const px = xs[safeIdx];
      const py = ys[safeIdx];
      const deltaFromStart = pt.value - normalized[0].value;

      if (crosshairGroup && vLine && hLine && haloEl && dotEl) {
        vLine.setAttribute('x1', f2(px));
        vLine.setAttribute('x2', f2(px));
        hLine.setAttribute('y1', f2(py));
        hLine.setAttribute('y2', f2(py));
        haloEl.setAttribute('cx', f2(px));
        haloEl.setAttribute('cy', f2(py));
        dotEl.setAttribute('cx', f2(px));
        dotEl.setAttribute('cy', f2(py));
        crosshairGroup.style.opacity = '1';
      }

      if (hudEl && hudTimeEl && hudLabelEl && hudValueEl) {
        const formattedVal = formatValue(pt.value);
        hudTimeEl.textContent =
          pt.hudTime || formatHudTimestamp(pt.tsMs, pt.ts);
        hudLabelEl.textContent = `${label}：`;
        hudValueEl.textContent =
          pt.raw && pt.raw !== formattedVal
            ? `${formattedVal} (${pt.raw})`
            : formattedVal;

        hudEl.classList.add('is-active');
        hudEl.style.visibility = 'visible';
        hudEl.style.opacity = '1';

        const hudW = hudEl.offsetWidth || 164;
        const hudH = hudEl.offsetHeight || 48;
        const absX = cPadL + px;
        const absY = cPadT + py;
        const placeLeft = absX + 14 + hudW > boxW - 8;
        const rawHudX = placeLeft ? absX - hudW - 14 : absX + 14;
        const rawHudY = absY - hudH * 0.5;
        const clampedX = Math.max(6, Math.min(boxW - hudW - 6, rawHudX));
        const clampedY = Math.max(6, Math.min(boxH - hudH - 6, rawHudY));
        hudEl.style.transform = `translate3d(${Math.round(clampedX)}px, ${Math.round(clampedY)}px, 0)`;
      }

      if (instance.onHover) {
        instance.onHover({
          point: {
            ...pt.rawPoint,
            ts: pt.ts,
            tsMs: pt.tsMs,
            value: pt.value,
            raw: pt.raw,
            formattedTime: formatHudTimestamp(pt.tsMs, pt.ts),
            formattedValue: formatValue(pt.value),
          },
          index: safeIdx,
          deltaFromStart,
          min: rawMin,
          max: rawMax,
        });
      }
    };

    /**
     * Hides crosshair + HUD and notifies caller onHover(null).
     */
    instance.clearHover = () => {
      if (crosshairGroup) {
        crosshairGroup.style.opacity = '0';
      }
      if (hudEl) {
        hudEl.classList.remove('is-active');
        hudEl.style.opacity = '0';
        hudEl.style.visibility = 'hidden';
      }
      if (instance.onHover) {
        instance.onHover(null);
      }
    };

    if (svgEl) {
      const handlePointer = (event) => {
        if (instance.disposed || n === 0) return;
        const box = svgEl.getBoundingClientRect();
        if (box.width <= 0) return;

        const svgX = ((event.clientX - box.left) / box.width) * width;
        const nearestIdx = findNearestInSorted(xs, svgX);
        const activePoint = normalized[nearestIdx];
        const sourceRatio = n > 1 ? nearestIdx / (n - 1) : 0;

        if (instance.syncGroup && syncGroups.has(instance.syncGroup)) {
          const peers = syncGroups.get(instance.syncGroup);
          for (const peer of Array.from(peers)) {
            if (!peer.container || !peer.container.isConnected) {
              disposeInstance(peer);
              continue;
            }
            if (peer === instance) {
              peer.activateIndex(nearestIdx);
            } else if (peer.points.length > 0) {
              const peerIdx = findPeerSyncIndex(
                peer.points,
                activePoint.tsMs,
                sourceRatio
              );
              peer.activateIndex(peerIdx);
            }
          }
        } else {
          instance.activateIndex(nearestIdx);
        }
      };

      const handleLeave = () => {
        if (instance.disposed) return;
        if (instance.syncGroup && syncGroups.has(instance.syncGroup)) {
          const peers = syncGroups.get(instance.syncGroup);
          for (const peer of Array.from(peers)) {
            if (!peer.container || !peer.container.isConnected) {
              disposeInstance(peer);
              continue;
            }
            peer.clearHover();
          }
        } else {
          instance.clearHover();
        }
      };

      svgEl.addEventListener('pointermove', handlePointer);
      svgEl.addEventListener('pointerdown', handlePointer);
      svgEl.addEventListener('pointerleave', handleLeave);
      svgEl.addEventListener('pointercancel', handleLeave);
    }
  };

  draw(Boolean(animate));

  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver(() => {
      if (instance.disposed) return;
      if (instance.resizeRaf) {
        cancelAnimationFrame(instance.resizeRaf);
      }
      instance.resizeRaf = requestAnimationFrame(() => {
        instance.resizeRaf = 0;
        if (instance.disposed || !container.isConnected) return;
        const nextRect = container.getBoundingClientRect();
        const cs =
          typeof window !== 'undefined'
            ? window.getComputedStyle(container)
            : null;
        const pH = cs
          ? (parseFloat(cs.paddingLeft) || 0) +
            (parseFloat(cs.paddingRight) || 0)
          : 0;
        const pV = cs
          ? (parseFloat(cs.paddingTop) || 0) +
            (parseFloat(cs.paddingBottom) || 0)
          : 0;
        const nextW = Math.round(
          (nextRect.width || container.clientWidth || 0) - pH
        );
        const nextH = Math.round(
          (nextRect.height || container.clientHeight || 0) - pV
        );
        if (nextW <= 0 || nextH <= 0) return;
        if (
          Math.abs(nextW - instance.width) < 1 &&
          Math.abs(nextH - instance.height) < 1
        ) {
          return;
        }
        draw(false);
      });
    });
    ro.observe(container);
    instance.resizeObserver = ro;
  }
}

/**
 * Downsamples or left-pads numeric samples into a crisp broadcast spectrum bar meter
 * with 0px sharp rectangular geometry, staggered entrance, and brightest live newest bar.
 *
 * @param {HTMLElement | null} container
 * @param {number[]} values
 * @param {object} [options]
 * @param {string} [options.color='var(--sig-cyan)']
 * @param {number} [options.bars=24]
 */
export function renderSparkWave(container, values, options = {}) {
  if (!container) return;

  const { color = 'var(--sig-cyan)', bars = 24 } = options;
  const barCount = Math.max(4, Math.min(64, Math.round(Number(bars) || 24)));
  const clean = Array.isArray(values)
    ? values.map((v) => Number(v)).filter((v) => Number.isFinite(v))
    : [];

  /** @type {Array<{ value: number, active: boolean }>} */
  const slots = new Array(barCount);

  if (clean.length === 0) {
    for (let i = 0; i < barCount; i += 1) {
      slots[i] = { value: 0, active: false };
    }
  } else if (clean.length <= barCount) {
    const padCount = barCount - clean.length;
    for (let i = 0; i < padCount; i += 1) {
      slots[i] = { value: 0, active: false };
    }
    for (let i = 0; i < clean.length; i += 1) {
      slots[padCount + i] = { value: clean[i], active: true };
    }
  } else {
    for (let i = 0; i < barCount; i += 1) {
      if (i === barCount - 1) {
        // Always pin newest rightmost bar to exact latest sample
        slots[i] = { value: clean[clean.length - 1], active: true };
      } else {
        const start = Math.floor((i * clean.length) / barCount);
        const end = Math.max(
          start + 1,
          Math.floor(((i + 1) * clean.length) / barCount)
        );
        let sum = 0;
        for (let k = start; k < end; k += 1) {
          sum += clean[k];
        }
        slots[i] = { value: sum / (end - start), active: true };
      }
    }
  }

  let minVal = Infinity;
  let maxVal = -Infinity;
  for (let i = 0; i < barCount; i += 1) {
    if (!slots[i].active) continue;
    if (slots[i].value < minVal) minVal = slots[i].value;
    if (slots[i].value > maxVal) maxVal = slots[i].value;
  }

  const vw = barCount * 6;
  const vh = 44;
  const maxBarH = vh - 4;
  const stepX = 6;
  const barW = 4;

  const computeSlotMetrics = (slot, idx) => {
    if (!slot.active) {
      return {
        ratio: 0.06,
        opacity: 0.18,
        fill: 'var(--line-strong)',
        isNewest: false,
      };
    }

    let ratio;
    if (maxVal > minVal) {
      const norm = (slot.value - minVal) / (maxVal - minVal);
      ratio = 0.18 + norm * 0.82;
    } else {
      ratio = maxVal === 0 ? 0.12 : 0.58;
    }

    const isNewest = idx === barCount - 1;
    const progress = barCount > 1 ? idx / (barCount - 1) : 1;
    const opacity = isNewest ? 1 : Number((0.3 + progress * 0.56).toFixed(2));

    return {
      ratio: Math.max(0.06, Math.min(1, ratio)),
      opacity,
      fill: color,
      isNewest,
    };
  };

  const reduced = prefersReducedMotion();
  const existingSvg = container.querySelector(
    `svg.spark-svg[data-bars="${barCount}"]`
  );

  // Smooth compositor scaleY update in-place on live ticks (zero DOM flicker)
  if (existingSvg) {
    const barEls = existingSvg.querySelectorAll('rect.spark-bar');
    const capEl = existingSvg.querySelector('rect.spark-live-cap');
    if (barEls.length === barCount) {
      for (let i = 0; i < barCount; i += 1) {
        const m = computeSlotMetrics(slots[i], i);
        const rectEl = barEls[i];
        rectEl.setAttribute('fill', m.fill);
        rectEl.setAttribute('fill-opacity', String(m.opacity));
        rectEl.style.transition = reduced
          ? 'none'
          : 'transform 420ms cubic-bezier(0.16, 1, 0.3, 1), fill-opacity 280ms ease';
        rectEl.style.transform = `scaleY(${m.ratio.toFixed(4)})`;

        if (m.isNewest && capEl) {
          const capY = vh - 1 - maxBarH * m.ratio;
          capEl.setAttribute('y', f2(Math.max(1, capY - 1.5)));
          capEl.setAttribute('fill', color);
          capEl.setAttribute('fill-opacity', slots[i].active ? '1' : '0');
        }
      }
      return;
    }
  }

  // Initial mount: sharp 0px bars with staggered spectrum grow-in
  let rectsMarkup = '';
  let liveCapMarkup = '';

  for (let i = 0; i < barCount; i += 1) {
    const m = computeSlotMetrics(slots[i], i);
    const bx = i * stepX + 1;
    const by = vh - 1 - maxBarH;
    const initialScale = reduced ? m.ratio.toFixed(4) : '0.02';

    rectsMarkup += `<rect class="spark-bar" data-target-scale="${m.ratio.toFixed(4)}" x="${bx}" y="${by}" width="${barW}" height="${maxBarH}" fill="${escapeText(m.fill)}" fill-opacity="${m.opacity}" style="transform-origin: ${bx}px ${vh - 1}px; transform: scaleY(${initialScale});" />`;

    if (i === barCount - 1) {
      const capY = vh - 1 - maxBarH * m.ratio;
      liveCapMarkup = `<rect class="spark-live-cap" x="${bx}" y="${f2(Math.max(1, capY - 1.5))}" width="${barW}" height="1.5" fill="${escapeText(color)}" fill-opacity="${slots[i].active ? '1' : '0'}" />`;
    }
  }

  container.innerHTML = `
    <svg class="spark-svg" data-bars="${barCount}" viewBox="0 0 ${vw} ${vh}" preserveAspectRatio="none" width="100%" height="100%" aria-hidden="true" style="display: block; overflow: visible;">
      <line x1="0" y1="${vh - 0.5}" x2="${vw}" y2="${vh - 0.5}" stroke="var(--line-grid)" stroke-width="1" />
      ${rectsMarkup}
      ${liveCapMarkup}
    </svg>
  `;

  if (!reduced) {
    const mountedBars = container.querySelectorAll('rect.spark-bar');
    // Commit scaleY(0.02) initial state before triggering staggered grow animation
    void container.offsetWidth;
    for (let i = 0; i < mountedBars.length; i += 1) {
      const barEl = mountedBars[i];
      const targetScale = barEl.getAttribute('data-target-scale') || '0.1';
      const delayMs = i * 16;
      barEl.style.transition = `transform 520ms cubic-bezier(0.16, 1, 0.3, 1) ${delayMs}ms`;
      barEl.style.transform = `scaleY(${targetScale})`;
    }
  }
}
