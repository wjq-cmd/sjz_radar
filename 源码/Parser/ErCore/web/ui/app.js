"use strict";
/* ============================================================================
   DFM 战术雷达 · 前端逻辑（0901 全量重写）
   数据契约与后端 /api/state 完全一致（liveview.snapshot / RadarApp.state）：
     self,self_yaw,self_name,self_hp,live_active,entities[],flow,meta,cursor,status
   entity: key,kind(self|mate|player|ai|box|loot),name,hero,is_bot,world,rel,yaw,
           team,slot,dead,status,status_key,hp,helmet,vest,helmet_dur,vest_dur,
           bp,cr,weapon,curr_weapon,trail,item_id,grade,price
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const MOBILE = () => window.matchMedia("(max-width:900px)").matches;

/* ------------------------------------------------------------- 偏好存储 */
// 所有开关走同一套读写，避免各处散落 localStorage 键名。
const PREF_DEF = {
  // ai 默认开（0901 改）：烽火地带每局都有 PVE 人机，"看不到 AI"是用户实际
  // 反馈的问题；AI 只按出生点显示、空心标记 + 4 分钟自然过期，噪音可控，
  // 嫌吵一键即关（坞/设置层/战况岛 AI 格三处入口）。
  follow:1, trail:1, hp:1, loot:1, box:1, container:1, aibox:0, ai:1, mate:1, cone:1, gunlen:92, dotsize:100, fontsize:100, tagweight:600,
  name:1, wpn:1, gear:1, dist:1, foe:1, ray:0, tagop:80, lootmin:4, lootsort:"grade", alert:80, sort:"team",
  walltrans:78, floortrans:0, model3d:"tactical", visiblecolor3d:"#49d5ff", occludedcolor3d:"#ff4058", directionstyle3d:"arrow", directionanchor3d:"head", theme:"dark", v3d:0, pip:1, cam3d:"chase", fpvtau:80, fpvheight:1.6, fov:58, charsize:100, q3d:"auto", fpscap:0,
  box3d:0,                                // 3D 人物方框（视图栏 #s-box3d，默认关）
  warn3d:0, warnd:150, warnr:200, warnsz:100,   // 3D 屏幕外预警：距离(m) / 圆半径(px) / 箭头大小(%)
  // 3D：视图 / 小地图 / 相机模式 / 第一视角平滑(ms) / 第一视角眼高(m) / 视野 FOV(度)
  mapkey:"daba",                          // 上次选择的地图(setMap 会回写,启动读回)
};
const PREF = {};
for (const k in PREF_DEF) {
  const v = localStorage.getItem("nr2_" + k);
  PREF[k] = (v === null) ? PREF_DEF[k] : (isNaN(v) || v === "" ? v : Number(v));
}
function setPref(k, v) { PREF[k] = v; try { localStorage.setItem("nr2_" + k, v); } catch (e) {} }
// V5.1.5+ removes the ambiguous old "follow" entry; migrate it to third-person follow.
if (PREF.cam3d === "follow") setPref("cam3d", "chase");

/* ------------------------------------------------------------- 主题 */
// 旧键 paneltheme 只切侧栏；新键 theme 切整站。无 theme 时按旧值/系统偏好迁移。
try {
  if (!localStorage.getItem("nr2_theme")) {
    const old = localStorage.getItem("nr2_paneltheme");
    const sysLight = matchMedia("(prefers-color-scheme: light)").matches;
    setPref("theme", old === "light" ? "light" : old === "dark" ? "dark" : (sysLight ? "light" : "dark"));
  }
} catch (e) {}
function applyTheme(t) {
  t = t === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = t === "light" ? "#F5F6F7" : "#0B0C0E";
  document.dispatchEvent(new CustomEvent("themechange", { detail: { theme: t } }));
}
applyTheme(PREF.theme);

/* ------------------------------------------------------------- 色彩语义 */
// 队伍色板：同队同色、异队异色。self/mate/box/loot 固定语义色。
const TEAM_COLORS = ["var(--team-1)","var(--team-2)","var(--team-3)","var(--team-4)","var(--team-5)","var(--team-6)","var(--team-7)","var(--team-8)"];
const C_SELF = "var(--ally)", C_MATE = "var(--mate)", C_BOX = "var(--box)", C_AI = "var(--loot)", C_UNK = "var(--unk)";
const GRADE_COL = {1:"var(--unk)",2:"var(--ally)",3:"var(--mate)",4:"var(--box)",5:"var(--loot)",6:"var(--danger)"};
// 背包名 -> 等级(gear_catalog.tier)：装备行「包N」用
const BAG_LV = {"斜挎包": 1, "运动背包": 1, "旅行背包": 1, "DG运动背包": 1, "帆布背囊": 1, "轻型户外背包": 2, "战术快拆背包": 2, "突袭战术背包": 2, "露营背包": 2, "大型登山包": 3, "3H战术背包": 3, "DASH战术背包": 3, "GA野战背包": 3, "雨林猎手背包": 3, "MAP侦察背包": 4, "野战徒步背包": 4, "D2战术登山包": 4, "GT1户外登山包": 4, "生存战术背包": 4, "蛟龙战术背包": 4, "ALS背负系统": 5, "HLS-2重型背包": 5, "D3战术登山包": 5, "GT5野战背包": 5, "不投放": 6, "重型登山包": 6, "D7战术背包": 6, "GTO重型战术包": 6};
const RIG_LV = {"便携胸包":1,"快速侦察胸挂":1,"轻型战术胸挂":1,"简易挂载包":1,"尼龙挎包":2,"HK3便携胸挂":2,"D01轻型胸挂":2,"通用战术胸挂":2,"胸挂包":2,"简易携行弹挂":3,"HD3战术胸挂":3,"DSA战术胸挂":3,"G01战术弹挂":3,"突袭胸挂":3,"强袭战术背心":4,"突击者战术背心":4,"DRC先进侦察胸挂":4,"GIR野战胸挂":4,"蛟龙战术胸挂":4,"飓风战术胸挂":5,"指挥官战术背心":5,"黑鹰野战胸挂":5,"DAR突击手胸挂":5,"通用胸挂":5,"指挥官背心":6,"非法道具 看到报bug":6,"G.T.I 5级胸挂":6};
// 背包名 -> 容量(格)：装备行「包N · M格」用
const BAG_CAP = {"斜挎包": 6, "运动背包": 8, "旅行背包": 10, "DG运动背包": 12, "帆布背囊": 10, "轻型户外背包": 12, "战术快拆背包": 15, "突袭战术背包": 15, "露营背包": 15, "大型登山包": 16, "3H战术背包": 18, "DASH战术背包": 20, "GA野战背包": 20, "雨林猎手背包": 21, "MAP侦察背包": 24, "野战徒步背包": 24, "D2战术登山包": 24, "GT1户外登山包": 25, "生存战术背包": 28, "ALS背负系统": 28, "HLS-2重型背包": 28, "D3战术登山包": 28, "GT5野战背包": 30, "重型登山包": 40, "D7战术背包": 35, "GTO重型战术包": 45};
const GRADE_NM  = {1:"1级",2:"2级",3:"3级",4:"4级",5:"5级",6:"6级"};
const STATE_COL = {down:"var(--warn)", dying:"var(--warn)", dead:"var(--box)", revive:"var(--ally)", box_carried:"var(--warn)", box_searching:"var(--warn)", box_empty:"var(--unk)", box_looted:"var(--box)"};
const STATE_TXT = {down:"倒地", dying:"倒地", dead:"阵亡", revive:"救援", box_carried:"搬运中", box_searching:"搜刮中", box_empty:"已搜空", box_looted:"已搜刮"};
const KIND_NM   = {player:"玩家", ai:"AI", self:"自己", box:"死亡盒", container:"容器", loot:"物资", mate:"队友", unknown:"未知"};
const LOOT_IMG  = (id) => "/resources/items/" + encodeURIComponent(id) + ".png";
// 干员头像：换图后把版本号 +1，绕开浏览器长缓存
const AVATAR_V = "3";
const avatarUrl = (hero) => "/avatars/" + encodeURIComponent(hero) + ".png?v=" + AVATAR_V;

// 死亡盒配色：玩家盒金色，人机盒保持原紫色
const boxColor = (e) => (e.is_ai === true || e.is_bot === true) ? C_BOX : "var(--loot)";
// 贴脸预警的滞回状态（key -> bool）：避免距离在阈值边缘抖动时每帧翻转
const alertHold = new Map();
function entColor(e) {
  // 自己绿、队友蓝；敌队按队号取色板（一队一色）
  if (e.kind === "self") return C_SELF;
  if (e.kind === "mate") return C_MATE;
  if (e.kind === "container") return "var(--container)";
  if (e.kind === "box")  return boxColor(e);
  if (e.kind === "loot") return GRADE_COL[e.grade] || "var(--loot)";
  if (e.kind === "ai")   return C_AI;
  if (e.team > 0) return TEAM_COLORS[(e.team - 1) % TEAM_COLORS.length];
  return C_UNK;
}
// 干员台头像里的两字缩写：优先干员名，其次玩家名
function initials(s) {
  s = String(s || "").trim();
  if (!s) return "?";
  const cn = s.match(/[\u4e00-\u9fa5]/g);
  if (cn && cn.length) return cn.slice(0, 2).join("");
  return s.slice(0, 2).toUpperCase();
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtT(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
}
// 情报龄：出生点标记等「非实时」坐标必须带龄，否则会被当成实时位置
function agoTxt(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  return sec < 60 ? sec + "s前" : Math.floor(sec / 60) + "m前";
}
const UPDATE_RANGE_M = 300;
const RANGE_ENTITY_KINDS = new Set(["player", "ai", "mate", "unknown"]);
const DMAX_M = 5000;   // 超过任何地图尺度的距离判无效（自机坐标未解出时的护栏）
function dist2(e) {
  if (!e.rel) return null;
  const x = e.rel[0], y = e.rel[1];
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const d = Math.hypot(x, y) / 100;
  return d <= DMAX_M ? d : null;
}
function isOutOfUpdateRange(e) {
  // Keep the last-known point visible, but make it visually distinct from a
  // live point. Death markers and AI spawn markers already carry their own
  // historical-position semantics and should not get a second warning.
  return RANGE_ENTITY_KINDS.has(e.kind) && !e.dead && !e.spawn_mark
    && e.out_of_range === true;
}
function dz(e) {
  if (!e.rel) return null;
  const z = e.rel[2];
  if (!Number.isFinite(z)) return null;
  const v = z / 100;
  return Math.abs(v) <= DMAX_M ? v : null;
}
// 「（人机干员）」后缀只标伪真人 bot，真 AI 不带
function nameOf(e) {
  if (e.kind === "loot") return lootName(e);
  const n = e.hero || e.name || KIND_NM[e.kind] || "未知";
  return (e.is_bot && (e.hero || e.name)) ? n + "（人机）" : n;
}

let _toastT = 0;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.classList.add("on");
  clearTimeout(_toastT); _toastT = setTimeout(() => t.classList.remove("on"), 1700);
}

/* ------------------------------------------------------------ 地图与投影 */
let MAP_INFO = null, MAPS = {}, DEF_MAP = "daba", map = null, tileLayer = null;
// 世界坐标 -> 平面 (lng,lat)，与官方 main.js getMapPos 一致（含 rotate）
function worldToPlane(wx, wy) {
  const i = MAP_INFO, bj = i.bj || 128, xB = i.width / bj, yB = i.height / bj;
  const cx = i.centerX, cy = i.centerY, r = Number(i.rotate || 0);
  if (r === 90)  return { lng: bj - (cy + wy) / yB, lat: -bj + (cx - wx) / xB };
  if (r === -90) return { lng: bj + (cy + wy) / yB, lat: -bj - (cx - wx) / xB };
  return { lng: bj - (cx - wx) / xB, lat: -bj - (cy + wy) / yB };
}
function pointLatLng(w) { const p = worldToPlane(w[0], w[1]); return L.latLng(p.lat, p.lng); }
// 世界 yaw -> 屏幕旋转角（0=向上，顺时针为正）。
// 直接投影 p 与 p+dir 两点求平面增量，天然吃掉各地图的 rotate 差异。
// dy 必须是 +Δlat：CRS.Simple 的 lat 向上，而 worldToPlane 里 +wy 映到
// −lat（r=0 支），两个负号相互抵消——写负号会把南北朝向镜像（0902 实修，
// 73976 局 13193 对位移-方位角实证 yaw 前向=(cos,+sin)，中位误差 14.4°）。
function yawToScreen(w, yaw) {
  if (yaw == null) return null;
  const r = yaw * Math.PI / 180, L0 = 1500;
  const a = worldToPlane(w[0], w[1]);
  const b = worldToPlane(w[0] + Math.cos(r) * L0, w[1] + Math.sin(r) * L0);
  const dx = b.lng - a.lng, dy = b.lat - a.lat;
  if (!dx && !dy) return null;
  return Math.atan2(dx, dy) * 180 / Math.PI;
}

/* ---------------------------------------------------------- 渲染缓存/状态 */
let markers = new Map(), trails = new Map(), tags = new Map(), hpbars = new Map(), gearbars = new Map(), foebars = new Map();
let selfMarker = null, pickMarker = null;
let follow = !!PREF.follow, seeking = false, followKey = "__self";
let sel = null;           // 干员台选中项 key（地图上闪烁 + 视图定位）
let lastSnap = null;
const lootRows = new Map();
const opRows = new Map();   // 干员台行复用池（key -> {node, html}），避免整块重建打断 hover
const foldGrp = new Set();
// 「值没变就不碰 DOM」缓存：draw 是 100ms 级热路径
const _rc = { pos:new Map(), tip:new Map(), tag:new Map(), icon:new Map(), iconBase:new Map(),
              hp:new Map(), trail:new Map(), roster:"", isl:"", head:"" };

/* ------------------------------------------- 位置平滑（指数趋近 + rAF） */
// 把低频坐标连成连续移动；τ=120ms。传送阈值按世界厘米计算，不能把
// CRS.Simple 的投影单位当经纬度：旧 0.002 在大坝仅约 1.3cm，走路也会吸附。
const smPos = new Map(), smTgt = new Map();
const SM_TAU = 0.12, SM_SNAP_CM = 3000;

