/**
 * BiliPulse // SIGNAL STUDIO — Synthetic Creator Telemetry & Live Pulse Stream
 * Pure offline procedural geometric SVG covers (zero external image requests, zero rounded rects).
 */

const DEMO_UID = "100001001";
const DEMO_ACCOUNT_NAME = "极客信号局";

function sanitizeSvgText(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function buildMotifSvg(motif, c1, c2, idx) {
  const phase = (idx + 1) * 0.65;
  switch (motif) {
    case "scope": {
      let pts1 = "";
      let pts2 = "";
      for (let i = 0; i <= 32; i++) {
        const x = 16 + i * 9;
        const y1 = 86 + Math.sin(i * 0.42 + phase) * 24 + Math.cos(i * 0.19) * 9;
        const y2 = 92 + Math.cos(i * 0.36 + phase) * 15;
        pts1 += `${x.toFixed(1)},${y1.toFixed(1)} `;
        pts2 += `${x.toFixed(1)},${y2.toFixed(1)} `;
      }
      return `
        <polyline points="${pts2.trim()}" fill="none" stroke="${c2}" stroke-width="1.2" stroke-opacity="0.55" />
        <polyline points="${pts1.trim()}" fill="none" stroke="${c1}" stroke-width="2.2" />
        <line x1="214" y1="38" x2="214" y2="132" stroke="${c2}" stroke-width="1" stroke-dasharray="3,3" />
        <circle cx="214" cy="76" r="4" fill="${c1}" />
        <circle cx="214" cy="76" r="10" fill="none" stroke="${c1}" stroke-width="1" stroke-opacity="0.6" />
      `;
    }
    case "cube": {
      return `
        <polygon points="168,36 226,64 168,92 110,64" fill="${c1}" fill-opacity="0.16" stroke="${c1}" stroke-width="1.6" />
        <polygon points="110,64 168,92 168,138 110,110" fill="${c2}" fill-opacity="0.12" stroke="${c2}" stroke-width="1.4" />
        <polygon points="226,64 168,92 168,138 226,110" fill="${c1}" fill-opacity="0.08" stroke="${c1}" stroke-width="1.4" />
        <line x1="168" y1="22" x2="168" y2="150" stroke="${c2}" stroke-width="0.9" stroke-dasharray="2,3" stroke-opacity="0.65" />
        <line x1="86" y1="88" x2="250" y2="88" stroke="${c1}" stroke-width="0.9" stroke-dasharray="2,3" stroke-opacity="0.55" />
      `;
    }
    case "radar": {
      return `
        <circle cx="176" cy="84" r="46" fill="none" stroke="${c2}" stroke-width="1.1" stroke-opacity="0.45" />
        <circle cx="176" cy="84" r="30" fill="none" stroke="${c1}" stroke-width="1.2" stroke-dasharray="4,3" stroke-opacity="0.7" />
        <circle cx="176" cy="84" r="14" fill="${c1}" fill-opacity="0.18" stroke="${c1}" stroke-width="1.5" />
        <line x1="120" y1="84" x2="232" y2="84" stroke="${c2}" stroke-width="0.9" stroke-opacity="0.5" />
        <line x1="176" y1="30" x2="176" y2="138" stroke="${c2}" stroke-width="0.9" stroke-opacity="0.5" />
        <polygon points="176,84 218,56 222,74" fill="${c1}" fill-opacity="0.32" />
        <circle cx="204" cy="66" r="3.2" fill="#FFFFFF" />
      `;
    }
    case "spectrum": {
      let bars = "";
      for (let i = 0; i < 16; i++) {
        const x = 68 + i * 12;
        const h = Math.round(18 + Math.abs(Math.sin(i * 0.62 + phase)) * 62 + (i % 3) * 6);
        const y = 122 - h;
        const fill = i % 4 === 0 ? c1 : c2;
        const op = (0.35 + (i / 16) * 0.55).toFixed(2);
        bars += `<rect x="${x}" y="${y}" width="8" height="${h}" fill="${fill}" fill-opacity="${op}" />`;
      }
      return `
        ${bars}
        <line x1="60" y1="122" x2="266" y2="122" stroke="${c1}" stroke-width="1.4" />
      `;
    }
    case "circuit": {
      return `
        <polyline points="54,56 106,56 128,78 192,78 214,54 268,54" fill="none" stroke="${c2}" stroke-width="1.8" />
        <polyline points="54,106 96,106 118,86 198,86 222,110 268,110" fill="none" stroke="${c1}" stroke-width="1.8" />
        <rect x="48" y="50" width="12" height="12" fill="#0B101B" stroke="${c2}" stroke-width="1.6" />
        <rect x="154" y="72" width="14" height="14" fill="${c1}" fill-opacity="0.28" stroke="${c1}" stroke-width="1.6" />
        <rect x="262" y="48" width="12" height="12" fill="#0B101B" stroke="${c2}" stroke-width="1.6" />
        <rect x="262" y="104" width="12" height="12" fill="${c1}" stroke="${c1}" stroke-width="1.2" />
      `;
    }
    default: {
      return `
        <polygon points="166,34 224,118 108,118" fill="${c1}" fill-opacity="0.14" stroke="${c1}" stroke-width="1.7" />
        <polygon points="186,48 238,118 134,118" fill="${c2}" fill-opacity="0.12" stroke="${c2}" stroke-width="1.3" />
        <line x1="72" y1="92" x2="150" y2="80" stroke="#FFFFFF" stroke-width="1.4" stroke-opacity="0.75" />
        <line x1="184" y1="80" x2="268" y2="62" stroke="${c1}" stroke-width="1.8" />
        <line x1="184" y1="86" x2="268" y2="96" stroke="${c2}" stroke-width="1.8" />
      `;
    }
  }
}

export function buildDemoCoverSvg({
  channel = "频道01",
  tag = "示例",
  headline = "信号监控台",
  c1 = "#FF2A7A",
  c2 = "#00E5FF",
  motif = "scope",
  index = 0,
} = {}) {
  const safeChannel = sanitizeSvgText(channel);
  const safeTag = sanitizeSvgText(tag);
  const safeHeadline = sanitizeSvgText(String(headline).slice(0, 15));
  const motifMarkup = buildMotifSvg(motif, c1, c2, index);

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180">
    <defs>
      <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#070A11"/>
        <stop offset="60%" stop-color="#0D1422"/>
        <stop offset="100%" stop-color="#121C2E"/>
      </linearGradient>
    </defs>
    <rect width="320" height="180" fill="url(#bg)"/>
    <line x1="0" y1="45" x2="320" y2="45" stroke="#1E293B" stroke-width="1"/>
    <line x1="0" y1="90" x2="320" y2="90" stroke="#1E293B" stroke-width="1"/>
    <line x1="0" y1="135" x2="320" y2="135" stroke="#1E293B" stroke-width="1"/>
    <line x1="80" y1="0" x2="80" y2="180" stroke="#1E293B" stroke-width="1"/>
    <line x1="160" y1="0" x2="160" y2="180" stroke="#1E293B" stroke-width="1"/>
    <line x1="240" y1="0" x2="240" y2="180" stroke="#1E293B" stroke-width="1"/>
    <polygon points="0,0 320,0 320,3 0,3" fill="${c1}"/>
    ${motifMarkup}
    <polygon points="198,12 308,12 308,32 300,40 198,40" fill="#090D16" stroke="${c2}" stroke-width="1.2"/>
    <text x="253" y="30" text-anchor="middle" fill="${c2}" font-family="Consolas,monospace" font-size="11" font-weight="700">${safeTag}</text>
    <text x="14" y="28" fill="#94A3B8" font-family="Consolas,monospace" font-size="11" font-weight="700">${safeChannel}</text>
    <rect x="0" y="132" width="320" height="48" fill="#070A11" fill-opacity="0.86"/>
    <rect x="14" y="141" width="3" height="28" fill="${c1}"/>
    <text x="24" y="160" fill="#F8FAFC" font-family="Bahnschrift,PingFang SC,Microsoft YaHei UI,sans-serif" font-size="15" font-weight="700">${safeHeadline}</text>
  </svg>`;

  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export function buildFallbackCoverSvg(seedText = "暂无封面") {
  const safeLabel = sanitizeSvgText(String(seedText || "暂无封面").slice(0, 14));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180">
    <rect width="320" height="180" fill="#0A0E17"/>
    <line x1="0" y1="60" x2="320" y2="60" stroke="#1B2434" stroke-width="1"/>
    <line x1="0" y1="120" x2="320" y2="120" stroke="#1B2434" stroke-width="1"/>
    <line x1="106" y1="0" x2="106" y2="180" stroke="#1B2434" stroke-width="1"/>
    <line x1="214" y1="0" x2="214" y2="180" stroke="#1B2434" stroke-width="1"/>
    <polygon points="132,64 188,64 188,108 180,116 132,116" fill="none" stroke="#00E5FF" stroke-width="1.6"/>
    <polygon points="152,78 172,90 152,102" fill="#FF2A7A"/>
    <text x="160" y="148" text-anchor="middle" fill="#94A3B8" font-family="Consolas,monospace" font-size="12" font-weight="700">${safeLabel}</text>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export function buildDemoAvatarSvg(accent = "#FF2A7A", accent2 = "#00E5FF") {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
    <rect width="64" height="64" fill="#0B101B"/>
    <polygon points="0,0 64,0 64,4 0,4" fill="${accent}"/>
    <polygon points="10,14 54,14 54,48 46,56 10,56" fill="#111A2B" stroke="${accent2}" stroke-width="2"/>
    <polyline points="16,36 24,36 28,24 35,44 40,32 48,32" fill="none" stroke="${accent}" stroke-width="2.6"/>
    <circle cx="48" cy="32" r="2.6" fill="${accent2}"/>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const DEMO_ACCOUNT_PROFILES = [
  {
    uid: DEMO_UID,
    name: DEMO_ACCOUNT_NAME,
    targetFollowers: 286428,
    onlineScale: 1.0,
    phaseShift: 0,
    accent: "#FF2A7A",
    accent2: "#00E5FF",
    videoSlice: [0, 12],
  },
  {
    uid: "100001002",
    name: "量子视觉实验室",
    targetFollowers: 142890,
    onlineScale: 0.62,
    phaseShift: 1.85,
    accent: "#00E5FF",
    accent2: "#9D5CFF",
    videoSlice: [1, 9],
  },
  {
    uid: "100001003",
    name: "声波电台 FM",
    targetFollowers: 64310,
    onlineScale: 0.38,
    phaseShift: 3.4,
    accent: "#00E699",
    accent2: "#FFB020",
    videoSlice: [3, 10],
  },
];

const DEMO_VIDEO_SPECS = [
  {
    bvid: "BV1Qx4y1a7K2",
    title: "手搓静音双卡人工智能工作站：风道流体仿真与满载热成像全记录",
    headline: "双卡工作站流体热成像",
    play_count: 1846200,
    cid_count: 3,
    duration: 1348,
    pub_days: 2,
    weight: 0.235,
    colors: ["#FF2A7A", "#00E5FF"],
    tag: "硬件算力",
    motif: "cube",
  },
  {
    bvid: "BV1mZ421B7p9",
    title: "零依赖手写 60帧 示波器级矢量遥测引擎：单调样条与像素对齐",
    headline: "60帧 矢量示波器引擎",
    play_count: 1294500,
    cid_count: 2,
    duration: 986,
    pub_days: 6,
    weight: 0.175,
    colors: ["#00E5FF", "#7C5CFF"],
    tag: "矢量波形",
    motif: "scope",
  },
  {
    bvid: "BV1rH4y1M7vL",
    title: "轻量接口与本地数据库压测实录：单机每秒万级时序写入调优",
    headline: "本地数据库时序压测调优",
    play_count: 968400,
    cid_count: 2,
    duration: 1112,
    pub_days: 11,
    weight: 0.135,
    colors: ["#00E5FF", "#10B981"],
    tag: "时序存储",
    motif: "circuit",
  },
  {
    bvid: "BV1wT421Q7nE",
    title: "告别圆角矩形审美疲劳！瑞士国际主义网格系统与工业界面实战",
    headline: "瑞士网格与工业界面",
    play_count: 842100,
    cid_count: 1,
    duration: 824,
    pub_days: 16,
    weight: 0.115,
    colors: ["#FF2A7A", "#F59E0B"],
    tag: "网格设计",
    motif: "prism",
  },
  {
    bvid: "BV1cJ4m1W7qD",
    title: "全闪万兆家庭数据中心：低功耗机柜布线与监控总线搭建",
    headline: "全闪万兆家庭数据中心",
    play_count: 715800,
    cid_count: 3,
    duration: 1520,
    pub_days: 23,
    weight: 0.085,
    colors: ["#7C5CFF", "#00E5FF"],
    tag: "万兆存储",
    motif: "spectrum",
  },
  {
    bvid: "BV1fN411X7uA",
    title: "浏览器视图过渡进阶：几何圆环裁剪与跨视图状态编排",
    headline: "浏览器视图过渡编排",
    play_count: 589300,
    cid_count: 1,
    duration: 742,
    pub_days: 31,
    weight: 0.068,
    colors: ["#FF2A7A", "#7C5CFF"],
    tag: "动效引擎",
    motif: "radar",
  },
  {
    bvid: "BV1kP411R7bC",
    title: "软件定义无线电入门：80 元接收机实时解码近地气象卫星云图",
    headline: "卫星无线电云图解码",
    play_count: 524600,
    cid_count: 2,
    duration: 1265,
    pub_days: 42,
    weight: 0.052,
    colors: ["#10B981", "#00E5FF"],
    tag: "无线解码",
    motif: "scope",
  },
  {
    bvid: "BV1tV411F7zM",
    title: "参考级广色域监视器校色全流程：硬件色彩查找表与灰阶实测",
    headline: "参考监视器硬件校色",
    play_count: 412900,
    cid_count: 1,
    duration: 695,
    pub_days: 55,
    weight: 0.041,
    colors: ["#F59E0B", "#FF2A7A"],
    tag: "色彩科学",
    motif: "prism",
  },
  {
    bvid: "BV1yK4y1C7sP",
    title: "系统内核可观测性实战：零侵入追踪微秒级网络延迟毛刺",
    headline: "内核网络延迟观测",
    play_count: 356700,
    cid_count: 2,
    duration: 1084,
    pub_days: 69,
    weight: 0.032,
    colors: ["#00E5FF", "#3B82F6"],
    tag: "内核观测",
    motif: "spectrum",
  },
  {
    bvid: "BV1dL411E7gH",
    title: "磁轴与霍尔传感器延迟盲测：用逻辑分析仪还原真实微秒级触发曲线",
    headline: "磁轴逻辑分析仪实测",
    play_count: 298400,
    cid_count: 1,
    duration: 638,
    pub_days: 84,
    weight: 0.025,
    colors: ["#FF2A7A", "#10B981"],
    tag: "延迟盲测",
    motif: "radar",
  },
  {
    bvid: "BV1pS421K7xN",
    title: "高可靠异步采集器工程学：动态签名、令牌桶限流与断点恢复",
    headline: "异步采集器限流工程",
    play_count: 246100,
    cid_count: 1,
    duration: 876,
    pub_days: 98,
    weight: 0.020,
    colors: ["#10B981", "#7C5CFF"],
    tag: "异步采集",
    motif: "circuit",
  },
  {
    bvid: "BV1uM4y1D7vQ",
    title: "信号控制室桌面声学改造：驻波扫频测量、低频陷阱与近场监听几何",
    headline: "工作室声学扫频摆位",
    play_count: 193800,
    cid_count: 1,
    duration: 764,
    pub_days: 116,
    weight: 0.017,
    colors: ["#F59E0B", "#00E5FF"],
    tag: "声学改造",
    motif: "cube",
  },
];

function computeCircadianTelemetry(tsMs, stepIndex, nowMs) {
  const d = new Date(tsMs);
  const hourFloat = d.getHours() + d.getMinutes() / 60;
  const daysAgo = Math.max(0, (nowMs - tsMs) / (86400 * 1000));
  const isWeekend = d.getDay() === 0 || d.getDay() === 6;

  // Circadian viewer rhythm: peak around 21:00, secondary noon bump around 12:30, trough ~04:30
  const circadian = Math.sin(((hourFloat - 14.5) / 24) * Math.PI * 2);
  const eveningPeak = Math.exp(-Math.pow((hourFloat - 21.2) / 2.9, 2));
  const noonBump = Math.exp(-Math.pow((hourFloat - 12.6) / 2.0, 2)) * 0.42;

  // Upload surge envelopes around 2d, 6d, 16d, and 23d ago
  const uploadSurge =
    Math.exp(-Math.pow((daysAgo - 1.8) / 1.4, 2)) * 185 +
    Math.exp(-Math.pow((daysAgo - 5.7) / 1.8, 2)) * 130 +
    Math.exp(-Math.pow((daysAgo - 15.8) / 2.2, 2)) * 95 +
    Math.exp(-Math.pow((daysAgo - 22.8) / 2.5, 2)) * 80;

  const weekendBoost = isWeekend ? 48 : 0;
  const harmonicNoise = Math.sin(stepIndex * 0.47) * 22 + Math.cos(stepIndex * 0.21) * 14;

  const onlineTotal = Math.max(
    68,
    Math.round(
      275 +
        circadian * 105 +
        eveningPeak * 310 +
        noonBump * 115 +
        weekendBoost +
        uploadSurge * 0.55 +
        harmonicNoise
    )
  );

  // Follower growth rate per hour scaled by interval
  const hourlyFollowerRate =
    14.5 +
    eveningPeak * 16 +
    (isWeekend ? 4.2 : 0) +
    uploadSurge * 0.22 +
    Math.sin(stepIndex * 0.33) * 3.1;

  return { onlineTotal, hourlyFollowerRate: Math.max(1.2, hourlyFollowerRate) };
}

function downsampleSnapshots(snapshots, maxPoints = 280) {
  if (!Array.isArray(snapshots) || snapshots.length <= maxPoints) {
    return snapshots;
  }

  const total = snapshots.length;
  const tailKeep = 32;
  const headCount = total - tailKeep;
  const targetHead = Math.max(48, maxPoints - tailKeep);
  const stride = headCount / targetHead;

  const mustKeep = new Set([0, total - 1]);

  // Preserve global min/max indices so chart callout pins stay exact
  let minF = Infinity;
  let maxF = -Infinity;
  let minFIdx = 0;
  let maxFIdx = 0;
  let minO = Infinity;
  let maxO = -Infinity;
  let minOIdx = 0;
  let maxOIdx = 0;

  for (let i = 0; i < total; i++) {
    const s = snapshots[i];
    if (typeof s.follower_count === "number") {
      if (s.follower_count < minF) {
        minF = s.follower_count;
        minFIdx = i;
      }
      if (s.follower_count > maxF) {
        maxF = s.follower_count;
        maxFIdx = i;
      }
    }
    if (typeof s.online_viewers_total === "number") {
      if (s.online_viewers_total < minO) {
        minO = s.online_viewers_total;
        minOIdx = i;
      }
      if (s.online_viewers_total > maxO) {
        maxO = s.online_viewers_total;
        maxOIdx = i;
      }
    }
  }
  mustKeep.add(minFIdx);
  mustKeep.add(maxFIdx);
  mustKeep.add(minOIdx);
  mustKeep.add(maxOIdx);

  for (let k = 0; k < targetHead; k++) {
    mustKeep.add(Math.min(headCount - 1, Math.round(k * stride)));
  }
  for (let i = Math.max(0, headCount); i < total; i++) {
    mustKeep.add(i);
  }

  const indices = Array.from(mustKeep).sort((a, b) => a - b);
  return indices.map((idx) => snapshots[idx]);
}

function resolveDatasetContainer(stateOrDataset) {
  if (stateOrDataset && stateOrDataset.accountsByUid && stateOrDataset.activeUid) {
    return stateOrDataset;
  }
  if (
    stateOrDataset &&
    stateOrDataset.demoDataset &&
    stateOrDataset.demoDataset.accountsByUid
  ) {
    return stateOrDataset.demoDataset;
  }
  const created = createDemoDataset();
  if (stateOrDataset && typeof stateOrDataset === "object") {
    stateOrDataset.demoDataset = created;
  }
  return created;
}

function resolveDataset(stateOrDataset) {
  if (stateOrDataset && Array.isArray(stateOrDataset.allSnapshots)) {
    return stateOrDataset;
  }
  const container = resolveDatasetContainer(stateOrDataset);
  return (
    container.accountsByUid[container.activeUid] ||
    Object.values(container.accountsByUid)[0]
  );
}

function createSingleDemoAccountDataset(profile, now, timeline) {
  const minMs = 60 * 1000;
  const dayMs = 24 * 60 * minMs;
  const scale = profile.onlineScale || 1;
  const phaseShift = profile.phaseShift || 0;

  const rawSteps = timeline.map((pt, idx) => {
    const tele = computeCircadianTelemetry(pt.ts, idx + Math.round(phaseShift * 10), now);
    const onlineTotal = Math.max(18, Math.round(tele.onlineTotal * scale));
    const deltaFollowers = Math.max(0, Math.round(tele.hourlyFollowerRate * scale * pt.dtHours));
    return {
      ts: pt.ts,
      onlineTotal,
      deltaFollowers,
    };
  });

  const totalGrowth = rawSteps.reduce((acc, item) => acc + item.deltaFollowers, 0);
  const targetLatestFollowers = profile.targetFollowers || 286428;
  let runningFollowers = Math.max(12000, targetLatestFollowers - totalGrowth);
  const specs = DEMO_VIDEO_SPECS.slice(profile.videoSlice[0], profile.videoSlice[1]);
  const weightSum = specs.reduce((acc, s) => acc + s.weight, 0) || 1;
  const totalPlayBase = Math.round(
    specs.reduce((acc, s) => acc + s.play_count * scale, 0) - timeline.length * 140 * scale
  );

  const snapshots = rawSteps.map((step, idx) => {
    runningFollowers += step.deltaFollowers;
    const fromEnd = rawSteps.length - 1 - idx;
    const isPartial = fromEnd === 19 || fromEnd === 142;
    const status = isPartial ? "partial" : "success";
    const succeeded = isPartial ? Math.max(1, specs.length - 1) : specs.length;

    return {
      id: idx + 1,
      uid: profile.uid,
      collected_at: new Date(step.ts).toISOString(),
      collected_at_ts: Math.floor(step.ts / 1000),
      follower_count: runningFollowers,
      following_count: 42 + (profile.uid.charCodeAt(0) % 70),
      online_viewers_total: step.onlineTotal,
      video_count: specs.length,
      videos_succeeded: succeeded,
      extra_metrics: {
        total_video_plays: Math.max(0, Math.round(totalPlayBase + idx * 140 * scale)),
      },
      status,
      error_message: isPartial ? "1 个分P节点触发瞬时频控 (-799)，退避重试后已跳过" : null,
      duration_ms: 820 + Math.round(Math.abs(Math.sin(idx * 0.63 + phaseShift)) * 410),
    };
  });

  const latest = snapshots[snapshots.length - 1];
  const previous = snapshots[snapshots.length - 2] || latest;

  const videoHistories = {};

  let remainingOnline = latest.online_viewers_total;
  const videos = specs.map((spec, idx) => {
    const normWeight = spec.weight / weightSum;
    const channel = `频道${String(idx + 1).padStart(2, "0")}`;
    const online =
      idx === specs.length - 1
        ? Math.max(1, remainingOnline)
        : Math.max(1, Math.round(latest.online_viewers_total * normWeight));
    remainingOnline = Math.max(1, remainingOnline - online);

    const onlineCounts = snapshots.map((snap, hIdx) => {
      const wave = Math.sin(hIdx * 0.39 + idx * 1.17 + phaseShift) * (2 + normWeight * 22);
      const isLast = hIdx === snapshots.length - 1;
      return isLast
        ? online
        : Math.max(1, Math.round(snap.online_viewers_total * normWeight + wave));
    });

    const scaledPlayCount = Math.round(spec.play_count * (0.55 + scale * 0.45));
    const likeRatio = 0.052 + ((idx * 17 + Math.round(phaseShift * 10)) % 75) / 1000;
    const coinRatio = 0.008 + ((idx * 13 + Math.round(phaseShift * 7)) % 34) / 1000;
    const likeCount = Math.round(scaledPlayCount * likeRatio);
    const coinCount = Math.round(scaledPlayCount * coinRatio);

    const rawIncrements = onlineCounts.map((cnt, hIdx) => {
      if (hIdx === 0) return 0;
      const dtHours = Math.max(
        1 / 60,
        (snapshots[hIdx].collected_at_ts - snapshots[hIdx - 1].collected_at_ts) / 3600
      );
      return Math.max(1, Math.round((cnt * 6.5 + (scaledPlayCount / 120000) * 45) * dtHours));
    });
    const rawGainSum = rawIncrements.reduce((acc, n) => acc + n, 0);
    const maxAllowedGain = Math.round(scaledPlayCount * 0.68);
    const gainScale = rawGainSum > maxAllowedGain && rawGainSum > 0 ? maxAllowedGain / rawGainSum : 1;
    const playIncrements = rawIncrements.map((inc, hIdx) =>
      hIdx === 0 ? 0 : Math.max(1, Math.round(inc * gainScale))
    );
    const totalWindowPlayGain = playIncrements.reduce((acc, n) => acc + n, 0);
    let runningPlays = Math.max(0, scaledPlayCount - totalWindowPlayGain);

    const rawLikeIncrements = playIncrements.map((pInc, hIdx) => {
      if (hIdx === 0) return 0;
      const engagementWave = 0.78 + Math.sin(hIdx * 0.51 + idx * 1.43 + phaseShift) * 0.28;
      return Math.max(0, Math.round(pInc * likeRatio * engagementWave));
    });
    const rawLikeGainSum = rawLikeIncrements.reduce((acc, n) => acc + n, 0);
    const maxAllowedLikeGain = Math.round(likeCount * 0.72);
    const likeGainScale =
      rawLikeGainSum > maxAllowedLikeGain && rawLikeGainSum > 0
        ? maxAllowedLikeGain / rawLikeGainSum
        : 1;
    const likeIncrements = rawLikeIncrements.map((lInc, hIdx) =>
      hIdx === 0 ? 0 : Math.max(0, Math.round(lInc * likeGainScale))
    );
    const totalWindowLikeGain = likeIncrements.reduce((acc, n) => acc + n, 0);
    let runningLikes = Math.max(0, likeCount - totalWindowLikeGain);

    const history = snapshots.map((snap, hIdx) => {
      const count = onlineCounts[hIdx];
      runningPlays += playIncrements[hIdx];
      runningLikes += likeIncrements[hIdx];
      return {
        collected_at: snap.collected_at,
        collected_at_ts: snap.collected_at_ts,
        online_count: count,
        online_raw: `${count}人正在看`,
        play_count: runningPlays,
        like_count: runningLikes,
      };
    });
    const bvidKey = profile.uid === DEMO_UID ? spec.bvid : `${spec.bvid}_${profile.uid.slice(-3)}`;
    videoHistories[bvidKey] = history;

    return {
      bvid: bvidKey,
      uid: profile.uid,
      aid: 1120000000 + idx * 38417 + Number(profile.uid.slice(-4)),
      title: spec.title,
      pic: buildDemoCoverSvg({
        channel,
        tag: spec.tag,
        headline: spec.headline,
        c1: spec.colors[0],
        c2: spec.colors[1],
        motif: spec.motif,
        index: idx,
      }),
      pubdate: Math.floor((now - spec.pub_days * dayMs) / 1000),
      duration: spec.duration,
      play_count: scaledPlayCount,
      like_count: likeCount,
      coin_count: coinCount,
      stats_synced_at: latest.collected_at,
      cid_count: spec.cid_count,
      last_online_count: online,
      last_online_raw: `${online}人正在看`,
      last_checked_at: latest.collected_at,
      _weight: normWeight,
      _likeRatio: likeRatio,
      _likeCarry: 0,
      _phase: idx * 0.91 + phaseShift,
    };
  });

  const exactVideoSum = videos.reduce((acc, v) => acc + v.last_online_count, 0);
  latest.online_viewers_total = exactVideoSum;
  if (videoHistories[videos[0].bvid]?.length) {
    videoHistories[videos[0].bvid][videoHistories[videos[0].bvid].length - 1].online_count =
      videos[0].last_online_count;
  }

  return {
    tickCounter: 0,
    baseTotalSnapshots: 8640 - Math.round(phaseShift * 420),
    baseAnchorLevel: Math.round(590 * scale),
    allSnapshots: snapshots,
    videos,
    videoHistories,
    logs: snapshots.slice(-30).reverse(),
    settings: {
      uid: profile.uid,
      has_cookie: true,
      cookie_preview: "凭证已配置",
      interval_minutes: 5,
      enabled: true,
      request_delay_ms: 200,
      video_sync_interval_minutes: 30,
      account_name: profile.name,
      account_face: buildDemoAvatarSvg(profile.accent, profile.accent2),
    },
    scheduler: {
      running: true,
      enabled: true,
      interval_minutes: 5,
      next_run_time: new Date(now + 4 * minMs + 12 * 1000).toISOString(),
      is_crawling: false,
      crawling_uid: null,
      enabled_accounts_count: DEMO_ACCOUNT_PROFILES.length,
      last_crawl_at: latest.collected_at,
      last_crawl_status: latest.status,
    },
    latest,
    previous,
  };
}

export function createDemoDataset() {
  const now = Date.now();
  const minMs = 60 * 1000;
  const hourMs = 60 * minMs;
  const dayMs = 24 * hourMs;

  const timeline = [];
  for (let t = now - 30 * dayMs; t < now - 7 * dayMs; t += 2 * hourMs) {
    timeline.push({ ts: t, dtHours: 2 });
  }
  for (let t = now - 7 * dayMs; t < now - 24 * hourMs; t += 30 * minMs) {
    timeline.push({ ts: t, dtHours: 0.5 });
  }
  for (let t = now - 24 * hourMs; t <= now; t += 5 * minMs) {
    timeline.push({ ts: t, dtHours: 5 / 60 });
  }

  const accountsByUid = {};
  const accountOrder = [];
  for (const profile of DEMO_ACCOUNT_PROFILES) {
    accountsByUid[profile.uid] = createSingleDemoAccountDataset(profile, now, timeline);
    accountOrder.push(profile.uid);
  }

  return {
    activeUid: DEMO_UID,
    accountOrder,
    accountsByUid,
  };
}

function tickSingleDemoAccount(ds) {
  ds.tickCounter = (ds.tickCounter || 0) + 1;

  const now = Date.now();
  const nowIso = new Date(now).toISOString();
  const nowSec = Math.floor(now / 1000);
  const prevSnap = ds.latest;

  const baseLevel = ds.baseAnchorLevel || 590;
  const baseAnchor =
    baseLevel +
    Math.sin(ds.tickCounter * 0.27) * (baseLevel * 0.08) +
    Math.cos(ds.tickCounter * 0.11) * (baseLevel * 0.04);

  let newOnlineTotal = 0;
  let totalPlaysSum = 0;

  for (let i = 0; i < ds.videos.length; i++) {
    const v = ds.videos[i];
    const ideal = Math.max(2, baseAnchor * (v._weight || 0.04));
    const pull = (ideal - v.last_online_count) * 0.28;
    const harmonic = Math.sin(ds.tickCounter * 0.65 + (v._phase || i)) * (1.5 + (v._weight || 0.04) * 28);
    const microJitter = ((ds.tickCounter * 7 + i * 13) % 7) - 3;
    const nextOnline = Math.max(1, Math.round(v.last_online_count + pull + harmonic * 0.45 + microJitter * 0.6));

    v.last_online_count = nextOnline;
    v.last_online_raw = `${nextOnline}人正在看`;
    v.last_checked_at = nowIso;

    const playStep = Math.max(0, Math.round((nextOnline / 45) + ((ds.tickCounter + i) % 3 === 0 ? 2 : 0)));
    v.play_count += playStep;
    const likeRatio = Number(v._likeRatio || 0.068);
    const likeAccum = Number(v._likeCarry || 0) + playStep * likeRatio;
    const likeStep = Math.floor(likeAccum);
    v._likeCarry = likeAccum - likeStep;
    v.like_count = Math.max(0, Number(v.like_count || 0) + likeStep);
    totalPlaysSum += v.play_count;
    newOnlineTotal += nextOnline;

    const hist = ds.videoHistories[v.bvid] || (ds.videoHistories[v.bvid] = []);
    hist.push({
      collected_at: nowIso,
      collected_at_ts: nowSec,
      online_count: nextOnline,
      online_raw: `${nextOnline}人正在看`,
      play_count: v.play_count,
      like_count: v.like_count,
    });
    if (hist.length > 1100) {
      hist.splice(760, 1);
    }
  }

  let followerStep = 0;
  const roll = Math.sin(ds.tickCounter * 1.73 + (ds.videos[0]?._phase || 0)) * 0.5 + 0.5;
  if (roll > 0.52) {
    followerStep = 1 + (ds.tickCounter % 4);
  } else if (ds.tickCounter % 19 === 0) {
    followerStep = -1;
  }

  const nextFollowers = Math.max(0, (prevSnap?.follower_count || 10000) + followerStep);
  const nextId = (prevSnap?.id || ds.allSnapshots.length) + 1;

  const liveSnap = {
    id: nextId,
    uid: ds.settings.uid,
    collected_at: nowIso,
    collected_at_ts: nowSec,
    follower_count: nextFollowers,
    following_count: prevSnap?.following_count || 94,
    online_viewers_total: newOnlineTotal,
    video_count: ds.videos.length,
    videos_succeeded: ds.videos.length,
    extra_metrics: {
      total_video_plays: totalPlaysSum,
    },
    status: "success",
    error_message: null,
    duration_ms: 880 + ((ds.tickCounter * 73) % 340),
  };

  ds.previous = prevSnap;
  ds.latest = liveSnap;
  ds.allSnapshots.push(liveSnap);
  ds.baseTotalSnapshots = (ds.baseTotalSnapshots || 8640) + 1;

  if (ds.allSnapshots.length > 1100) {
    ds.allSnapshots.splice(760, 1);
  }

  ds.logs = ds.allSnapshots.slice(-30).reverse();
  ds.scheduler.last_crawl_at = nowIso;
  ds.scheduler.last_crawl_status = "success";

  const nextRunMs = Date.parse(ds.scheduler.next_run_time || "");
  if (Number.isNaN(nextRunMs) || nextRunMs <= now + 1500) {
    ds.scheduler.next_run_time = new Date(now + ds.scheduler.interval_minutes * 60 * 1000).toISOString();
  }

  return liveSnap;
}

export function tickDemoDataset(stateOrDataset) {
  if (stateOrDataset && Array.isArray(stateOrDataset.allSnapshots)) {
    return tickSingleDemoAccount(stateOrDataset);
  }
  const container = resolveDatasetContainer(stateOrDataset);
  let activeSnap = null;
  for (const uid of container.accountOrder) {
    const ds = container.accountsByUid[uid];
    if (!ds) continue;
    const snap = tickSingleDemoAccount(ds);
    if (uid === container.activeUid) {
      activeSnap = snap;
    }
  }
  return activeSnap;
}

export function getDemoAccounts(stateOrDataset) {
  const container = resolveDatasetContainer(stateOrDataset);
  return container.accountOrder.map((uid, idx) => {
    const ds = container.accountsByUid[uid];
    const latest = ds.latest;
    const prev = ds.previous;
    return {
      uid,
      account_name: ds.settings.account_name,
      account_face: ds.settings.account_face,
      has_cookie: true,
      cookie_preview: "凭证已配置",
      enabled: true,
      sort_order: idx,
      latest_follower_count: latest?.follower_count ?? null,
      latest_online_viewers_total: latest?.online_viewers_total ?? null,
      follower_delta:
        latest && prev ? Number(latest.follower_count || 0) - Number(prev.follower_count || 0) : 0,
      online_delta:
        latest && prev
          ? Number(latest.online_viewers_total || 0) - Number(prev.online_viewers_total || 0)
          : 0,
      video_count: ds.videos.length,
      last_crawl_at: latest?.collected_at ?? null,
      last_crawl_status: latest?.status ?? null,
      is_crawling: false,
      is_demo: true,
    };
  });
}

export function switchDemoAccount(stateOrDataset, uid) {
  const container = resolveDatasetContainer(stateOrDataset);
  if (uid && container.accountsByUid[uid]) {
    container.activeUid = uid;
  }
  return container.activeUid;
}

export function reorderDemoAccounts(stateOrDataset, orderedUids = []) {
  const container = resolveDatasetContainer(stateOrDataset);
  const existing = container.accountOrder || [];
  const existingSet = new Set(existing);
  const seen = new Set();
  const nextOrder = [];

  for (const raw of orderedUids) {
    const clean = String(raw || "").trim();
    if (clean && existingSet.has(clean) && !seen.has(clean)) {
      seen.add(clean);
      nextOrder.push(clean);
    }
  }
  for (const uid of existing) {
    if (!seen.has(uid)) {
      nextOrder.push(uid);
    }
  }
  container.accountOrder = nextOrder;
  return getDemoAccounts(container);
}

const BJT_OFFSET_SEC = 8 * 3600;

function getBjtDayStartSec(tsSec) {
  const shifted = tsSec + BJT_OFFSET_SEC;
  return Math.floor(shifted / 86400) * 86400 - BJT_OFFSET_SEC;
}

function formatBjtDate(dayStartSec) {
  const d = new Date((dayStartSec + BJT_OFFSET_SEC) * 1000);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function computeDayFollowerChange(dayCounts, prevDayCounts) {
  if (!dayCounts || dayCounts.length === 0) return null;
  const endVal = dayCounts[dayCounts.length - 1];
  if (prevDayCounts && prevDayCounts.length > 0) {
    return endVal - prevDayCounts[prevDayCounts.length - 1];
  }
  if (dayCounts.length >= 2) {
    return endVal - dayCounts[0];
  }
  return null;
}

function computeDemoFollowerDaily(allSnapshots, refTsSec) {
  const todayStart = getBjtDayStartSec(refTsSec);
  const yesterdayStart = todayStart - 86400;
  const dayBeforeStart = todayStart - 2 * 86400;
  const tomorrowStart = todayStart + 86400;

  const dayBeforeCounts = [];
  const yesterdayCounts = [];
  const todayCounts = [];

  for (const snap of allSnapshots || []) {
    if (snap.follower_count === null || snap.follower_count === undefined) continue;
    const ts = Number(snap.collected_at_ts);
    const val = Number(snap.follower_count);
    if (!Number.isFinite(ts) || !Number.isFinite(val)) continue;
    if (ts < dayBeforeStart || ts >= tomorrowStart) continue;
    if (ts < yesterdayStart) {
      dayBeforeCounts.push(val);
    } else if (ts < todayStart) {
      yesterdayCounts.push(val);
    } else {
      todayCounts.push(val);
    }
  }

  return {
    yesterday: computeDayFollowerChange(yesterdayCounts, dayBeforeCounts),
    today: computeDayFollowerChange(todayCounts, yesterdayCounts),
    yesterday_date: formatBjtDate(yesterdayStart),
    today_date: formatBjtDate(todayStart),
    timezone: "Asia/Shanghai",
  };
}

export function getDemoDashboard(stateOrDataset, hours = 24) {
  const container = resolveDatasetContainer(stateOrDataset);
  const ds = resolveDataset(stateOrDataset);
  const allSnaps = ds.allSnapshots;
  const latest = ds.latest || allSnaps[allSnaps.length - 1];
  const prev = ds.previous || allSnaps[allSnaps.length - 2] || latest;

  let filtered = allSnaps;
  const numHours = Number(hours || 0);
  if (numHours > 0 && latest) {
    const cutoffSec = latest.collected_at_ts - numHours * 3600;
    filtered = allSnaps.filter((s) => s.collected_at_ts >= cutoffSec);
    if (filtered.length < 2) {
      filtered = allSnaps.slice(-Math.min(24, allSnaps.length));
    }
  }

  const sampledSnapshots = downsampleSnapshots(filtered, 280);
  const sortedVideos = [...ds.videos].sort(
    (a, b) => Number(b.last_online_count || 0) - Number(a.last_online_count || 0)
  );
  const refTsSec = Math.max(
    Math.floor(Date.now() / 1000),
    Number(latest?.collected_at_ts || 0)
  );

  return {
    settings: { ...ds.settings },
    scheduler: { ...ds.scheduler },
    latest,
    previous: prev,
    follower_delta:
      latest && prev ? Number(latest.follower_count || 0) - Number(prev.follower_count || 0) : 0,
    online_delta:
      latest && prev
        ? Number(latest.online_viewers_total || 0) - Number(prev.online_viewers_total || 0)
        : 0,
    follower_daily: computeDemoFollowerDaily(allSnaps, refTsSec),
    total_snapshots: ds.baseTotalSnapshots || allSnaps.length,
    total_videos: ds.videos.length,
    snapshots: sampledSnapshots,
    top_videos: sortedVideos,
    accounts: getDemoAccounts(container),
  };
}

function downsampleVideoHistory(history, maxPoints = 280) {
  if (!Array.isArray(history) || history.length <= maxPoints) {
    return history;
  }

  const total = history.length;
  const tailKeep = Math.min(32, Math.floor(total / 4));
  const headCount = total - tailKeep;
  const targetHead = Math.max(24, maxPoints - tailKeep);
  const stride = headCount / targetHead;

  const mustKeep = new Set([0, total - 1]);
  let minOnline = Infinity;
  let maxOnline = -Infinity;
  let minOnlineIdx = 0;
  let maxOnlineIdx = 0;
  let minPlay = Infinity;
  let maxPlay = -Infinity;
  let minPlayIdx = -1;
  let maxPlayIdx = -1;
  let minLike = Infinity;
  let maxLike = -Infinity;
  let minLikeIdx = -1;
  let maxLikeIdx = -1;

  for (let i = 0; i < total; i++) {
    const pt = history[i];
    const online = Number(pt.online_count);
    if (Number.isFinite(online)) {
      if (online < minOnline) {
        minOnline = online;
        minOnlineIdx = i;
      }
      if (online > maxOnline) {
        maxOnline = online;
        maxOnlineIdx = i;
      }
    }
    if (pt.play_count !== null && pt.play_count !== undefined) {
      const plays = Number(pt.play_count);
      if (Number.isFinite(plays)) {
        if (plays < minPlay) {
          minPlay = plays;
          minPlayIdx = i;
        }
        if (plays > maxPlay) {
          maxPlay = plays;
          maxPlayIdx = i;
        }
      }
    }
    if (pt.like_count !== null && pt.like_count !== undefined) {
      const likes = Number(pt.like_count);
      if (Number.isFinite(likes)) {
        if (likes < minLike) {
          minLike = likes;
          minLikeIdx = i;
        }
        if (likes > maxLike) {
          maxLike = likes;
          maxLikeIdx = i;
        }
      }
    }
  }

  mustKeep.add(minOnlineIdx);
  mustKeep.add(maxOnlineIdx);
  if (minPlayIdx >= 0) mustKeep.add(minPlayIdx);
  if (maxPlayIdx >= 0) mustKeep.add(maxPlayIdx);
  if (minLikeIdx >= 0) mustKeep.add(minLikeIdx);
  if (maxLikeIdx >= 0) mustKeep.add(maxLikeIdx);

  for (let k = 0; k < targetHead; k++) {
    mustKeep.add(Math.min(headCount - 1, Math.round(k * stride)));
  }
  for (let i = Math.max(0, headCount); i < total; i++) {
    mustKeep.add(i);
  }

  const indices = Array.from(mustKeep).sort((a, b) => a - b);
  return indices.map((idx) => history[idx]);
}

export function getDemoVideoHistory(stateOrDataset, bvid, options = 120) {
  const ds = resolveDataset(stateOrDataset);
  const targetBvid = bvid || ds.videos[0]?.bvid;
  const isObj = options && typeof options === "object";
  const hours = isObj ? options.hours : undefined;
  const limit = isObj ? options.limit : Number(options || 120);
  const maxPoints = isObj ? Number(options.maxPoints || 280) : 280;

  const existing = ds.videoHistories?.[targetBvid];
  let source = Array.isArray(existing) && existing.length > 0 ? existing : null;

  if (!source) {
    const vid = ds.videos.find((v) => v.bvid === targetBvid) || ds.videos[0];
    const weight = vid ? vid._weight || 0.06 : 0.06;
    const allSnaps = ds.allSnapshots || [];
    const endPlays = Number(vid?.play_count || 10000);
    const startPlays = Math.max(0, endPlays - allSnaps.length * 12);
    const endLikes = Number(vid?.like_count || Math.round(endPlays * 0.07));
    const startLikes = Math.max(0, endLikes - Math.round((endPlays - startPlays) * 0.07));
    source = allSnaps.map((s, idx) => {
      const wave = Math.sin(idx * 0.45) * 3;
      const count = Math.max(1, Math.round(s.online_viewers_total * weight + wave));
      const ratio = allSnaps.length > 1 ? idx / (allSnaps.length - 1) : 1;
      return {
        collected_at: s.collected_at,
        collected_at_ts: s.collected_at_ts,
        online_count: count,
        online_raw: `${count}人正在看`,
        play_count: Math.round(startPlays + (endPlays - startPlays) * ratio),
        like_count: Math.round(startLikes + (endLikes - startLikes) * ratio),
      };
    });
  }

  if (hours !== undefined && hours !== null) {
    const numHours = Number(hours || 0);
    let filtered = source;
    const latestPt = source[source.length - 1];
    if (numHours > 0 && latestPt) {
      const cutoffSec = Number(latestPt.collected_at_ts) - numHours * 3600;
      filtered = source.filter((pt) => Number(pt.collected_at_ts) >= cutoffSec);
      if (filtered.length < 2) {
        filtered = source.slice(-Math.min(12, source.length));
      }
    }
    return downsampleVideoHistory(filtered, maxPoints);
  }

  return source.slice(-Math.max(1, limit || 120));
}

export function getDemoLogs(stateOrDataset, limit = 30) {
  const ds = resolveDataset(stateOrDataset);
  return ds.allSnapshots.slice(-limit).reverse();
}
