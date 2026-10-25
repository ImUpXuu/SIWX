/* 日志页 —— 实时拉取服务端环形日志缓冲 */
import { createDropdown } from '/widgets.js?v=2026100601';

const { esc, fetchJSON, copyText, toast, timeStr } = window.SX;

let paused = false;
let autoScroll = true;
let lastSig = "";
let pollTimer = null;
let clearMark = 0;          // 「清空」水位：只显示该时刻之后的日志，避免 1s 后整屏刷回来
let levelDrop = null;       // 级别筛选（自绘下拉，替代原生 select）

function el(id) { return document.getElementById(id); }

/* /api/logs 返回混合形状：[ts, msg]（文件轨）与 [ts, level, module, msg]（结构化轨） */
function entryMsg(entry) {
  return String(entry.length >= 4 ? entry[3] : entry[1]);
}

/* 文件轨条目 [ts, "2026-… [DEBUG] siwx: …"] 自带级别前缀，解析出来与结构化轨同口径 */
const _FILE_LEVEL_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]?\d*\s*\[(\w+)\]/;

function entryLevel(entry) {
  if (entry.length >= 4) return String(entry[1]).toUpperCase();
  const m = _FILE_LEVEL_RE.exec(String(entry[1] || ""));
  return m ? m[1].toUpperCase() : null;
}

function levelClass(entry) {
  const lv = entryLevel(entry);
  if (lv === "ERROR") return "log-line err";
  if (lv === "WARN") return "log-line warn";
  if (lv === "INFO") return "log-line ok";
  if (lv === "DEBUG") return "log-line dim";
  const m = entryMsg(entry);
  if (m.includes("[错误]") || m.includes("✗") || m.includes("失败")) return "log-line err";
  if (m.includes("✔") || m.includes("成功") || m.includes("已验证")) return "log-line ok";
  if (m.includes("⚠") || m.includes("跳过") || m.includes("缺失")) return "log-line warn";
  return "log-line dim";
}

function renderLogs(logs) {
  // 轮询可能在切页后仍在飞，视图已卸载时直接放弃本轮渲染
  const box = el("log-box");
  if (!box) return;
  const filter = (levelDrop && levelDrop.value) || "";
  const kw = (el("log-text")?.value || "").trim().toLowerCase();
  const tsOn = el("log-ts")?.checked || false;
  let shown = logs.filter(l => l[0] > clearMark);
  if (filter) shown = shown.filter(l => entryLevel(l) === filter);
  if (kw) shown = shown.filter(l => entryMsg(l).toLowerCase().includes(kw));
  shown = shown.slice(-1000);
  const last = shown[shown.length - 1];
  const sig = `${filter}:${kw}:${tsOn}:${shown.length ? `${shown.length}:${last[0]}:${entryMsg(last)}` : "0"}`;
  if (sig === lastSig) return;
  lastSig = sig;
  box.innerHTML = shown.map((entry) => {
    const t = tsOn && entry[0] ? tsSpan(entry[0]) : "";
    return `<div class="${levelClass(entry)}">${t}${esc(entryMsg(entry))}</div>`;
  }).join("")
    || `<div class="log-line dim">${filter || kw ? "没有匹配的日志…" : "暂无日志…"}</div>`;
  el("log-count").textContent = `${shown.length} 条（最近）`;
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

/* 时间戳列：与全局 SX.timeStr 同一份 HH:MM:SS（原页内自拼已收敛） */
function tsSpan(ms) {
  const d = new Date(ms);
  if (isNaN(d)) return "";
  return `<span class="t">${timeStr(ms)}</span>`;
}

async function poll() {
  if (paused || document.hidden) return;
  try {
    const d = await fetchJSON("/api/logs");
    renderLogs(d.logs || []);
  } catch (e) { /* 忽略 */ }
}

export async function init() {
  // 级别筛选下拉（替代原生 select）：选项与值口径与原 select 一致
  //（空串 = 全部；空值时按钮文案用 placeholder 保持「全部级别」）
  levelDrop = createDropdown({
    options: [
      { value: "", label: "全部级别" },
      { value: "ERROR", label: "仅错误" },
      { value: "WARN", label: "仅警告" },
      { value: "INFO", label: "仅信息" },
      { value: "DEBUG", label: "仅调试" },
    ],
    value: "",
    placeholder: "全部级别",
    label: "按级别筛选",
  });
  el("log-filter").appendChild(levelDrop.el);
  levelDrop.onChange = () => {
    lastSig = "";           // 换筛选条件后强制重渲染
    poll();
  };
  // 文本搜索 / 时间戳开关：只改渲染不改数据源，重渲染即可
  el("log-text")?.addEventListener("input", () => { lastSig = ""; poll(); });
  el("log-ts")?.addEventListener("change", () => { lastSig = ""; poll(); });
  el("log-copy")?.addEventListener("click", async () => {
    // 复制当前筛选条件下展示的日志（与界面所见一致），而非全量缓冲
    const lines = [...el("log-box").querySelectorAll(".log-line")]
      .map(n => n.textContent.trimStart()).filter(Boolean);
    if (!lines.length) { toast("日志为空", true); return; }
    const ok = await copyText(lines.join("\n"));
    toast(ok ? "已复制当前显示的日志" : "复制失败", !ok);
  });
  el("log-pause")?.addEventListener("click", () => {
    paused = !paused;
    el("log-pause").textContent = paused ? "继续" : "暂停";
    el("log-pause").setAttribute("aria-pressed", String(paused));
  });
  el("log-clear")?.addEventListener("click", () => {
    clearMark = Date.now() + 1;
    const box = el("log-box");
    if (box) box.innerHTML = "";
    lastSig = "";
  });
  el("log-auto")?.addEventListener("change", (e) => {
    autoScroll = e.target.checked;
    if (autoScroll) {
      const box = el("log-box");
      if (box) box.scrollTop = box.scrollHeight;
    }
  });
  // 粗略/精细切换（此前是静态文案）：读写 /api/logs/settings，落盘口径不变，
  // 只改日志页展示——粗略隐藏 DEBUG 行（服务端 _merged_logs 同口径过滤）
  el("log-mode")?.addEventListener("click", async () => {
    const b = el("log-mode");
    const next = b.textContent.trim() === "粗略模式" ? "detailed" : "rough";
    b.disabled = true;
    try {
      const r = await fetchJSON("/api/logs/settings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ level: next }),
      });
      b.textContent = r.level === "detailed" ? "精细模式" : "粗略模式";
      lastSig = "";         // 口径变了，强制重渲染
      await poll();
    } catch (e) {
      toast("切换失败：" + e.message, true);
    } finally {
      b.disabled = false;
    }
  });
  // 初始状态对齐服务端持久化的口径
  try {
    const s = await fetchJSON("/api/logs/settings");
    const b = el("log-mode");
    if (b) b.textContent = s.level === "detailed" ? "精细模式" : "粗略模式";
  } catch (_) { /* 保持默认文案 */ }
  await poll();
  pollTimer = setInterval(poll, 1000);
}

export function destroy() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  if (levelDrop) { levelDrop.destroy(); levelDrop = null; }
}