/* ------------------------------------------------ 自机位置跳变过滤 */
// 后端在数据变稀时偶尔会把自机位置判到别人（实测：队友）身上。直接画出去会让 2D
// 标记"飘走"、跟随/第一视角的 3D 相机跟着飞（黑屏 + 抽搐）。这里做一次合理性检查：
// 单次跳变超过 SELF_JUMP_CM 先按住旧位置，等新位置连续 SELF_HOLD_N 次都报上来才
// 接受 —— 真实复活/换点最多晚约 0.3 秒显示。
// 20m 的依据：走路约 0.5m/帧、轮询回退时最多几米，单帧 20m 只可能是误判或复活。
const SELF_JUMP_CM = 2000;    // 20m
const SELF_HOLD_N = 6;
let selfHold = null;
function normalizeSelf(raw) {
  if (!raw || raw.length < 3) { selfHold = null; return raw; }
  const x = Number(raw[0]), y = Number(raw[1]);
  if (!isFinite(x) || !isFinite(y)) return raw;      // 交给后端的世界坐标判据兜底
  if (!selfHold) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  const d = Math.hypot(x - selfHold.p[0], y - selfHold.p[1]);
  if (d <= SELF_JUMP_CM) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  selfHold.n += 1;
  if (selfHold.n >= SELF_HOLD_N) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  return selfHold.p.slice();                          // 按住旧位置，等它被连续确认
}
let smRaf = 0, smLast = 0, smSelfW = null;
function smDistanceCm(p, t) {
  const info = MAP_INFO;
  if (!info || !(info.width > 0) || !(info.height > 0)) return Infinity;
  const x = info.width / (info.bj || 128), y = info.height / (info.bj || 128);
  const rotate = Number(info.rotate || 0), lat = t.lat - p.lat, lng = t.lng - p.lng;
  return rotate === 90 || rotate === -90 ? Math.hypot(lat * x, lng * y) : Math.hypot(lng * x, lat * y);
}
function smStep() {
  smRaf = 0;
  const now = performance.now();
  const dt = Math.min((now - smLast) / 1000, 0.25) || 0.016; smLast = now;
  const a = 1 - Math.exp(-dt / SM_TAU);
  let active = false;
  for (const [k, t] of smTgt) {
    const m = markers.get(k) || (k === "__self" ? selfMarker : null);
    if (!m) { smPos.delete(k); smTgt.delete(k); continue; }
    let p = smPos.get(k);
    if (!p) { p = { lat: t.lat, lng: t.lng }; smPos.set(k, p); }
    let chg = false;
    const dl = Math.abs(t.lat - p.lat), dg = Math.abs(t.lng - p.lng);
    if (smDistanceCm(p, t) > SM_SNAP_CM) { p.lat = t.lat; p.lng = t.lng; chg = true; }
    else if (dl > 1e-9 || dg > 1e-9) { p.lat += (t.lat - p.lat) * a; p.lng += (t.lng - p.lng) * a; chg = true; active = true; }
    if (chg) {
      m.setLatLng([p.lat, p.lng]);
      const g = tags.get(k); if (g) g.setLatLng([p.lat, p.lng]);
      const h = hpbars.get(k); if (h) h.setLatLng([p.lat, p.lng]);
      const gb = gearbars.get(k); if (gb) gb.setLatLng([p.lat, p.lng]);
      const fb = foebars.get(k); if (fb) fb.setLatLng([p.lat, p.lng]);
    }
    if (follow && k === followKey) {
      const center = map.latLngToLayerPoint(map.getCenter());
      const target = map.latLngToLayerPoint([p.lat, p.lng]);
      // 阈值放宽：跟随移动目标时避免每帧微调造成小地图抖动
      if (Math.hypot(target.x - center.x, target.y - center.y) >= 3)
        map.panTo([p.lat, p.lng], { animate: false });
    }
  }
  if (active) smRaf = requestAnimationFrame(smStep);
}
function smKick() { if (!smRaf) { smLast = performance.now(); smRaf = requestAnimationFrame(smStep); } }
function smSet(key, ll) {
  smTgt.set(key, { lat: ll.lat, lng: ll.lng });
  if (!smPos.has(key)) smPos.set(key, { lat: ll.lat, lng: ll.lng });
  smKick();
}

/* -------------------------------------------------------------- 血量 */
// hp.total = [真实 Health, 已知 MaxHealth 或 null]。部位属性独立保留，
// 不能相加充当总血量；上限缺失时显示当前血量数值，比例保持未知。
const HP_ORDER = ["head","thorax","abdomen","larm","rarm","lleg","rleg"];
const HP_MAX = { head:40, thorax:50, abdomen:30, larm:30, rarm:30, lleg:40, rleg:30 };
const HP_NM  = { head:"头", thorax:"胸", abdomen:"腹", larm:"左臂", rarm:"右臂", lleg:"左腿", rleg:"右腿" };
function hpCol(f) { return !Number.isFinite(f) ? "var(--unk)" : f <= 0 ? "var(--danger)" : f > .66 ? "var(--ally)" : f > .33 ? "var(--warn)" : "var(--danger)"; }
function hpPart(hp, k) {
  const rec = hp ? hp[k] : null;
  const mx = (rec && Number.isFinite(rec[1]) && rec[1] >= 5 && rec[1] <= 500)
    ? rec[1] : HP_MAX[k];
  const cur = (rec && Number.isFinite(rec[0]) && rec[0] >= 0 && rec[0] <= mx)
    ? rec[0] : null;
  return { known: cur != null, mx: mx, cur: cur,
           f: cur == null ? null : cur / mx };
}
function hpStat(hp) {
  let worst = null;
  for (const k of HP_ORDER) {
    const p = hpPart(hp, k);
    if (p.known && (!worst || p.f < worst.f)) { worst = p; worst.k = k; }
  }
  const total = hp && hp.total;
  const cur = total && Number.isFinite(total[0]) && total[0] >= 0 ? total[0] : null;
  const mx = total && Number.isFinite(total[1]) && total[1] > 0 && total[1] >= cur ? total[1] : null;
  const complete = cur != null && (mx != null || cur === 0);
  const frac = cur === 0 ? 0 : complete ? Math.max(0, Math.min(1, cur / mx)) : null;
  return { frac, upper: complete ? frac : 1, complete,
           known: cur != null ? 1 : 0, cur, mx, worst };
}
function hpHurt(hp) {
  if (!hp) return false;
  const total = hpStat(hp);
  if (total.complete && total.cur < total.mx) return true;
  for (const k of HP_ORDER) { const p = hpPart(hp, k); if (p.known && p.f < 1) return true; }
  return false;
}
function hpBarHtml(hp, cls) {
  const st = hpStat(hp);
  const pct = st.frac * 100;
  const value = st.complete ? ' aria-valuenow="' + Math.round(pct) + '"' : "";
  const uncertain = st.complete ? "" : '<span style="display:block;height:100%;width:'
    + ((st.upper - st.frac) * 100).toFixed(1)
    + '%;background:repeating-linear-gradient(135deg,#66717a 0 2px,#333b43 2px 4px)"></span>';
  return '<div class="' + cls + '" role="meter" aria-label="剩余血量" aria-valuemin="0"'
    + ' aria-valuemax="100"' + value + ' aria-valuetext="' + hpPct(hp)
    + '" title="' + hpPct(hp) + '" style="display:flex"><i style="flex:none;width:'
    + pct.toFixed(1) + "%;background:" + hpCol(st.frac) + '"></i>' + uncertain + '</div>';
}
function hpPct(hp) {
  const st = hpStat(hp);
  if (!st.known) return "未知";
  const cur = Number(st.cur.toFixed(1));
  return st.mx != null ? cur + "/" + Number(st.mx.toFixed(1)) : String(cur);
}
function gearValue(durability, level) {
  return (Array.isArray(durability) && durability[0] != null) ? String(Math.round(durability[0])) : String(level || "?");
}
function gearIconHtml(kind, level, value) {
  const color = GRADE_COL[Number(level)] || "var(--unk)";
  const labels = {helmet:"头盔", armor:"护甲", bag:"背包", rig:"胸挂"};
  return '<span class="gear-chip" title="' + (labels[kind] || "装备") + ' ' + level
    + '级"><i class="gear-icon ' + kind + '" style="--gear:' + color + '"></i><b>' + value + '</b></span>';
}
function updHp(key, ll, hp, outOfRange) {
  const want = PREF.hp && hp != null;
  let hb = hpbars.get(key);
  if (!want) { if (hb) { hb.remove(); hpbars.delete(key); _rc.hp.delete(key); } return; }
  const html = hpBarHtml(hp, "hpb" + (outOfRange ? " out-range" : ""));
  const mk = () => L.divIcon({ className: "", html: html, iconSize: [64, 5], iconAnchor: [32, -38] });
  if (!hb) {
    hb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 900 });
    hb.addTo(map); hpbars.set(key, hb); _rc.hp.set(key, html);
  } else if (_rc.hp.get(key) !== html) { hb.setIcon(mk()); _rc.hp.set(key, html); }
}
/* 标记下方装备行：头甲耐久 + 背包/胸挂等级，图标颜色统一表示等级。 */
function gearBarHtml(e) {
  const parts = [];
  if (e.helmet) parts.push(gearIconHtml("helmet", e.helmet, gearValue(e.helmet_dur, e.helmet)));
  if (e.vest) parts.push(gearIconHtml("armor", e.vest, gearValue(e.vest_dur, e.vest)));
  const blv = e.bp ? BAG_LV[e.bp] : null;
  const rlv = e.cr ? RIG_LV[e.cr] : null;
  if (blv) parts.push(gearIconHtml("bag", blv, blv));
  if (rlv) parts.push(gearIconHtml("rig", rlv, rlv));
  if (!parts.length) return "";
  return '<div class="grb' + (e._out_of_range ? " out-range" : "") + '">' + parts.join("") + "</div>";
}
function updGear(key, ll, html) {
  const want = PREF.gear && html;
  let gb = gearbars.get(key);
  if (!want) { if (gb) { gb.remove(); gearbars.delete(key); } return; }
  const mk = () => L.divIcon({ className: "", html: html, iconSize: [156, 18], iconAnchor: [78, -21] });
  if (!gb) {
    gb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 880 });
    gb.addTo(map); gearbars.set(key, gb); gb._h = html;
  } else if (gb._h !== html) { gb.setIcon(mk()); gb._h = html; }
}
/* 敌人标识：仅敌队玩家（team>0）显示，红底"敌人"，压在信息条正上方。
   跟随「干员名」显示开关（PREF.name）；纵向位置随信息条高度与字体设置联动。 */
function updFoe(key, ll, on, tagAnchorY, down) {
  const want = on && PREF.foe;
  let fb = foebars.get(key);
  if (!want) { if (fb) { fb.remove(); foebars.delete(key); } return; }
  const fs = (Number(PREF.fontsize) || 100) / 100;
  const h = Math.round(14 * fs);
  // 锚点 = 信息条顶边距 + 标识高度：标识底边正好贴住信息条顶边，不留缝
  const ay = Math.round(tagAnchorY + h);
  // 倒地（down/dying）→ 橙色「倒地」；否则红色「敌人」
  const d = !!down;
  const mk = () => L.divIcon({
    className: "",
    html: '<div class="foe' + (d ? " down" : "") + '">' + (d ? "倒地" : "敌人") + "</div>",
    iconSize: [44, h], iconAnchor: [22, ay] });
  if (!fb) {
    fb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 1010 });
    fb.addTo(map); foebars.set(key, fb); fb._a = ay; fb._d = d;
  } else if (fb._a !== ay || fb._d !== d) { fb.setIcon(mk()); fb._a = ay; fb._d = d; }
}
/* ---------------------------------------------------------- 地图标记构建 */
// 单个干员标记 = 朝向锥 + 核心点 + 状态环，全部由 CSS 变量驱动，
// 只有 html 指纹变化才 setIcon（避免每 tick 重建 DOM）。
function mkIcon(e, col, rot) {
  const st = e.status_key && STATE_COL[e.status_key] ? STATE_COL[e.status_key] : null;
  const cls = ["mk"];
  if (e.kind === "self") cls.push("self");
  if (st) cls.push("st");
  if (e.dead) cls.push("dead");
  if (e._out_of_range) cls.push("out-range");
  // 出生点标记：非实时坐标，画成空心，和实时点在视觉上必须能区分
  if (e.spawn_mark) cls.push("spawn");
  if (sel === e.key) cls.push("mark");
  if (e._alert) cls.push("mark");
  let style = "--c:" + col;
  if (st) style += ";--s:" + st;
  if (rot != null) style += ";--rot:" + rot.toFixed(1) + "deg";
  const aim = PREF.cone && rot != null && !e.dead;
  const gun = aim ? '<i class="mk-gun"></i>' : "";
  const cone = aim ? '<i class="mk-cone"></i>' : "";
  // 有干员头像就用头像当标记主体，否则退回原来的圆点
  const face = e.hero
    ? '<img class="mk-face" alt="" src="' + avatarUrl(e.hero) + '" onerror="this.remove()">'
    : '<i class="mk-core"></i>';
  return '<div class="' + cls.join(" ") + '" style="' + style + '">' + gun + cone + face + '</div>';
}
function divIcon(html, size, anchor) {
  return L.divIcon({ className: "", html: html, iconSize: size, iconAnchor: anchor });
}
// 头顶信息条：一行「状态 · T队号 · 头甲 · 武器 · 干员 · 距离」，按设置裁剪
function tagHtml(e, d) {
  const parts = [];
  const col = entColor(e);          // 整条文字用队伍色
  const st = e.status_key ? STATE_TXT[e.status_key] : null;
  if (st) parts.push("<b>" + st + "</b>");
  if (e._out_of_range) parts.push('<span class="far">超距·最后位置</span>');
  // 出生点：位置来自出生事件而非实时轨迹，条上直说 + 带龄
  if (e.spawn_mark) parts.push('<span class="sp">出生点'
    + (e.age_sec != null ? " " + agoTxt(e.age_sec) : "") + "</span>");
  if (e.team > 0 && e.kind !== "mate") parts.push('<span class="tm">T' + e.team + "</span>");
  if (PREF.wpn) { const w = (e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon)); if (w) parts.push("<i>" + esc(w) + "</i>"); }
  if (PREF.name) { const n = e.hero || e.name; if (n) parts.push(esc(e.is_bot && !e.hero ? n + "（人机）" : n)); }
  if (PREF.dist && d != null) parts.push('<span class="tnum">' + d.toFixed(0) + "m</span>");
  if (!parts.length) return "";
  return '<div class="tb-wrap"><div class="tag' + (e._out_of_range ? " out-range" : "")
    + '" style="--tc:' + col + '">' + parts.join("") + "</div></div>";
}
// 信息条真实宽度：离屏容器渲染一次实测，按 HTML 指纹缓存（同样文本只量一次）。
// 估算字宽在中英混排 + 分段 gap 下必然偏小，会导致 Leaflet 固定框裁字。
const _tagW = new Map();
let _ruler = null;
function measureTag(html) {
  const hit = _tagW.get(html);
  if (hit) return hit;
  if (!_ruler) {
    _ruler = document.createElement("div");
    _ruler.style.cssText = "position:absolute;left:-9999px;top:-9999px;visibility:hidden;white-space:nowrap;pointer-events:none";
    document.body.appendChild(_ruler);
  }
  _ruler.innerHTML = html;
  const el = _ruler.querySelector(".tag");
  const w = Math.min(460, Math.max(56, Math.ceil((el ? el.getBoundingClientRect().width : 120) + 6)));
  if (_tagW.size > 600) _tagW.clear();
  _tagW.set(html, w);
  return w;
}

