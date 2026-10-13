/* 日志页 —— 实时拉取服务端环形日志缓冲 */
const { esc, fetchJSON } = window.SX;

let paused = false;
let autoScroll = true;
let lastSig = "";
let pollTimer = null;
let clearMark = 0;          // 「清空」水位：只显示该时刻之后的日志，避免 1s 后整屏刷回来

function el(id) { return document.getElementById(id); }

/* /api/logs 返回混合形状：[ts, msg]（文件轨）与 [ts, level, module, msg]（结构化轨） */
function entryMsg(entry) {
  return String(entry.length >= 4 ? entry[3] : entry[1]);
}

function levelClass(entry) {
  const lv = entry.length >= 4 ? String(entry[1]).toUpperCase() : "";
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
  const shown = logs.filter(l => l[0] > clearMark).slice(-1000);
  const last = shown[shown.length - 1];
  const sig = shown.length ? `${shown.length}:${last[0]}:${entryMsg(last)}` : "0";
  if (sig === lastSig) return;
  lastSig = sig;
  box.innerHTML = shown.map((entry) =>
    `<div class="${levelClass(entry)}">${esc(entryMsg(entry))}</div>`).join("")
    || '<div class="log-line dim">暂无日志…</div>';
  el("log-count").textContent = `${shown.length} 条（最近）`;
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

async function poll() {
  if (paused || document.hidden) return;
  try {
    const d = await fetchJSON("/api/logs");
    renderLogs(d.logs || []);
  } catch (e) { /* 忽略 */ }
}

export async function init() {
  el("log-pause").addEventListener("click", () => {
    paused = !paused;
    el("log-pause").textContent = paused ? "继续" : "暂停";
    el("log-pause").setAttribute("aria-pressed", String(paused));
  });
  el("log-clear").addEventListener("click", () => {
    clearMark = Date.now() + 1;
    el("log-box").innerHTML = "";
    lastSig = "";
  });
  el("log-auto").addEventListener("change", (e) => {
    autoScroll = e.target.checked;
    if (autoScroll) el("log-box").scrollTop = el("log-box").scrollHeight;
  });
  await poll();
  pollTimer = setInterval(poll, 1000);
}

export function destroy() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}