// Tooltip：点击/悬停才看的完整档案，信息量可以大
function tipHtml(e, d) {
  const col = entColor(e);
  const h = ['<div class="tip-h"><span style="width:8px;height:8px;border-radius:50%;background:'
    + col + ';display:inline-block"></span>' + esc(nameOf(e))
    + (e.team > 0 ? '<span style="color:var(--txt-3);font-weight:400">T' + e.team + "</span>" : "") + "</div>"];
  if (e.kind === "container") {
    h.push('<div class="tip-r">搜索容器 · 内容未知</div>');
    if (e.class_path) h.push('<div class="tip-r">' + esc(e.class_path.split('.').pop()) + '</div>');
  } else if (e.kind === "box") {
    h.push('<div class="tip-r">物资 <b>' + (e.empty === true ? '已搜空' : e.empty === false ? '未空' : '未知') + '</b></div>');
    if (e.searching_count || e.looting_count) h.push('<div class="tip-r">正在交互 <b>' + Math.max(e.searching_count || 0, e.looting_count || 0) + ' 人</b></div>');
    h.push(boxContentsHtml(e));
  }
  if (e.kind === "loot") {
    const value = lootValue(e), count = lootCount(e);
    h.push('<div class="tip-r">品质 <b style="color:' + (GRADE_COL[e.grade] || "#fff") + '">'
      + (GRADE_NM[e.grade] || "未知") + "</b> · 数量 <b>" + (count == null ? "未知" : count) + "</b></div>");
    if (value.total != null) h.push('<div class="tip-r">参考总价 <b>' + lootMoney(value.total) + '</b></div>');
    if (value.unit != null) h.push('<div class="tip-r">单件参考价 <b>' + lootMoney(value.unit) + '</b></div>');
    if (Number.isFinite(e.price_per_slot) && e.price_per_slot >= 0)
      h.push('<div class="tip-r">单格参考价 <b>' + lootMoney(e.price_per_slot) + '</b></div>');
    if (!lootItemId(e)) h.push('<div class="tip-r">尚未识别物品 ID</div>');
    else if (lootName(e) === "未知物资") h.push('<div class="tip-r">名称表未收录 · ID ' + esc(lootItemId(e)) + '</div>');
    if (value.unit == null) h.push('<div class="tip-r">参考价未知</div>');
    if (e.source_type === 2) h.push('<div class="tip-r">来源 <b>丢弃物</b></div>');
    if (e.pickup_status === "candidate")
      h.push('<div class="tip-r" style="color:var(--warn)">疑似拾取 · 有人曾在此停留，尚未确认</div>');
    if (e.channel_state === "irrelevant") h.push('<div class="tip-r">最后已知位置</div>');
  } else {
    if (e._out_of_range) h.push('<div class="tip-r" style="color:var(--warn)">超出更新范围 · 最后已知位置</div>');
    if (e.status) h.push('<div class="tip-r">状态 <b style="color:'
      + (STATE_COL[e.status_key] || "#fff") + '">' + esc(e.status) + "</b></div>");
    const g = [];
    if (e.helmet) g.push("头" + e.helmet + durTxt(e.helmet_dur));
    if (e.vest) g.push("甲" + e.vest + durTxt(e.vest_dur));
    const w = (e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon));
    if (w) g.push(esc(w));
    if (g.length) h.push('<div class="tip-r">装备 <b>' + g.join(" · ") + "</b></div>");
    // 血量：总量 + 最惨部位 + 情报完整度。部分同步时显示已知范围，
    // 不把缺失部位伪装成满血。
    if (!e.dead && e.hp != null) {
      const st = hpStat(e.hp);
      if (st.known) {
        h.push('<div class="tip-r">血量 <b style="color:' + hpCol(st.frac) + '">' + hpPct(e.hp) + "</b>"
          + (st.worst && st.worst.f < 1
             ? ' <span style="color:var(--txt-3)">最低 ' + HP_NM[st.worst.k] + " "
               + Math.round(st.worst.cur) + "/" + Math.round(st.worst.mx) + "</span>" : "")
          + "</div>");
        h.push(hpBarHtml(e.hp, "op-hp"));
        if (!st.complete)
          h.push('<div class="tip-r" style="font-size:10.5px;opacity:.66">血量上限未知</div>');
      }
    }
    if (e.spawn_mark) h.push('<div class="tip-r" style="color:var(--warn)">出生点情报 <b>'
      + (e.age_sec != null ? agoTxt(e.age_sec) : "—") + "</b>（非实时位置）</div>");
  }
  if (d != null) h.push('<div class="tip-r tnum">距离 <b>' + d.toFixed(0) + "m</b>"
    + (dz(e) != null ? " · 高差 <b>" + (dz(e) > 0 ? "+" : "") + dz(e).toFixed(0) + "m</b>" : "") + "</div>");
  h.push('<div class="tip-r tnum" style="font-size:10.5px;opacity:.72">x' + Math.round(e.world[0])
    + " y" + Math.round(e.world[1]) + " z" + Math.round(e.world[2])
    + (e.yaw != null ? " · " + Math.round(e.yaw) + "°" : "") + "</div>");
  return h.join("");
}
function durTxt(v) {
  if (!Array.isArray(v) || v[0] == null) return "";
  return " " + Math.round(v[0]) + "/" + (v[1] != null ? Math.round(v[1]) : "?");
}

function boxContentsHtml(e) {
  const items = Array.isArray(e.contents) ? e.contents : [];
  if (!e.contents_known && !items.length) return '<div class="tip-r">物品清单 <b>未同步</b></div>'
    + (e.looted ? '<div class="tip-r">已搜刮，但尚未收到该盒子的物品明细</div>' : '');
  const heading = e.contents_known ? '最近同步清单' : '上次清单（待更新）';
  const unknown = items.filter(item => !item.revealed).length;
  const lines = ['<div class="tip-r">' + heading + ' <b>' + items.length + ' 条'
    + (unknown ? ' · ' + unknown + ' 条未搜索' : '') + '</b></div>'];
  lines.push('<div style="max-height:220px;overflow:auto">');
  for (const item of items) {
    const count = Number.isSafeInteger(item.count) && item.count >= 0 ? item.count : '?';
    lines.push('<div class="tip-r" style="gap:12px"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + esc(item.name || '未识别物品') + '</span><b class="tnum">×' + count + '</b></div>');
  }
  lines.push('</div>');
  return lines.join('');
}

/* -------------------------------------------------------- Loot display rules */
function lootCount(e) {
  return Number.isSafeInteger(e.stack_count) && e.stack_count >= 0 ? e.stack_count : null;
}
function lootName(e) {
  return typeof e.name === "string" && e.name.trim() && e.name !== "物资" ? e.name : "未知物资";
}
function lootItemId(e) {
  const id = String(e.item_id == null ? "" : e.item_id);
  return /^[1-9][0-9]{0,19}$/.test(id) ? id : null;
}
function lootValue(e) {
  const unit = Number.isFinite(e.price) && e.price >= 0 ? e.price : null, count = lootCount(e);
  const product = unit != null && count != null ? unit * count : null;
  return { unit, total: product != null && Number.isFinite(product) && product <= Number.MAX_SAFE_INTEGER ? product : null };
}
const LOOT_MONEY_FORMAT = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
function lootMoney(value) { return LOOT_MONEY_FORMAT.format(value); }
function lootMinGrade() {
  const g = Number(PREF.lootmin);
  return Number.isInteger(g) && g >= 0 && g <= 6 ? g : 4;
}
function lootFilterName() {
  const g = lootMinGrade();
  return ({0:"全部品质",3:"蓝以上",4:"紫以上",5:"金以上",6:"仅红"})[g] || g + "级以上";
}
function lootAvailable(e) {
  return e.kind === "loot" && Array.isArray(e.world) && e.world.length >= 3
    && e.world.slice(0, 3).every(v => Number.isFinite(v) && Math.abs(v) <= 2097152)
    && e.world.slice(0, 3).some(v => v !== 0)
    && !["removed", "confirmed"].includes(e.pickup_status) && e.hidden !== true
    && e.item_state !== 5 && lootCount(e) !== 0;
}
function lootVisible(e) {
  return !!PREF.loot && lootAvailable(e) && (lootMinGrade() === 0 ||
    (Number.isInteger(e.grade) && e.grade >= lootMinGrade() && e.grade <= 6));
}
function lootIdentity(e) {
  const gid = typeof e.inventory_gid === "string" ? e.inventory_gid :
    Number.isSafeInteger(e.inventory_gid) ? String(e.inventory_gid) : "";
  return /^[1-9][0-9]*$/.test(gid) && lootItemId(e) ? gid + ":" + lootItemId(e) : null;
}
function prepareLootEntities(entities) {
  const latest = new Map(), other = [];
  for (const e of entities) {
    if (e.kind !== "loot") { other.push(e); continue; }
    const id = lootIdentity(e) || "actor:" + e.key, old = latest.get(id);
    const seq = x => Number.isFinite(x.updated_seq) ? x.updated_seq : -1;
    if (!old || seq(e) > seq(old) || (seq(e) === seq(old) && String(e.key) > String(old.key))) latest.set(id, e);
  }
  return other.concat([...latest.values()].filter(lootAvailable));
}
function lootSort(a, b) {
  const near = e => { const d = dist2(e); return Number.isFinite(d) ? d : 1e18; };
  const va = lootValue(a), vb = lootValue(b);
  const value = (va.total == null) - (vb.total == null) || (vb.total || 0) - (va.total || 0)
    || (va.unit == null) - (vb.unit == null) || (vb.unit || 0) - (va.unit || 0);
  const quality = (b.grade || 0) - (a.grade || 0), distance = near(a) - near(b);
  const order = PREF.lootsort === "dist" ? distance || quality || value :
    PREF.lootsort === "grade" ? quality || value || distance : value || quality || distance;
  return order || lootName(a).localeCompare(lootName(b), "zh") || String(a.key).localeCompare(String(b.key));
}
function lootThumb(e) {
  const id = lootItemId(e), label = lootName(e);
  return '<span class="loot-thumb" style="--c:' + (GRADE_COL[e.grade] || "var(--unk)") + '">'
    + '<span aria-hidden="true">' + esc(label === "未知物资" ? "?" : initials(label)) + '</span>'
    + (id ? '<img loading="lazy" alt="" src="' + LOOT_IMG(id) + '" onerror="this.remove()">' : '') + '</span>';
}
function lootIconHtml(e) {
  const count = lootCount(e);
  return lootThumb(e) + (count != null && count > 1 ? '<span class="loot-qty">×' + count + '</span>' : '')
    + (e.pickup_status === "candidate" ? '<span class="loot-question">?</span>' : '');
}
function loot3dSnapshot(s) {
  return { ...s, entities: prepareLootEntities(s.entities || []).filter(visible)
    .map(e => e.kind !== "loot" ? e : { ...e, name: lootName(e)
      + (lootCount(e) != null && lootCount(e) > 1 ? " ×" + lootCount(e) : "")
      + (e.pickup_status === "candidate" ? " · 疑似拾取" : "") }) };
}
/* ---------------------------------------------------- End loot display rules */

/* -------------------------------------------------------------- 主渲染 */
function visible(e) {
  if (e.kind === "container" && !PREF.container) return false;
  if (e.kind === "box"  && !PREF.box)  return false;
  if (e.kind === "box" && !PREF.aibox && (e.is_ai === true || e.is_bot === true)) return false;
  if (e.kind === "ai"   && !PREF.ai)   return false;
  if (e.kind === "loot") return lootVisible(e);
  if (e.kind === "mate" && !PREF.mate) return false;
  return true;
}
function draw(s) {
  if (lastSnap && sel) {
    const old = (lastSnap.entities || []).find(e => e.key === sel);
    const next = (s.entities || []).find(e => e.key === sel);
    if (lastSnap.flow !== s.flow || (old && next && old.kind === "loot" && next.kind === "loot"
      && lootIdentity(old) && lootIdentity(next) && lootIdentity(old) !== lootIdentity(next))) {
      sel = null; clearPick();
    }
  }
  lastSnap = s;
  const alertR = Number(PREF.alert) || 0;

  /* --- 自己 --- */
  const self = s.self;
  if (self) {
    const ll = pointLatLng(self);
    const rot = yawToScreen(self, s.self_yaw);
    const ownLife = s.self_life;
    const ownState = ownLife && ownLife.dead ? "dead" : ownLife && ownLife.downed ? "down" : null;
    const pe = { key:"__self", kind:"self", world:self, yaw:s.self_yaw, name:s.self_name || "自己", status_key:ownState, status:STATE_TXT[ownState] || null };
    const html = mkIcon(pe, C_SELF, rot);
    if (!selfMarker) {
      selfMarker = L.marker(ll, { icon: divIcon(html, [26, 26], [13, 13]), keyboard:false, zIndexOffset:1200 });
      selfMarker.bindTooltip("", { direction:"top", offset:[0,-12] });
      selfMarker.addTo(map);
      selfMarker.on("click", () => focusEntity("__self"));   // 点地图上的自己 = 跟随自己
      _rc.icon.set("__self", html);
      smPos.set("__self", { lat: ll.lat, lng: ll.lng });
    }
    if (_rc.icon.get("__self") !== html) {
      selfMarker.setIcon(divIcon(html, [26, 26], [13, 13])); _rc.icon.set("__self", html);
    }
    const spos = self[0] + "," + self[1];
    if (_rc.pos.get("__self") !== spos) { _rc.pos.set("__self", spos); smSet("__self", ll); }
    // 3D 下的 2D 小地图不靠 raf 平滑循环：数据一变就把中心对到自己
    // （仅当跟随目标是自己；点选别人时由 smStep 跟随该目标，别把人拽回来）
    if (follow && is3d() && followKey === "__self") map.panTo(ll, { animate: false });
    smSelfW = { x:self[0], y:self[1], yaw:s.self_yaw };
    const tip = tipHtml({ ...pe, world:self, rel:[0,0,0], hp:s.self_hp }, 0);
    if (_rc.tip.get("__self") !== tip) { selfMarker.setTooltipContent(tip); _rc.tip.set("__self", tip); }
    updHp("__self", ll, s.self_hp);
  } else if (selfMarker) {
    selfMarker.remove(); selfMarker = null;
    ["__self"].forEach(k => { _rc.pos.delete(k); _rc.tip.delete(k); _rc.icon.delete(k);
      smTgt.delete(k); smPos.delete(k); });
    updHp("__self", null, null);
  }

  /* --- 实体 --- */
  const seen = new Set();
  const ents = prepareLootEntities(s.entities || []);
  for (const e of ents) {
    if (!e.world || !visible(e)) continue;
    seen.add(e.key);
    const ll = pointLatLng(e.world), col = entColor(e), d = dist2(e);
    e._out_of_range = isOutOfUpdateRange(e);
    // 贴脸预警只认实时坐标：出生点标记是历史情报，闪红会造成假警报。
    // 带滞回：进入用 alertR、退出用 1.25×alertR。否则距离在阈值边缘抖动时
    // _alert 会逐帧翻转，导致徽章反复显隐 + 列表反复重排（视觉上就是闪）。
    const alertPrev = alertHold.get(e.key) === true;
    const alertLimit = alertPrev ? alertR * 1.25 : alertR;
    const alertOn = alertR > 0 && d != null && d <= alertLimit
      && !e.spawn_mark && (e.kind === "player" || e.kind === "ai") && !e.dead;
    if (alertOn !== alertPrev) alertHold.set(e.key, alertOn);
    e._alert = alertOn;
    const ppos = e.world[0] + "," + e.world[1] + "," + e.world[2];
    let m = markers.get(e.key);

    if (e.kind === "loot") {
      const cls = "loot-point" + (sel === e.key ? " sel" : "")
        + (e.pickup_status === "candidate" ? " candidate" : "");
      const html = lootIconHtml(e), signature = cls + html;
      const icon = () => L.divIcon({html, className: cls, iconSize:[30,30], iconAnchor:[15,15]});
      if (!m) {
        m = L.marker(ll, { icon:icon(), keyboard:false, zIndexOffset:sel === e.key ? 1400 : 0 });
        m.bindTooltip("", {direction:"top",offset:[0,-16]});
        m.addTo(map); markers.set(e.key,m);
        m.on("click", () => focusEntity(e.key));
        smSet(e.key, ll);
        _rc.pos.set(e.key,ppos); _rc.icon.set(e.key,signature);
      } else {
        if (_rc.pos.get(e.key) !== ppos) { m.setLatLng(ll); _rc.pos.set(e.key,ppos); smSet(e.key, ll); }
        if (_rc.icon.get(e.key) !== signature) { m.setIcon(icon()); _rc.icon.set(e.key,signature); }
        m.setZIndexOffset(sel === e.key ? 1400 : 0);
      }
      const tip = tipHtml(e,d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key,tip); }
    } else if (e.kind === "box" || e.kind === "container") {
      const cls = "dbox";
      if (!m) {
        m = L.marker(ll, { icon: divIcon('<div class="' + cls + '" style="--box:'
            + boxColor(e) + '"></div>', [16,16], [8,8]), keyboard:false });
        m.bindTooltip("", { direction:"top", offset:[0,-10], interactive:true });
        m.on("tooltipopen", event => {
          const el = event.tooltip.getElement();
          if (el) L.DomEvent.disableScrollPropagation(el);
        });
        m.addTo(map); markers.set(e.key, m); _rc.pos.set(e.key, ppos);
        m.on("click", () => focusEntity(e.key));
        smSet(e.key, ll);
      } else if (_rc.pos.get(e.key) !== ppos) { m.setLatLng(ll); _rc.pos.set(e.key, ppos); smSet(e.key, ll); }
      const tip = tipHtml(e, d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key, tip); }

    } else {
      const rot = yawToScreen(e.world, e.yaw);
      const html = mkIcon(e, col, rot);
      // 只有朝向会高频变化：base（不含 --rot）不变时原地改 CSS 变量即可，避免
      // setIcon 重建 DOM —— 重建会销毁并重建头像 <img>，表现为「头像突然闪一下」。
      const htmlBase = rot != null ? mkIcon(e, col, null) : html;
      if (!m) {
        m = L.marker(ll, { icon: divIcon(html, [26,26], [13,13]), keyboard:false,
          zIndexOffset: e.kind === "mate" ? 400 : 600 });
        m.bindTooltip("", { direction:"top", offset:[0,-12] });
        m.addTo(map); markers.set(e.key, m);
        m.on("click", () => focusEntity(e.key));   // 点地图上的干员 = 跟随他
        _rc.icon.set(e.key, html); _rc.iconBase.set(e.key, htmlBase); _rc.pos.set(e.key, ppos);
        smPos.set(e.key, { lat: ll.lat, lng: ll.lng });
      } else {
        if (_rc.iconBase.get(e.key) === htmlBase) {
          if (rot != null && _rc.icon.get(e.key) !== html) {
            const el = m.getElement() && m.getElement().firstElementChild;
            if (el) el.style.setProperty("--rot", rot.toFixed(1) + "deg");
            _rc.icon.set(e.key, html);
          }
        } else if (_rc.icon.get(e.key) !== html) {
          m.setIcon(divIcon(html, [26,26], [13,13]));
          _rc.icon.set(e.key, html); _rc.iconBase.set(e.key, htmlBase);
        }
        if (_rc.pos.get(e.key) !== ppos) { _rc.pos.set(e.key, ppos); smSet(e.key, ll); }
      }
      const tip = tipHtml(e, d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key, tip); }

      // 头顶信息条：队友不上（同队距离无情报价值），自己不上
      let g = tags.get(e.key);
      const th = (e.kind !== "mate") ? tagHtml(e, d) : "";
      // 信息条底边固定贴住标记顶部上方 4px：随标记大小/字体大小一起让位，不挡头像。
      // 敌人标识要叠在它正上方，所以锚点先算出来共用。
      const tFsz = (Number(PREF.fontsize) || 100) / 100;
      const tMks = (Number(PREF.dotsize) || 100) / 100;
      const tH = Math.round(19 * tFsz);
      const tAnchorY = Math.round(tH + 13 * tMks + 4);
      if (th) {
        // iconSize 必须能装下整条内容，否则 Leaflet 的固定框会把文字裁掉。
        // 估宽不可靠（字重/间距/分段 gap 都影响），改为离屏实测一次真实宽度并缓存。
        const w = measureTag(th);
        const ic = () => divIcon(th, [w, tH], [w / 2, tAnchorY]);
        if (!g) { g = L.marker(ll, { icon: ic(), keyboard:false, interactive:false, zIndexOffset:1000 });
          g.addTo(map); tags.set(e.key, g); _rc.tag.set(e.key, th); }
        else if (_rc.tag.get(e.key) !== th) { g.setIcon(ic()); _rc.tag.set(e.key, th); }
      } else if (g) { g.remove(); tags.delete(e.key); _rc.tag.delete(e.key); }

      updHp(e.key, ll, e.dead ? null : e.hp, e._out_of_range);
      updGear(e.key, ll, e.dead ? "" : gearBarHtml(e));
      // 只有敌队玩家挂红色「敌人」标识：自己/队友/AI/盒子都不挂
      updFoe(e.key, ll, e.kind === "player" && e.team > 0, tAnchorY,
             !e.dead && (e.status_key === "down" || e.status_key === "dying"));
    }

    /* 轨迹：滚动窗口，位置不动 = 轨迹不变，用首尾+长度做指纹 */
    let tl = trails.get(e.key);
    if (PREF.trail && e.trail && e.trail.length > 1 && e.kind !== "loot" && e.kind !== "box") {
      const t0 = e.trail[0], t1 = e.trail[e.trail.length - 1];
      const th = t0[0] + "," + t0[1] + "|" + t1[0] + "," + t1[1] + "|" + e.trail.length
        + "|" + (e._out_of_range ? "far" : "near");
      if (_rc.trail.get(e.key) !== th) {
        const lls = e.trail.map(pointLatLng);
        if (!tl) { tl = L.polyline(lls, { color: col, weight: 1.6,
            opacity: e._out_of_range ? .10 : .38, interactive: false });
          tl.addTo(map); trails.set(e.key, tl); }
        else { tl.setLatLngs(lls); tl.setStyle({ color: col, opacity: e._out_of_range ? .10 : .38 }); }
        _rc.trail.set(e.key, th);
      }
    } else if (tl) { tl.remove(); trails.delete(e.key); _rc.trail.delete(e.key); }
  }

  /* --- 清理离场实体 --- */
  for (const [k, m] of markers) if (!seen.has(k)) {
    m.remove(); markers.delete(k);
    _rc.pos.delete(k); _rc.tip.delete(k); _rc.icon.delete(k); _rc.trail.delete(k);
    smPos.delete(k); smTgt.delete(k); alertHold.delete(k);
    const t = trails.get(k); if (t) { t.remove(); trails.delete(k); }
  }
  for (const [k, g] of tags)   if (!seen.has(k)) { g.remove(); tags.delete(k); _rc.tag.delete(k); }
  for (const [k, h] of hpbars) if (!seen.has(k) && k !== "__self") { h.remove(); hpbars.delete(k); _rc.hp.delete(k); }
  for (const [k, g] of gearbars) if (!seen.has(k) && k !== "__self") { g.remove(); gearbars.delete(k); }
  for (const [k, f] of foebars) if (!seen.has(k)) { f.remove(); foebars.delete(k); }
  if (sel && !seen.has(sel) && sel !== "__self") { sel = null; clearPick(); }

  drawIsland(s, ents);
  drawRoster(s, ents);
  drawPlaybar(s);
}

/* -------------------------------------------------------------- 战况岛 */
// 折叠态只回答一个问题：还剩几个敌人、最近的有多远。
function drawIsland(s, ents) {
  const nP = ents.filter(e => e.kind === "player").length;
  const nA = ents.filter(e => e.kind === "ai").length;
  const nB = ents.filter(e => e.kind === "box").length;
  const nM = ents.filter(e => e.kind === "mate").length;
  const nSelf = s.self ? 1 : 0;
  const humDead = ents.filter(e => e.kind === "player" && e.dead).length;
  const nDown = ents.filter(e => e.kind === "player" && !e.dead && e.status_key === "down").length;
  const nAlive = Math.max(0, nP - humDead - nDown);
  // 阵亡只算真人：玩家阵亡数 与「真人盒子数」取大（人机盒子不计入）
  const nBHuman = ents.filter(e => e.kind === "box"
    && e.is_ai !== true && e.is_bot !== true).length;
  const nDead = Math.max(humDead, nBHuman);
  // 最近威胁：活着的敌方玩家（AI 不算威胁基线）
  let near = null;
  for (const e of ents) {
    if (e.kind !== "player" || e.dead) continue;
    const d = dist2(e);
    if (d != null && (near == null || d < near)) near = d;
  }
  const live = s.live_active;
  const stale = s.status === "offline" || s.status === "waiting";
  const key = [nAlive, nDown, nDead, nB, nA, nM + nSelf, live ? 1 : 0, stale ? 1 : 0,
               PREF.ai ? 1 : 0, near == null ? "-" : near.toFixed(0), s.flow || ""].join("|");
  if (_rc.isl === key) return;
  _rc.isl = key;
  $("isl-num").textContent = nAlive;
  $("isl-down").textContent = nDown;
  $("isl-dead").textContent = nDead;
  $("isl-near").innerHTML = near == null ? '最近 <b>--</b>' : '最近 <b>' + near.toFixed(0) + "m</b>";
  $("c-alive").textContent = nAlive;
  $("c-dead").textContent = nDead;
  $("c-box").textContent = nB;
  $("c-ai").textContent = nA;
  // AI 格常态是「数得到、看不见」的困惑源（AI 默认关）：关着就画成暗态，
  // 点一下即开（绑定见 js4），不必去翻设置。
  $("c-ai").parentElement.classList.toggle("off", !PREF.ai);
  $("c-team").textContent = nM + nSelf;
  const il = $("island");
  il.classList.toggle("replay", !live);
  il.classList.toggle("stale", stale);
  const stMap = { offline:"未连接", waiting:"等待流量", detecting:"识别对局中", error:"解析异常" };
  const foot = (stMap[s.status] || (live ? "实时解析" : "回放")) + " · " + (s.flow || "识别中…");
  if ($("isl-foot").textContent !== foot) $("isl-foot").textContent = foot;
}

/* ------------------------------------------------------------ 干员台 */
// 呈现哲学：人不是一张平表，而是「按队伍成组、组内按距离升序」。
// 一行只给三件事：是谁（色+名+干员）、什么状态（徽标/血条）、多远（大字距离）。
function threatSort(a, b) {
  const da = dist2(a), db = dist2(b);
  if (da == null && db == null) return 0;
  if (da == null) return 1;
  if (db == null) return -1;
  return da - db;
}
function grpOf(e) {
  // 自己与队友同属「我方」组（组头取自己色）；组内再按绿/蓝区分
  if (e.kind === "self" || e.kind === "mate") return { id:"ally", nm:"我方", c:C_SELF, o:0 };
  if (e.kind === "box")  return { id:"box",  nm:"死亡盒", c:C_BOX, o:8 };
  if (e.kind === "ai")   return { id:"ai",   nm:"AI 单位", c:C_AI, o:7 };
  // 敌队一律同权重 o=2：组间顺序完全交给「最近距离」决定（威胁优先），
  // 否则队号会盖过距离，出现 5队165m 排在 3队94m 前面的反直觉排序。
  if (e.team > 0) return { id:"t" + e.team, nm:"第 " + e.team + " 队", c:TEAM_COLORS[(e.team-1)%TEAM_COLORS.length], o:2 };
  return { id:"unk", nm:"未知队伍", c:C_UNK, o:6 };
}
function opRow(e) {
  // 自己的相对距离恒为 0，显示成 -- 免得看着像贴脸的敌人
  const isSelf = e.kind === "self";
  const col = entColor(e), d = isSelf ? null : dist2(e), z = isSelf ? null : dz(e);
  const st = e.status_key;
  const isBox = e.kind === "box";
  const badges = [];
  // 盒子的「已搜空 / 搜刮中 / 搬运中」单独占一行，不跟名称挤在一起
  const boxState = (isBox && st && STATE_TXT[st])
    ? '<span class="badge" style="--b:' + STATE_COL[st] + '">' + STATE_TXT[st] + "</span>" : "";
  if (st && STATE_TXT[st] && !isBox) badges.push('<span class="badge" style="--b:' + STATE_COL[st] + '">' + STATE_TXT[st] + "</span>");
  else if (e.dead && !isBox) badges.push('<span class="badge ghost" style="--b:' + C_BOX + '">阵亡</span>');
  // 贴脸徽章常驻占位：显隐交给 syncOpLive，避免阈值边界抖动触发整块重建
  badges.push('<span class="badge" data-alert="1" style="--b:var(--danger)" hidden>贴脸</span>');
  if (e._out_of_range) badges.push('<span class="badge ghost" style="--b:var(--warn)">超距·最后位置</span>');
  if (e.is_bot && !e.hero) badges.push('<span class="badge bot-confirmed" title="PlayerState 多字段一致确认">人机</span>');
  // 出生点标记（PVE AI 没有槽位轨迹，坐标只能来自出生点）：必须标出来，
  // 否则用户会当成实时位置。带龄，越旧越不可信。
  // 年龄随时间增长，不能进 html 指纹；此处只放占位，文本由 syncOpLive 刷新
  if (e.spawn_mark) badges.push('<span class="badge ghost" style="--b:var(--txt-3)">出生点'
    + (e.age_sec != null ? ' <i class="sp-age" data-age="1"></i>' : "") + "</span>");
  const gear = [];
  if (e.helmet) gear.push(gearIconHtml("helmet", e.helmet, gearValue(e.helmet_dur, e.helmet)));
  if (e.vest) gear.push(gearIconHtml("armor", e.vest, gearValue(e.vest_dur, e.vest)));
  const w = (e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon));
  const meta = [];
  if (gear.length) meta.push('<span class="gear-inline">' + gear.join("") + "</span>");
  if (w) meta.push('<span class="wp">' + esc(w) + "</span>");
  if (e.slot != null && !meta.length) meta.push("槽 " + e.slot);
  // 干员台专属：背包/胸挂显示名（1108/1107 段，liveview GEAR_NAMES 解析；
  // user 定版 0831 只上干员台，不进气泡/头顶信息条/地图）。单独一行——
  // .op-m nowrap+hidden，挤进装备行会被右侧裁切
  const bpCr = [];
  if (e.bp) bpCr.push(esc(e.bp));
  if (e.cr) bpCr.push(esc(e.cr));
  const disp = e.hero || e.name || KIND_NM[e.kind] || "未知";
  const sub = (e.hero && e.name && e.hero !== e.name) ? '<span class="hero">' + esc(e.name) + "</span>" : "";
  // 血量：有数据就画条；部分同步时用范围表达不确定性。
  const showHp = !isBox && !e.dead && e.hp != null;   // 血量进单独容器，避免每帧重绘整块
  const details = [];
  if (boxState) details.push(boxState);
  details.push(...badges);
  details.push(...meta);
  if (bpCr.length) details.push('<span class="bags">' + bpCr.join(" · ") + "</span>");
  return '<div class="op' + (sel === e.key ? " sel" : "") + (e.dead ? " dead" : "")
    + (isBox && sel === e.key ? " expanded" : "")
    + (e._out_of_range ? " out-range" : "")
    + '" data-k="' + esc(e.key) + '">'
    + '<div class="op-av' + (isBox ? " box" : "") + '" style="--c:' + col + '"'
    + (e.hid ? ' title="干员ID ' + esc(String(e.hid)) + '"' : "") + '>'
    + (isBox ? "" : esc(initials(disp)))
    + (!isBox && e.hero ? '<img alt="" src="' + avatarUrl(e.hero)
        + '" onerror="this.remove()">' : "")
    + (st && STATE_COL[st] ? '<i class="op-ring" style="--s:' + STATE_COL[st] + '"></i>' : "")
    + "</div>"
    + '<div class="op-col">'
    +   '<div class="op-n"><span class="op-title">' + esc(disp) + sub + "</span></div>"
    +   (details.length ? '<div class="op-m">' + details.join('<i class="op-sep">·</i>') + "</div>" : "")
    +   (showHp ? '<div class="op-hp-live" data-hp="1"></div>' : "")
    +   (isBox && sel === e.key ? boxContentsHtml(e) : "")
    + "</div>"
    + '<div class="op-r"><div class="op-d tnum" data-d="1"><span class="dv">--</span><span class="du" hidden>m</span></div>'
    + (z != null ? '<div class="op-z tnum" data-z="1"></div>' : "")
    + "</div></div>";
}
function lootDistanceHtml(e) {
  const d = dist2(e), z = dz(e);
  return (Number.isFinite(d) ? d.toFixed(0) + "m" : "--")
    + (Number.isFinite(z) && Math.abs(z) >= 3 ? '<small>' + (z > 0 ? '↑ ' : '↓ ') + Math.abs(z).toFixed(0) + 'm</small>' : '');
}
function lootRow(e) {
  const col = GRADE_COL[e.grade] || "var(--unk)";
  const count = lootCount(e), value = lootValue(e);
  const detail = ['<span>' + (GRADE_NM[e.grade] || "品质未知") + '</span>'];
  detail.push('<span>' + (value.total != null ? "参考总价 " + lootMoney(value.total) :
    value.unit != null ? "单件参考 " + lootMoney(value.unit) : "参考价未知") + '</span>');
  if (count == null) detail.push('<span>数量未知</span>');
  if (e.source_type === 2) detail.push('<span>丢弃物</span>');
  if (e.pickup_status === "candidate") detail.push('<span class="loot-state">疑似拾取</span>');
  if (e.channel_state === "irrelevant") detail.push('<span>最后已知位置</span>');
  return '<div class="loot-row' + (sel === e.key ? " sel" : "") + '" data-k="' + esc(e.key) + '">'
    + lootThumb(e) + '<div class="loot-col"><span class="loot-n" style="color:' + col + '">'
    + '<span class="loot-name">' + esc(lootName(e)) + '</span>'
    + (count != null ? '<span class="loot-qty">×' + count + '</span>' : '') + '</span>'
    + '<span class="loot-m">' + detail.join('') + '</span></div>'
    + '<div class="loot-d tnum">' + lootDistanceHtml(e) + '</div></div>';
}
function renderLootRows(list, allLoot) {
  const body = $("loot-body");
  // 空态：物资关闭 / 筛选无结果 / 暂无。把文案当缓存键，变化时才重建。
  const empty = list.length ? null
    : !PREF.loot ? ["物资显示已关闭", "打开物资开关即可查看"]
    : allLoot.length ? ["当前品质筛选无结果", "可在设置中降低门槛或选择全部"]
    : ["暂无露天物资", "发现物资后显示，已确认移除的物资会自动消失"];
  const kind = empty ? "empty:" + empty[0] : "loot";
  if (body.dataset.renderKind !== kind) {
    body.replaceChildren(); lootRows.clear(); body.dataset.renderKind = kind;
    if (empty) { body.innerHTML = emptyHtml(empty[0], empty[1]); return; }
  }
  if (empty) return;
  const seen = new Set();
  let cursor = body.firstElementChild;
  for (const e of list) {
    seen.add(e.key);
    // Movement changes distance, not the thumbnail/name/quantity DOM.
    const html = lootRow({...e, rel:null});
    let row = lootRows.get(e.key);
    if (!row || row.html !== html) {
      const box = document.createElement("div"); box.innerHTML = html;
      const fresh = box.firstElementChild;
      if (!row) { row = {node:fresh}; lootRows.set(e.key,row); }
      else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
      row.html = html; row.distance = null;
    }
    const distance = lootDistanceHtml(e);
    if (row.distance !== distance) {
      row.node.querySelector(".loot-d").innerHTML = distance; row.distance = distance;
    }
    if (row.node !== cursor) body.insertBefore(row.node,cursor);
    cursor = row.node.nextElementSibling;
  }
  for (const [key,row] of lootRows) if (!seen.has(key)) { row.node.remove(); lootRows.delete(key); }
}
// 自己不在后端 entities 里（作为顶层 self 字段单独输出），这里补一条并入清单。
function selfAsOp(s) {
  if (!s.self) return null;
  const life = s.self_life;
  const ownState = life && life.dead ? "dead" : life && life.downed ? "down" : null;
  return { key:"__self", kind:"self", world:s.self, rel:[0,0,0], yaw:s.self_yaw || 0,
           name:s.self_name || "自己", hero:s.self_hero || null,
           status_key:ownState, status:STATE_TXT[ownState] || null,
           hp:s.self_hp, dead:!!(life && life.dead) };
}
function drawRoster(s, ents) {
  const ops = ents.filter(e => e.world && visible(e)
    && (e.kind === "player" || e.kind === "ai" || e.kind === "mate" || e.kind === "box"));
  const selfOp = selfAsOp(s);
  if (selfOp) ops.unshift(selfOp);
  const allLoot = ents.filter(lootAvailable), loot = allLoot.filter(lootVisible);
  $("n-ops").textContent = ops.length;
  $("n-loot").textContent = loot.length;
  const nEnemy = ops.filter(e => e.kind === "player" && !e.dead).length;
  const opsHead = nEnemy ? nEnemy + " 名敌人在场" : (ops.length ? ops.length + " 个目标" : "暂无目标");
  const lootHead = !PREF.loot ? "物资关闭"
    : loot.length ? loot.length + " 处物资"
    : allLoot.length ? "共 " + allLoot.length + " 处 · 筛选无结果" : "暂无物资";
  const head = opsHead + " · " + lootHead;
  if (_rc.head !== head) { $("pan-sub").textContent = head; _rc.head = head; }
  $("loot-filter-hint").textContent = lootFilterName();

  // 上区：干员（威胁 / 队伍两种读法）。展开成分组布局后增量复用节点，
  // 不再整块 innerHTML 重建 —— 重建会替换元素本身，打断鼠标下的 :hover 与过渡（表现为闪）。
  const layout = [];
  if (PREF.sort === "team") {
    // 按队伍成组：自己 → 我方 → 各敌队（按最近距离升序） → 未知 → AI → 盒子
    const groups = new Map();
    for (const e of ops) {
      const g = grpOf(e);
      if (!groups.has(g.id)) groups.set(g.id, { g: g, items: [] });
      groups.get(g.id).items.push(e);
    }
    const arr = [...groups.values()];
    for (const x of arr) {
      x.items.sort(threatSort);
      x.near = x.items.reduce((m, e) => { const d = dist2(e); return (d != null && (m == null || d < m)) ? d : m; }, null);
    }
    arr.sort((a, b) => a.g.o - b.g.o || (a.near ?? 1e9) - (b.near ?? 1e9));
    for (const x of arr) {
      const fold = foldGrp.has(x.g.id);
      layout.push({
        id: x.g.id,
        headHtml: '<div class="grp' + (fold ? " fold" : "") + '" data-g="' + x.g.id + '">'
          + '<i class="grp-bar" style="--c:' + x.g.c + '"></i>'
          + '<span class="grp-name">' + x.g.nm + "</span>"
          + '<span class="grp-cnt tnum">' + x.items.length + "</span>"
          + '<span class="grp-sp"></span>'
          + (x.near != null ? '<span class="grp-near tnum">最近 ' + x.near.toFixed(0) + "m</span>" : "")
          + '<svg class="grp-car" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></div>',
        itemsClass: "grp-items",
        rows: x.items.map(e => ({ key: e.key, html: opRow(e) })),
      });
    }
  } else {
    // 纯威胁排序：贴脸的永远在最上面，最直接的战术读法
    const list = ops.slice().sort((a, b) => {
      const pa = (a._alert ? 0 : 1), pb = (b._alert ? 0 : 1);
      if (pa !== pb) return pa - pb;
      const ka = a.kind === "box" ? 1 : 0, kb = b.kind === "box" ? 1 : 0;
      if (ka !== kb) return ka - kb;
      return threatSort(a, b);
    });
    layout.push({ id: "flat", headHtml: null, itemsClass: null, rows: list.map(e => ({ key: e.key, html: opRow(e) })) });
  }
  renderOpRows(layout);
  syncOpLive(ops);

  // 下区：物资（同一份快照，增量渲染，距离单独更新）
  renderLootRows(loot.slice().sort(lootSort), allLoot);
}
/* 干员台增量渲染（keyed 复用）。
   内容或顺序变化时只更新受影响的节点，绝不整块 innerHTML 重建 ——
   重建会替换元素本身，导致鼠标下的 :hover 与 CSS 过渡被打断（视觉上就是卡片闪）。
   布局：每组 = 组头(.grp) + 行容器(.grp-items)；威胁模式是单个无组头的扁平分组。 */
function htmlToNode(html) {
  const box = document.createElement("div");
  box.innerHTML = html;
  return box.firstElementChild;
}
function renderOpRows(layout) {
  const body = $("ops-body");
  if (!body) return;
  const empty = !layout.length || layout.every(g => !g.rows.length);
  if (empty) {
    if (body.dataset.renderKind !== "empty") {
      body.replaceChildren(); opRows.clear(); body.dataset.renderKind = "empty";
      body.innerHTML = emptyHtml("暂无目标", "等待对局数据");
    }
    return;
  }
  if (body.dataset.renderKind !== "static") {
    body.replaceChildren(); opRows.clear(); body.dataset.renderKind = "static";
  }
  const seen = new Set();
  let cursor = body.firstElementChild;
  for (const grp of layout) {
    if (grp.headHtml != null) {
      const key = "h:" + grp.id;
      seen.add(key);
      let row = opRows.get(key);
      if (!row || row.html !== grp.headHtml) {
        const fresh = htmlToNode(grp.headHtml);
        if (!row) { row = { node: fresh }; opRows.set(key, row); }
        else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
        row.html = grp.headHtml;
      }
      if (row.node !== cursor) body.insertBefore(row.node, cursor);
      cursor = row.node.nextElementSibling;
    }
    const cKey = "c:" + grp.id;
    seen.add(cKey);
    let holder = opRows.get(cKey);
    if (!holder) {
      holder = { node: document.createElement("div") };
      if (grp.itemsClass) holder.node.className = grp.itemsClass;
      opRows.set(cKey, holder);
    }
    if (holder.node !== cursor) body.insertBefore(holder.node, cursor);
    cursor = holder.node.nextElementSibling;
    let inner = holder.node.firstElementChild;
    for (const item of grp.rows) {
      seen.add(item.key);
      let row = opRows.get(item.key);
      if (!row || row.html !== item.html) {
        const fresh = htmlToNode(item.html);
        if (!row) { row = { node: fresh }; opRows.set(item.key, row); }
        else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
        row.html = item.html;
      }
      if (row.node !== inner) holder.node.insertBefore(row.node, inner);
      inner = row.node.nextElementSibling;
    }
  }
  // 本帧未安排的节点（离场实体、消失的分组、切换排序后的旧容器）连同池条目一起清掉
  for (const [key, row] of [...opRows]) {
    if (!seen.has(key)) { row.node.remove(); opRows.delete(key); }
  }
}
/* 距离/高度差单独刷新：不进 html 指纹，避免每跳 1 米就重建整块列表 */
function syncOpLive(ops) {
  const body = $("ops-body");
  if (!body) return;
  const rows = new Map();
  for (const row of body.querySelectorAll(".op")) rows.set(row.dataset.k, row);
  for (const e of ops) {
    const row = rows.get(e.key);
    if (!row) continue;
    const isSelf = e.kind === "self";
    const d = isSelf ? null : dist2(e), z = isSelf ? null : dz(e);
    const dEl = row.querySelector(".op-d");
    if (dEl) {
      const dtxt = d != null ? String(d.toFixed(0)) : "--";
      if (dEl._v !== dtxt) {
        // 结构固定（.dv 数值 + .du 单位），只改文本，避免每次重建内部节点
        const dv = dEl.querySelector(".dv"), du = dEl.querySelector(".du");
        if (dv) dv.textContent = dtxt;
        if (du) du.hidden = d == null;
        dEl._v = dtxt;
      }
      const dcol = e._alert ? "var(--danger)" : "var(--txt)";
      if (dEl._c !== dcol) { dEl.style.color = dcol; dEl._c = dcol; }
    }
    // 贴脸徽章与出生点年龄同样不进 html 指纹（随阈值/时间变化），在此单独刷新
    const alEl = row.querySelector("[data-alert]");
    if (alEl) alEl.hidden = !e._alert;
    const agEl = row.querySelector(".sp-age");
    if (agEl && e.age_sec != null) {
      const atxt = agoTxt(e.age_sec);
      if (agEl._v !== atxt) { agEl.textContent = atxt; agEl._v = atxt; }
    }
    const hpBox = row.querySelector("[data-hp]");
    if (hpBox && e.hp) {
      const hst = hpStat(e.hp);
      let hh = "";
      if (hst && hst.known && hst.frac < 1) {
        hh += '<div class="op-m"><span class="hpv" style="color:' + hpCol(hst.frac) + '">血 '
          + hpPct(e.hp) + "</span>"
          + (hst.worst && hst.worst.f < 1
             ? '<span style="color:var(--txt-3)"> ' + HP_NM[hst.worst.k] + " "
               + Math.round(hst.worst.cur) + "/" + Math.round(hst.worst.mx) + "</span>" : "")
          + "</div>";
      }
      hh += hpBarHtml(e.hp, "op-hp");
      if (hpBox._v !== hh) { hpBox.innerHTML = hh; hpBox._v = hh; }
    }
    const zEl = row.querySelector(".op-z");
    if (zEl && z != null) {
      const ztxt = (z > 0 ? "↑" : z < 0 ? "↓" : "") + Math.abs(z).toFixed(0) + "m";
      if (zEl._v !== ztxt) { zEl.textContent = ztxt; zEl._v = ztxt; }
      zEl.className = "op-z tnum " + (z > 3 ? "up" : z < -3 ? "dn" : "");
    }
  }
}
function emptyHtml(t, s) {
  return '<div class="empty"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
    + '<path d="M12 8v4M12 16h.01"/></svg>' + t + "<br><span style=\"opacity:.6\">" + s + "</span></div>";
}

/* -------------------------------------------------------------- 敌人连线 */
/* 从屏幕上方中心点连到每个敌人的头部。颜色 = 该敌人的队伍颜色（就是 3D 标签
   自己的 color，与人物标记同色），所以一队一色，能一眼看出连的是哪一队。
   只认敌队玩家的标签（r3d 在标签上打了 data-foe），开关关闭或非 3D 时清空。
   每帧跟随标签位置重画。 */
function updateRays() {
  const svg = $("ray-layer");
  if (!svg) return;
  // 用 body 的类判断，而不是 is3d()：后者是 const 且定义在本函数之后，
  // 而下面的 IIFE 首帧就会执行到这里，引用它会命中 TDZ 并中断整个脚本。
  if (!PREF.ray || !document.body.classList.contains("mode3d")) {
    if (svg.childElementCount) svg.replaceChildren();
    return;
  }
  const box = svg.getBoundingClientRect();
  const sx = Math.round(box.width / 2), sy = 0;
  const pts = [];
  // data-foe 而不是 .foe：连线的开与关只该由「敌人连线」决定，
  // 不该顺手被「敌人标识」联动（之前关掉敌人标识，连线会一起消失）。
  for (const lbl of document.querySelectorAll('#r3-lbl .r3-lbl[data-foe="1"]')) {
    const r = lbl.getBoundingClientRect();
    if (r.width <= 0 || r.bottom < 0 || r.top > innerHeight) continue;
    // _placeLabel 把队伍色写在 style.color 上，直接拿来当线色
    pts.push([Math.round(r.left + r.width / 2 - box.left), Math.round(r.bottom - box.top),
              lbl.style.color || "var(--danger)"]);
  }
  while (svg.childElementCount > pts.length) svg.lastElementChild.remove();
  while (svg.childElementCount < pts.length) {
    svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "line"));
  }
  for (let i = 0; i < pts.length; i++) {
    const ln = svg.children[i];
    ln.setAttribute("x1", sx); ln.setAttribute("y1", sy);
    ln.setAttribute("x2", pts[i][0]); ln.setAttribute("y2", pts[i][1]);
    // 用内联 style 而不是属性：样式表里的 #ray-layer line 规则优先级低于内联，
    // 但要压掉原来写死的红色，只有内联（或改样式表）才稳。
    if (ln.style.stroke !== pts[i][2]) ln.style.stroke = pts[i][2];
  }
}
(function rayTick() { updateRays(); requestAnimationFrame(rayTick); })();

/* -------------------------------------------------------------- 回放轨 */
function drawPlaybar(s) {
  const live = !s.replay;
  $("playbar").classList.toggle("hide", !!live);
  if (live) return;
  const c = s.cursor || {};
  if (!seeking && c.lo != null && c.hi != null && c.hi > c.lo && c.seq != null) {
    $("seek").value = Math.round(1000 * (c.seq - c.lo) / (c.hi - c.lo));
  }
  if (s.replay) {
    document.querySelectorAll("#spd button").forEach(b => b.classList.toggle("on", Number(b.dataset.spd) === s.replay.speed));
    playing = !s.replay.paused;
    $("ic-play").innerHTML = playing ? IC_PAUSE : IC_PLAY;
  }
  let lbl = "--:-- / --:--";
  if (c.lo != null && c.hi != null && c.seq != null && c.t_lo != null && c.t_hi != null) {
    const f = c.hi > c.lo ? (c.seq - c.lo) / (c.hi - c.lo) : 0;
    const dur = c.t_hi - c.t_lo;
    lbl = fmtT(f * dur) + " / " + fmtT(dur);
  }
  if (s.replay?.indexing) lbl = "正在索引完整记录…";
  else if (s.replay?.seeking) lbl = "正在重建人物关系… " + lbl;
  if ($("tlabel").textContent !== lbl) $("tlabel").textContent = lbl;
}

/* -------------------------------------------------------------- 选中/定位 */
function clearPick() { if (pickMarker) { pickMarker.remove(); pickMarker = null; } }
function focusEntity(key) {
  const s = lastSnap; if (!s) return;
  // 点自己 = 跟随镜头（自己不在 entities 里，单独走一条分支）。
  // 3D 下点自己直接切到第三跟随；2D 下走视图跟随。
  if (key === "__self") {
    followKey = "__self";
    if (typeof R3 !== "undefined" && R3 && R3.setFollowKey) R3.setFollowKey("__self");
    setFollow(true);   // 2D 视图与小地图都跟随自己（3D 下小地图同样靠它驱动 panTo）
    if (is3d() && R3) {
      R3.setCam("chase");
      setPref("cam3d", "chase");
      r3CamSync();
    } else if (s.self) {
      const ll = pointLatLng(s.self), mb = MAP_INFO.bounds;
      const lat = Math.min(Math.max(ll.lat, Math.min(mb[0][0], mb[1][0])), Math.max(mb[0][0], mb[1][0]));
      const lng = Math.min(Math.max(ll.lng, Math.min(mb[0][1], mb[1][1])), Math.max(mb[0][1], mb[1][1]));
      map.setView([lat, lng], Math.min(Math.max(map.getZoom(), 5), map.getMaxZoom()), { animate: true });
    }
    toast("镜头跟随自己");
    if (MOBILE()) closeRoster();
    return;
  }
  const e = prepareLootEntities(s.entities || []).find(x => x.key === key && visible(x));
  sel = (sel === key || !e) ? null : key;
  _rc.roster = ""; _rc.icon.clear();
  clearPick();
  if (!sel || !e || !e.world) {
    followKey = "__self";
    if (typeof R3 !== "undefined" && R3) R3.setFollowKey("__self");
    if (lastSnap) draw(lastSnap); return;
  }
  // 点中谁就跟随谁；再点一次取消，回到跟自己
  followKey = key;
  if (typeof R3 !== "undefined" && R3 && R3.setFollowKey) R3.setFollowKey(key);
  setFollow(true);
  const ll = pointLatLng(e.world);
  // 视图目标夹进地图 bounds：原点漂移局不把视图甩进无瓦片黑屏区
  const mb = MAP_INFO.bounds;
  const lat = Math.min(Math.max(ll.lat, Math.min(mb[0][0], mb[1][0])), Math.max(mb[0][0], mb[1][0]));
  const lng = Math.min(Math.max(ll.lng, Math.min(mb[0][1], mb[1][1])), Math.max(mb[0][1], mb[1][1]));
  map.setView([lat, lng], Math.min(Math.max(map.getZoom(), 5), map.getMaxZoom()), { animate: true });
  // The existing marker owns selection; no separate item overlay can outlive it.
  draw(lastSnap);
  if (typeof r3Focus === "function") r3Focus(sel);
  if (MOBILE()) closeRoster();
}

/* ------------------------------------------------------------ 地图初始化 */
function setMap(key, quiet) {
  const m = MAPS[key] || Object.values(MAPS)[0];
  if (!m) return;
  MAP_INFO = m;
  setPref("mapkey", key);
  const mb = L.latLngBounds(m.bounds[0], m.bounds[1]);
  if (tileLayer) { map.removeLayer(tileLayer); tileLayer = null; }
  tileLayer = L.tileLayer(m.tileUrl, { tileSize: m.tileSize || 256,
    maxNativeZoom: m.maxNativeZoom || 4, bounds: mb, keepBuffer: 3 }).addTo(map);
  map.setMaxBounds(mb.pad(0.25));
  map.fitBounds(mb);
  // 切图：平面区间变了，全部缓存清空强制重算，否则旧 marker 卡在上一张图坐标
  for (const k in _rc) if (_rc[k] instanceof Map) _rc[k].clear();
  _rc.roster = ""; _rc.isl = ""; _rc.head = "";
  for (const t of trails.values()) t.remove(); trails.clear();
  for (const g of tags.values()) g.remove(); tags.clear();
  for (const h of hpbars.values()) h.remove(); hpbars.clear();
  for (const g of gearbars.values()) g.remove(); gearbars.clear();
  for (const f of foebars.values()) f.remove(); foebars.clear();
  for (const mk of markers.values()) mk.remove(); markers.clear();
  smPos.clear(); smTgt.clear();
  if (selfMarker) { selfMarker.remove(); selfMarker = null; }
  clearPick();
  $("map-now").textContent = m.name;
  document.querySelectorAll("#maps .mcard").forEach(c => c.classList.toggle("on", c.dataset.k === key));
  if (!quiet) toast("已切换到 " + m.name);
  if (lastSnap) draw(lastSnap);
  if (typeof r3SwitchMap === "function") r3SwitchMap();
}
// 地图卡片缩略图：取瓦片金字塔第 1 层（2x2）的右下块——比 (0,0) 角块更靠近
// 图幅主体（角块常是纯地形/纯海），一眼能认出是哪张图。
function mapThumb(m) {
  return String(m.tileUrl).replace("{z}", "1").replace("{x}", "1").replace("{y}", "1");
}
fetch("/api/map").then(r => r.json()).then(info => {
  const list = (info && info.maps) || [];
  DEF_MAP = (info && info.default) || "daba";
  for (const m of list) MAPS[m.key] = m;
  map = L.map("map", { crs: L.CRS.Simple, minZoom: 2, maxZoom: 7, preferCanvas: true,
    attributionControl: false, zoomControl: false, zoomSnap: .25, wheelPxPerZoomLevel: 90 });
  L.control.zoom({ position: "bottomright" }).addTo(map);
  $("maps").innerHTML = list.map(m =>
    '<div class="mcard" data-k="' + esc(m.key) + '">'
    + '<img loading="lazy" src="' + esc(mapThumb(m)) + '" onerror="this.style.display=\'none\'">'
    + '<div class="now"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></div>'
    + "<b>" + esc(m.name) + "</b></div>").join("");
  $("maps").addEventListener("click", ev => {
    const c = ev.target.closest(".mcard");
    if (c) { setMap(c.dataset.k); closeSheet(); }
  });
  const want = (PREF.mapkey && MAPS[PREF.mapkey]) ? PREF.mapkey : DEF_MAP;
  setMap(want, true);
  map.on("dragstart", () => { if (follow) setFollow(false); });
  poll();
});

/* Local diagnostics distinguish source samples, snapshots and rendered frames. */
let gwPageFrames=0,gwUiSamples=0,gwPageFps=0,gwSnapshotHz=0,gwRateAt=performance.now();
function gwFrame(){gwPageFrames++;requestAnimationFrame(gwFrame);}requestAnimationFrame(gwFrame);
async function gwStatus(){
  const now=performance.now(),dt=Math.max(.1,(now-gwRateAt)/1000);
  gwPageFps=gwPageFrames/dt;gwSnapshotHz=gwUiSamples/dt;gwPageFrames=gwUiSamples=0;gwRateAt=now;
  try {
    const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);
    const s=await response.json(),r=s.rates||{},c=s.counts||{},t=s.transport||{},p=s.stats||{};
    $('gateway-rate').textContent='人物坐标 '+Number(r.characters||0).toFixed(1)+' 样本/秒 · '+(s.replay?.paused?'已暂停':s.replay?'离线回放':'实时解析');
    const render=window.gateway3d?.stat();
    $('gateway-stats').textContent=[
      '人物含待核实标记；自身姿态 '+Number(r.self||0).toFixed(1)+' Hz',
      '快照 '+gwSnapshotHz.toFixed(1)+' Hz · '+(is3d()?'3D渲染 '+Number(render?.fps||0).toFixed(1):'页面动画 '+gwPageFps.toFixed(1))+' FPS',
      '人物 '+(c.characters||0)+'（含自身 '+(c.charactersIncludingSelf||0)+'） · 确认人机 '+(c.confirmedBots ?? c.suspectedBots ?? 0)+' · AI单位 '+(c.ai||0)+' · 物资 '+(c.loot||0),
      is3d()?'3D人物 '+(render?.players||0)+' / 应显示 '+(render?.expectedCharacters||0)+' · 未标记人机 '+(render?.humanPlayers||0)+' · 地图 '+Number((render?.tris||0)/1000000).toFixed(2)+'M 面':'2D视图',
      '死亡盒 '+(c.box||0)+' · 容器 '+(c.container||0)+' · 物资ID待识别 '+(s.unknownLoot||0),
      'UDP 上行 '+(t.udpUpPackets??p.upPackets??0)+' / 下行 '+(t.udpDownPackets??p.downPackets??0),
      'Key候选 '+(t.bankCandidates||0)+' · 已验证连接 '+(t.udpBoundFlows||0),
      '输入异常 '+(p.inputErrors||0)+' · 序号缺口 '+(p.eventGaps||0)+' · 待解密 '+(t.pendingPackets||0),
      s.error||t.archiveError||t.parser?.error||'状态正常；平滑动画不会新增真实坐标样本'
    ].join('\n');
  }catch(e){$('gateway-rate').textContent='状态连接中断';}
  setTimeout(gwStatus,1000);
}gwStatus();
/* ---------------------------------------------------------------- 轮询 */
// 多用户版与 manager 的 20Hz 视图推进对齐；请求串行，避免慢网络堆积。
// 数据在变锁 50ms；静止指数回退省流量；后台标签页 1s 省电。
let lastUiErrorAt=0;
const POLL_FAST = 20, POLL_MAX = 100;
let pollIv = POLL_FAST, pollBusy = false, _lastBody = "";
async function poll() {
  let changed = false;
  if (!pollBusy) {
    pollBusy = true;
    try {
      const r = await fetch("/api/state",{cache:"no-store"});
      const s = await r.json();
      if (Number.isFinite(s.self_aim_yaw) && s.self_aim_age_ms <= 250) s.self_yaw = s.self_aim_yaw;
      s.self = normalizeSelf(s.self);   // 自机跳变过滤（2D 标记与 3D 相机共用同一份）
      const j = JSON.stringify(s);
      changed = j !== _lastBody; _lastBody = j; if(changed)gwUiSamples++;
      draw(s);
      if (R3 && is3d()) R3.update(loot3dSnapshot(s));
    } catch (e) { if (performance.now()-lastUiErrorAt>3000) { console.error("页面更新失败", e); lastUiErrorAt=performance.now(); } }
    pollBusy = false;
  }
  pollIv = document.hidden ? 1000 : (changed ? POLL_FAST : Math.min(pollIv * 1.7 + 1, POLL_MAX));
  setTimeout(poll, pollIv);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollIv = POLL_FAST; });

/* ------------------------------------------------------------ 交互装配 */
// 开关统一注册：一个 id 对应一个 PREF 键 + 若干副作用，避免到处写重复代码。
function bindToggle(ids, key, after) {
  const els = ids.map($).filter(Boolean);
  const sync = () => els.forEach(el => el.classList.toggle("on", !!PREF[key]));
  els.forEach(el => el.addEventListener("click", () => {
    setPref(key, PREF[key] ? 0 : 1);
    sync();
    if (after) after();
    if (lastSnap) draw(lastSnap);
    if (R3) { R3.setPrefs(PREF); if (lastSnap && is3d()) R3.update(loot3dSnapshot(lastSnap)); }
  }));
  sync();
}
// 需要拆点重绘的开关（过滤类）：先把该类实体全部撤下，下个 draw 自然重建
function purge() {
  for (const k in _rc) if (_rc[k] instanceof Map) _rc[k].clear();
  _rc.roster = "";
  for (const m of markers.values()) m.remove(); markers.clear();
  for (const t of trails.values()) t.remove(); trails.clear();
  for (const g of tags.values()) g.remove(); tags.clear();
  for (const h of hpbars.values()) h.remove(); hpbars.clear();
  for (const g of gearbars.values()) g.remove(); gearbars.clear();
  for (const f of foebars.values()) f.remove(); foebars.clear();
  smPos.clear(); smTgt.clear();
}
function setFollow(v) {
  follow = !!v; setPref("follow", follow ? 1 : 0);
  ["s-follow","qk-follow"].forEach(i => { const el = $(i); if (el) el.classList.toggle("on", follow); });
  if (follow) smKick();
}
$("s-follow").addEventListener("click", () => { setFollow(!follow); toast(follow ? "镜头跟随开启" : "镜头跟随关闭"); });
$("qk-follow").addEventListener("click", () => { setFollow(!follow); });
setFollow(follow);

// 功能台开关与移动端快捷条共用同一个 PREF 键，bindToggle 把所有 id 的 on 态一起同步。
bindToggle(["s-trail"], "trail", () => { if (!PREF.trail) { for (const t of trails.values()) t.remove(); trails.clear(); _rc.trail.clear(); } });
bindToggle(["s-hp"], "hp", () => { if (!PREF.hp) { for (const h of hpbars.values()) h.remove(); hpbars.clear(); _rc.hp.clear(); } });
bindToggle(["s-loot","qk-loot"], "loot", purge);
bindToggle(["s-box"], "box", purge);
bindToggle(["s-container"], "container", purge);
bindToggle(["s-aibox"], "aibox", purge);
bindToggle(["s-ai"], "ai", () => { purge(); toast(PREF.ai ? "AI 单位已显示（出生点情报）" : "AI 单位已隐藏"); });
bindToggle(["s-mate"], "mate", purge);
bindToggle(["s-foe"], "foe", purge);
bindToggle(["s-ray"], "ray");   // 连线每帧自取自清，不需要重建标记
// 人物方框：方框在 r3d 里是常驻的 LineSegments，开关只切 visible，所以这里
// 同样不用重建标记 —— bindToggle 末尾那次 R3.setPrefs + R3.update 就是即时生效。
bindToggle(["s-box3d"], "box3d");
// 屏幕外预警：箭头同样在 r3d 里每帧自取自清。距离用自己的「预警距离」
// （PREF.warnd），和「贴脸高亮」解耦 —— 后者只管箭头前方那个贴上来的小红点。
// 距离 = 0 时开了也不会有箭头，那种"开了没反应"最容易被当成 bug，直接提示。
bindToggle(["s-warn3d"], "warn3d", () => {
  if (PREF.warn3d && !(Number(PREF.warnd) > 0))
    toast("屏幕外预警已开，但「预警距离」是 0 —— 先给它设个距离");
});
bindToggle(["s-name"], "name", () => _rc.tag.clear());
bindToggle(["s-wpn"], "wpn", () => _rc.tag.clear());
bindToggle(["s-gear"], "gear", () => { _rc.tag.clear(); if (!PREF.gear) { for (const g of gearbars.values()) g.remove(); gearbars.clear(); } });
bindToggle(["s-dist"], "dist", () => _rc.tag.clear());
bindToggle(["s-cone"], "cone", () => _rc.icon.clear());
// 战况岛「AI」格直接当开关用：数得到却看不见是最容易被当成 bug 的体验，
// 一击即开，省掉「找设置」这一步。
{
  const cell = $("c-ai") && $("c-ai").parentElement;
  if (cell) {
    cell.title = "点击切换 AI 单位显示";
    cell.addEventListener("click", (ev) => { ev.stopPropagation(); $("s-ai").click(); });
  }
}

/* --- 功能台 / 干员台开合（两舷对称，移动端互斥） --- */
function openRoster() {
  if (MOBILE()) closeDock();
  $("roster").classList.remove("hide");
  document.body.classList.remove("roster-hidden");
  if (MOBILE()) { $("roster").classList.add("up"); $("veil").classList.add("on"); document.body.classList.add("sheeting"); }
  ["s-roster","qk-roster"].forEach(i => { const el = $(i); if (el) el.classList.add("on"); });
}
function closeRoster() {
  $("roster").classList.remove("up");
  if (!MOBILE()) { $("roster").classList.add("hide"); document.body.classList.add("roster-hidden"); }
  if (MOBILE()) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
  ["s-roster","qk-roster"].forEach(i => { const el = $(i); if (el) el.classList.remove("on"); });
}
function rosterOpen() { return MOBILE() ? $("roster").classList.contains("up") : !$("roster").classList.contains("hide"); }
function toggleRoster() { rosterOpen() ? closeRoster() : openRoster(); }

function openDock() {
  if (MOBILE()) closeRoster();
  $("dock").classList.remove("hide");
  document.body.classList.remove("dock-hidden");
  if (MOBILE()) { $("dock").classList.add("up"); $("veil").classList.add("on"); document.body.classList.add("sheeting"); }
}
function closeDock() {
  $("dock").classList.remove("up");
  if (!MOBILE()) { $("dock").classList.add("hide"); document.body.classList.add("dock-hidden"); }
  if (MOBILE()) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
}
function dockOpen() { return MOBILE() ? $("dock").classList.contains("up") : !$("dock").classList.contains("hide"); }
function toggleDock() { dockOpen() ? closeDock() : openDock(); }

$("s-roster").addEventListener("click", toggleRoster);
$("qk-roster").addEventListener("click", toggleRoster);
$("btn-roster-close").addEventListener("click", closeRoster);
$("roster-handle").addEventListener("click", toggleRoster);
$("btn-dock-close").addEventListener("click", closeDock);
$("dock-handle").addEventListener("click", toggleDock);
$("qk-set").addEventListener("click", toggleDock);
// 桌面两舷默认展开（战术信息是主体），移动端默认收起（屏幕小，地图优先）
if (MOBILE()) { $("roster").classList.remove("hide"); closeRoster(); closeDock(); } else openRoster();

/* --- 排序 / 行点击 --- */
$("sortbar").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-sort]"); if (!b) return;
  setPref("sort", b.dataset.sort);
  document.querySelectorAll("#sortbar .mini-tab").forEach(x => x.classList.toggle("on", x === b));
  _rc.roster = "";
  if (lastSnap) draw(lastSnap);
});
document.querySelectorAll("#sortbar .mini-tab").forEach(x => x.classList.toggle("on", x.dataset.sort === PREF.sort));
$("loot-sortbar").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-loot-sort]"); if (!b) return;
  setPref("lootsort", b.dataset.lootSort);
  document.querySelectorAll("#loot-sortbar button").forEach(x => x.classList.toggle("on", x === b));
  lootRows.clear(); $("loot-body").dataset.renderKind = "";
  if (lastSnap) draw(lastSnap);
});
document.querySelectorAll("#loot-sortbar button").forEach(x => x.classList.toggle("on", x.dataset.lootSort === PREF.lootsort));
$("roster").addEventListener("click", ev => {
  const g = ev.target.closest(".grp");
  if (g) { const id = g.dataset.g; foldGrp.has(id) ? foldGrp.delete(id) : foldGrp.add(id);
    _rc.roster = ""; if (lastSnap) draw(lastSnap); return; }
  const row = ev.target.closest("[data-k]");
  if (row) focusEntity(row.dataset.k);
});

/* --- 浮层（地图） --- */
let openedSheet = null;
function openSheet(id) {
  if (openedSheet === id) return closeSheet();
  closeSheet();
  openedSheet = id;
  $(id).classList.add("on");
  // 桌面：浮层是侧边抽屉而非模态。移动端：真模态，压暗背景并收起两舷面板。
  if (MOBILE()) { $("veil").classList.add("on"); document.body.classList.add("sheeting"); closeRoster(); closeDock(); }
  const dk = { "sheet-map":"s-map" }[id];
  if (dk && $(dk)) $(dk).classList.add("on");
  const qk = { "sheet-map":"qk-map" }[id];
  if (qk && $(qk)) $(qk).classList.add("on");
}
function closeSheet() {
  if (openedSheet) {
    $(openedSheet).classList.remove("on");
    ["s-map","qk-map"].forEach(i => { const el = $(i); if (el) el.classList.remove("on"); });
    openedSheet = null;
  }
  if (!(MOBILE() && (rosterOpen() || dockOpen()))) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
}
// 桌面：点浮层与功能台以外的地方即关闭（无遮罩也要有「点外面收起」的直觉）
document.addEventListener("pointerdown", function (ev) {
  if (!openedSheet || MOBILE()) return;
  if (ev.target.closest("#" + openedSheet) || ev.target.closest("#dock")) return;
  closeSheet();
}, true);
$("s-map").addEventListener("click", () => openSheet("sheet-map"));
$("qk-map").addEventListener("click", () => openSheet("sheet-map"));
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => closeSheet()));
$("veil").addEventListener("click", () => { closeSheet(); if (MOBILE()) { closeRoster(); closeDock(); } });
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape") { closeSheet(); if (MOBILE()) { closeRoster(); closeDock(); } }
});

/* --- 滑块：信息条底色 / 贴脸预警 --- */
function bindSlider(id, key, fmt, after) {
  const el = $(id), out = $("v-" + id.replace("s-", ""));
  el.value = PREF[key];
  const apply = () => { setPref(key, Number(el.value)); if (out) out.textContent = fmt(el.value); if (after) after(); };
  el.addEventListener("input", () => { apply(); if (R3) R3.setPrefs(PREF); });
  apply();
}
bindSlider("s-tagop", "tagop", v => v + "%", () => {
  document.documentElement.style.setProperty("--tagop", (Number(PREF.tagop) / 100).toFixed(2));
});
// 枪线长度：2D 走 CSS 变量；3D 读同一份 PREF（引用共享，改值即可见）
bindSlider("s-gunlen", "gunlen", v => v + "px", () => {
  document.documentElement.style.setProperty("--gunlen", Number(PREF.gunlen) + "px");
});
// Korr adapter receives input changes immediately. Position smoothing uses a time constant.
bindSlider("s-fpvtau", "fpvtau", v => Number(v) === 0 ? "最跟手" : v + "ms");
// 眼高/FOV 通过 setPrefs 即时传递，启动时也从本地偏好恢复。
bindSlider("s-fpvheight", "fpvheight", v => Number(v).toFixed(1) + "m");
bindSlider("s-fov", "fov", v => v + "°");
bindSlider("s-walltrans", "walltrans", v => v + "%");
bindSlider("s-floortrans", "floortrans", v => v + "%");
// 人物模型大小：只影响 3D 里的人物（自己/队友/敌人/AI），不含物资与死亡盒
bindSlider("s-charsize", "charsize", v => v + "%");
(function bindModelChoices(){
  const seg=$("model3dseg"); if(!seg)return;
  const sync=()=>seg.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.model===PREF.model3d));
  seg.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>{setPref("model3d",b.dataset.model);sync();if(R3)R3.setPrefs(PREF);toast("3D 人物模型已切换");}));
  sync();
})();
(function bindDirectionChoices(){
  const style=$('direction3dseg'),anchor=$('directionanchor3dseg');
  const sync=()=>{
    style?.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.direction===PREF.directionstyle3d));
    anchor?.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.anchor===PREF.directionanchor3d));
  };
  style?.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{setPref('directionstyle3d',b.dataset.direction);sync();if(R3)R3.setPrefs(PREF);}));
  anchor?.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{setPref('directionanchor3d',b.dataset.anchor);sync();if(R3)R3.setPrefs(PREF);}));
  sync();
})();
(function bindPanelTheme(){
  const seg=$("panelthemeseg"); if(!seg)return;
  const sync=()=>seg.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.theme===PREF.theme));
  seg.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>{
    setPref("theme",b.dataset.theme);applyTheme(PREF.theme);sync();
  }));
  sync();
})();
function bindColor(id,key){
  const el=$(id),out=$("v-"+id.replace("s-","")); if(!el)return;
  const valid=/^#[0-9a-f]{6}$/i.test(String(PREF[key]||""))?PREF[key]:PREF_DEF[key]; el.value=valid;
  const apply=()=>{const v=el.value.toLowerCase();setPref(key,v);if(out)out.textContent=v.toUpperCase();if(R3)R3.setPrefs(PREF);};
  el.addEventListener("input",apply); if(out)out.textContent=valid.toUpperCase();
}
bindColor("s-visiblecolor3d","visiblecolor3d");
bindColor("s-occludedcolor3d","occludedcolor3d");
// 预警距离（m）：进入这个距离的屏外敌人才画箭头。**独立于「贴脸高亮」** ——
// 贴脸是"近到要紧张"（默认 80m），预警是"该看一眼方向了"（默认 150m），
// 两者绑一起就只能二选一。拖到 0 = 关（和贴脸一样）。与 2D 一样带滞回？
// 不需要：箭头只是位置指示，阈值边缘抖动最多让箭头闪一下，没有重排代价。
bindSlider("s-warnd", "warnd", v => Number(v) === 0 ? "关" : v + "m");
// 预警基准圆半径（px）：只影响 r3d 画的那个圆的半径，改值即刻可见（无重建）
bindSlider("s-warnr", "warnr", v => v + "px");
// 预警箭头大小（%）：缩放三角与它旁边的距离数字（圆半径不跟着变，两者独立调）
bindSlider("s-warnsz", "warnsz", v => v + "%");
// 3D 画质：auto / perf / balanced / high（只调渲染分辨率，不动几何贴图）。
// 「自动」会按设备先给一档，之后实测帧率偏低就自动降档（r3d 的 setQuality/_autoDowngrade）。
(function bindQuality() {
  const seg = $("q3dseg");
  if (!seg) return;
  const sync = () => seg.querySelectorAll("button").forEach(b =>
    b.classList.toggle("on", b.dataset.q === (PREF.q3d || "auto")));
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    setPref("q3d", b.dataset.q);
    sync();
    if (R3) R3.setQuality(PREF.q3d);
  }));
  sync();
})();
// 帧率上限：0 = 无上限（跟随显示器刷新率）。按固定间隔出帧可让节拍更均匀。
(function bindFpsCap() {
  const seg = $("fpscapseg");
  if (!seg) return;
  const sync = () => seg.querySelectorAll("button").forEach(b =>
    b.classList.toggle("on", Number(b.dataset.f) === (Number(PREF.fpscap) || 0)));
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    setPref("fpscap", Number(b.dataset.f) || 0); if(R3) R3.setPrefs(PREF);
    sync();
  }));
  sync();
})();
// 标记大小 / 头顶条字体大小：整条缩放，不动 Leaflet 的锚点
bindSlider("s-dotsize", "dotsize", v => v + "%", () => {
  document.documentElement.style.setProperty("--mks", (Number(PREF.dotsize) || 100) / 100);
  _rc.tag.clear();      // 信息条锚点随标记大小重算
});
bindSlider("s-fontsize", "fontsize", v => v + "%", () => {
  document.documentElement.style.setProperty("--fs", (Number(PREF.fontsize) || 100) / 100);
  _rc.tag.clear();
});
// 状态字体粗细（头顶信息条的状态/名字/距离等一起变粗变细）
bindSlider("s-tagw", "tagweight", v => String(v), () => {
  document.documentElement.style.setProperty("--tagw", Number(PREF.tagweight) || 600);
  _rc.tag.clear();
});
bindSlider("s-alert", "alert", v => Number(v) === 0 ? "关" : v + "m", () => {
  _rc.roster = ""; _rc.icon.clear(); if (lastSnap) draw(lastSnap);
});
$("pick-loot").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-g]"); if (!b) return;
  setPref("lootmin", Number(b.dataset.g));
  document.querySelectorAll("#pick-loot button").forEach(x => x.classList.toggle("on", x === b));
  purge(); if (lastSnap) draw(lastSnap);
  if (R3 && lastSnap && is3d()) R3.update(loot3dSnapshot(lastSnap));
});
document.querySelectorAll("#pick-loot button").forEach(x =>
  x.classList.toggle("on", Number(x.dataset.g) === Number(PREF.lootmin)));

/* ------------------------------------------------------------ 3D 白模视图
   同源引擎：/m3d/r3d.js（three.js + 极简 GLB 读取），与 2D 共用同一份快照。
   模型 16~37MB，首次进图下载一次后走 ETag 长缓存；没有模型的图直接提示。 */
let R3 = null, r3busy = false, r3man = null;
const M3D = "/m3d/";
const is3d = () => !!R3 && document.body.classList.contains("mode3d");

function r3CamSync() {
  const c = R3 ? R3.camMode : (PREF.cam3d || "chase");
  document.querySelectorAll("#camseg button").forEach(b =>
    b.classList.toggle("on", b.dataset.cam === c));
}
async function r3Manifest() {
  if (r3man) return r3man;
  try { r3man = await (await fetch(M3D + "manifest.json", { cache: "no-cache" })).json(); }
  catch (e) { r3man = { maps: {} }; }
  return r3man;
}
function r3Loading(on, pct, txt) {
  const el = $("r3-load");
  el.classList.toggle("on", !!on);
  if (txt) $("r3-load-t").textContent = txt;
  $("r3-bar").firstElementChild.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
}
async function r3Model() {
  const k = (MAP_INFO && MAP_INFO.key) || DEF_MAP;
  const mf = await r3Manifest();
  const rec = mf.maps && mf.maps[k];
  if (!rec) return null;
  // GLB 长缓存必须带内容版本；只换同名文件时，force-cache 会在
  // max-age 内直接命中旧模型，ETag 根本没有机会参与重验证。
  const rev = rec.rev || rec.bytes || 1;
  return { url: M3D + rec.file + "?v=" + encodeURIComponent(rev), rec: rec, key: k };
}
// 干员台点选 -> 3D 镜头对准（2D 那边同时在做 setView，两边行为一致）
function r3Focus(key) {
  if (!is3d() || !R3) return;
  R3.focus(key, lastSnap);          // 定位会从「跟随」切到「自由」，相机条要跟着变
  setPref("cam3d", R3.camMode);
  r3CamSync();
}
// 换图：3D 开着就把新图白模换上；该图没有白模则退回 2D，不留一张错图在屏幕上
async function r3SwitchMap() {
  if (!R3 || !is3d()) return;
  const m = await r3Model();
  if (!m) { toast("该地图暂无 3D 白模，已切回 2D"); set3d(false, true); return; }
  const mb = (m.rec.bytes / 1048576).toFixed(0);
  r3Loading(true, 0, "载入 " + (m.rec.name || m.key) + " 白模 " + mb + "MB…");
  await R3.setMap(MAP_INFO, m.url, (got, tot) => r3Loading(true, tot ? got / tot * 100 : 0,
    "载入 " + (m.rec.name || m.key) + " " + (got / 1048576).toFixed(1) + "/" + mb + "MB"), m.rec);
  r3Loading(false);
  if (lastSnap) R3.update(loot3dSnapshot(lastSnap));
}

// 2D / 3D 分段与快捷条 3D 键的选中态统一在这里同步
function sync3dUI(on) {
  const d3 = $("s-3d"), d2 = $("s-2d"), qk = $("qk-3d");
  if (d3) d3.classList.toggle("on", on);
  if (d2) d2.classList.toggle("on", !on);
  if (qk) qk.classList.toggle("on", on);
}
async function set3d(on, quiet) {
  if (NO3D) return;
  if (r3busy) return;
  if (on === is3d()) return;
  if (!on) {
    document.body.classList.remove("mode3d");
    if (R3 && R3.setActive) R3.setActive(false);
    $("r3").classList.remove("on");
    $("camseg").classList.remove("on");
    $("map").classList.remove("mini");
    // 小地图窗口被拖过/缩过会留下内联样式，退出时清掉，否则 2D 主图不铺满
    ["left", "top", "right", "bottom", "width", "height"].forEach(k2 => { $("map").style[k2] = ""; });
    syncMiniHint();
    setTimeout(() => map.invalidateSize(), 60);
    r3Loading(false);
    sync3dUI(false);
    setPref("v3d", 0);
    setTimeout(() => map.invalidateSize(), 340);
    return;
  }
  r3busy = true;
  try {
    const m = await r3Model();
    if (!m) { toast("该地图暂无 3D 白模"); return; }
    if (!R3) {
      r3Loading(true, 2, "载入 3D 引擎…");
      const mod = await import(M3D + "korr-adapter.js?v=1.1.0");
      R3 = mod.create({
        canvas: $("r3-cv"), labels: $("r3-lbl"), warn: $("warn-layer"),
        mapInfo: MAP_INFO, model: m.rec, pref: PREF,
        onPick: (e) => { if (e && e.key && e.key !== "__self") focusEntity(e.key); },
        onToast: (msg) => toast(msg),
        bagLv: (typeof BAG_LV !== "undefined" ? BAG_LV : null),
        bagCap: (typeof BAG_CAP !== "undefined" ? BAG_CAP : null),
      });
      // 3D 内部因拖动平移而脱离跟随时，同步相机条高亮（不写偏好，下次进 3D 仍按原设置）
      R3.onCam = (mode) => { r3CamSync(); };
    }
    document.body.classList.add("mode3d");
    if (R3 && R3.setActive) R3.setActive(true);
    $("r3").classList.add("on");
    setTimeout(() => { miniRestore(); syncMiniHint(); }, 80);
    $("camseg").classList.add("on");
    if (PREF.pip !== 0) $("map").classList.add("mini");

    sync3dUI(true);
    setPref("v3d", 1);
    R3.resize();
    const mb = (m.rec.bytes / 1048576).toFixed(0);
    r3Loading(true, 3, "载入 " + (m.rec.name || m.key) + " 白模 " + mb + "MB…");
    await R3.setMap(MAP_INFO, m.url, (got, tot) => {
      r3Loading(true, tot ? got / tot * 100 : 0,
        "载入 " + (m.rec.name || m.key) + " " + (got / 1048576).toFixed(1) + "/" + mb + "MB");
    }, m.rec);
    r3Loading(false);
    const st = R3.stat();
    if (st.err) {
      // 同上：底图载入失败不再自动恢复，打断手机上的重载死循环
      setPref("v3d", 0);
      toast("3D 底图载入失败：" + st.err);
    }
    else if (!quiet) toast("3D 视图 · " + (st.tris / 1e6).toFixed(1) + "M 面"
      + (st.fps ? " · " + st.fps + " FPS" : ""));
    R3.setPrefs(PREF);
    R3.setQuality(PREF.q3d || "auto");     // 重进 3D 时按当前档位重设渲染分辨率
    R3.setCam(PREF.cam3d || "chase");
    // 3D 跟随时同步打开 2D 跟随，并立刻把小地图中心对到自己
    if ((PREF.cam3d || "chase") === "chase") {
      setFollow(true);
      if (lastSnap && lastSnap.self)
        setTimeout(() => map.panTo(pointLatLng(lastSnap.self), { animate: false }), 360);
    }
    r3CamSync();
    if (lastSnap) R3.update(loot3dSnapshot(lastSnap));
    setTimeout(() => map.invalidateSize(), 340);
  } catch (e) {
    r3Loading(false);
    // 载入失败就清 v3d，下次启动不再自动拉白模——否则手机上会变成
    // 「启动→载 daba→标签被杀→重载→再载」的死循环，只能手动再开 3D。
    setPref("v3d", 0);
    toast("3D 初始化失败：" + (e && e.message || e));
  } finally { r3busy = false; }
}
const NO3D = document.querySelector('meta[name="tq-no3d"]')?.content === "1";   // 服务端改写 meta：无 3D 资产时置 1，隐藏 3D 入口
if (NO3D) document.documentElement.dataset.no3d = "1";
if (NO3D) {
  ["qk-3d","s-3d"].forEach(i => { const el = $(i); if (el) el.style.display = "none"; });
  const pip = $("s-pip"); if (pip) pip.style.display = "none";
}
$("s-2d").addEventListener("click", () => set3d(false));
$("s-3d").addEventListener("click", () => set3d(true));
$("qk-3d").addEventListener("click", () => set3d(!is3d()));
$("camseg").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-cam]"); if (!b || !R3) return;
  document.querySelectorAll("#camseg button").forEach(x => x.classList.toggle("on", x === b));
  setPref("cam3d", b.dataset.cam);
  R3.setCam(b.dataset.cam);
});
$("s-pip").addEventListener("click", () => {
  setPref("pip", PREF.pip ? 0 : 1);
  $("s-pip").classList.toggle("on", !!PREF.pip);
  $("map").classList.toggle("mini", is3d() && PREF.pip !== 0);
  setTimeout(() => map.invalidateSize(), 340);
});
$("s-pip").classList.toggle("on", !!PREF.pip);
window.addEventListener("resize", () => { if (R3) R3.resize(); });
// 上次停在 3D 就恢复（模型走浏览器缓存，不会重复下载）
if (Number(PREF.v3d) === 1) setTimeout(() => set3d(true, true), 600);

/* --- 3D 小地图：拖动移动 / 滚轮缩放 --- */
{
  const el = $("map");
  let drag = null;
  const isMini = () => document.body.classList.contains("mode3d") && el.classList.contains("mini");
  el.addEventListener("pointerdown", e => {
    if (!isMini() || !e.shiftKey || e.button !== 0) return;   // Shift+拖动 = 移动这个框
    const r = el.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault(); e.stopPropagation();
  }, true);
  el.addEventListener("pointermove", e => {
    if (!drag) return;
    const x = Math.max(0, Math.min(window.innerWidth - 60, e.clientX - drag.dx));
    const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - drag.dy));
    el.style.left = x + "px"; el.style.top = y + "px";
    el.style.right = "auto"; el.style.bottom = "auto";
    map.invalidateSize(); syncMiniHint();
    e.preventDefault(); e.stopPropagation();
  }, true);
  const endDrag = () => { drag = null; syncMiniHint(); miniSave(); };
  window.syncMiniHint = () => {
    const el = $("map"), hint = $("mini-hint");
    if (!el || !hint) return;
    const show = document.body.classList.contains("mode3d") && el.classList.contains("mini");
    hint.classList.toggle("on", show);
    if (!show) return;
    const r2 = el.getBoundingClientRect();
    hint.style.left = Math.round(r2.left) + "px";
    hint.style.top = Math.max(4, Math.round(r2.top - 22)) + "px";
    hint.style.maxWidth = Math.round(r2.width) + "px";
  };
  const MINI_KEY = "nr2_mini";
  window.miniSave = () => {
    const el = $("map");
    if (!el.classList.contains("mini")) return;
    const r2 = el.getBoundingClientRect();
    try {
      localStorage.setItem(MINI_KEY, JSON.stringify({
        l: Math.round(r2.left), t: Math.round(r2.top),
        w: Math.round(r2.width), h: Math.round(r2.height) }));
    } catch (e) {}
  };
  window.miniRestore = () => {
    const el = $("map");
    let v = null;
    try { v = JSON.parse(localStorage.getItem(MINI_KEY) || "null"); } catch (e) {}
    if (!v || !v.w) return;                       // 没记录过就用默认位置
    const w = Math.max(150, Math.min(window.innerWidth * 0.8, v.w));
    const h = Math.max(110, Math.min(window.innerHeight * 0.8, v.h || w * 0.74));
    const l = Math.max(0, Math.min(window.innerWidth - 60, v.l || 0));
    const t = Math.max(0, Math.min(window.innerHeight - 40, v.t || 0));
    el.style.left = l + "px"; el.style.top = t + "px";
    el.style.right = "auto"; el.style.bottom = "auto";
    el.style.width = Math.round(w) + "px"; el.style.height = Math.round(h) + "px";
    map.invalidateSize(); syncMiniHint();
  };
  el.addEventListener("pointerup", endDrag, true);
  el.addEventListener("pointercancel", endDrag, true);
  el.addEventListener("wheel", e => {
    if (!isMini() || !e.shiftKey) return;   // 无 Shift 时交回 Leaflet 原有缩放
    e.preventDefault(); e.stopPropagation();
    const r = el.getBoundingClientRect();
    const w = Math.max(150, Math.min(window.innerWidth * 0.7, r.width * (e.deltaY < 0 ? 1.12 : 0.89)));
    el.style.width = Math.round(w) + "px";
    el.style.height = Math.round(w * 0.74) + "px";
    map.invalidateSize(); syncMiniHint(); miniSave();
  }, { passive: false, capture: true });
}

/* --- 回放控制 --- */
const IC_PAUSE = '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>';
const IC_PLAY  = '<path d="M7 4l13 8-13 8z"/>';
let playing = true;
$("btn-play").addEventListener("click", () => {
  playing = !playing;
  $("ic-play").innerHTML = playing ? IC_PAUSE : IC_PLAY;
  fetch("/api/ctrl?pause=" + (playing ? 0 : 1));
});
$("spd").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-spd]"); if (!b) return;
  document.querySelectorAll("#spd button").forEach(x => x.classList.toggle("on", x === b));
  fetch("/api/ctrl?speed=" + b.dataset.spd);
});
$("seek").addEventListener("pointerdown", () => { seeking = true; });
$("seek").addEventListener("pointerup", () => { seeking = false; fetch("/api/ctrl?seek=" + $("seek").value); });
$("seek").addEventListener("change", () => { seeking = false; fetch("/api/ctrl?seek=" + $("seek").value); });

/* --- 战况岛展开 --- */
$("island").addEventListener("click", () => $("island").classList.toggle("open"));

/* --- 窗口尺寸切换：桌面/移动端布局语义不同，重置一次开合状态 --- */
let _wasMobile = MOBILE();
window.addEventListener("resize", () => {
  const m = MOBILE();
  if (m !== _wasMobile) {
    _wasMobile = m;
    closeSheet();
    if (m) { $("roster").classList.remove("hide"); closeRoster(); } else openRoster();
  }
  if (map) map.invalidateSize();
});
