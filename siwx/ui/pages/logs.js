/* 日志页 —— 实时拉取服务端环形日志缓冲 */
const { esc, fetchJSON } = window.SX;

let paused = false;
let autoScroll = true;
let lastSig = "";
let pollTimer = null;

function el(id) { return document.getElementById(id); }

function levelClass(m) {
  if (m.includes("[错误]") || m.includes("✗") || m.includes("失败")) return "log-line err";
  if (m.includes("✔") || m.includes("成功") || m.includes("已验证")) return "log-line ok";
  if (m.includes("⚠") || m.includes("跳过") || m.includes("缺失")) return "log-line warn";
  return "log-line dim";
}

function logText(entry) {
  // /api/logs 混合返回旧格式 [ts,msg] 与新格式 [ts,level,module,msg]——
  // 原先恒取 entry[1]，4 元组条目在日志页显示的是 "INFO"/"WARN" 级别串
  return entry.length >= 4 ? entry[3] : entry[1];
}

function renderLogs(logs) {
  const box = el("log-box");
  const shown = logs.slice(-1000);
  const sig = shown.length ? `${shown.length}:${shown[shown.length - 1][0]}:${logText(shown[shown.length - 1])}` : "0";
  if (sig === lastSig) return;
  lastSig = sig;
  box.innerHTML = shown.map((entry) => {
    const m = logText(entry);
    const div = document.createElement("div");
    div.className = levelClass(m);
    div.textContent = m;
    return div.outerHTML;
  }).join("") || '<div class="log-line dim">暂无日志…</div>';
  el("log-count").textContent = `${logs.length} 条`;
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

async function poll() {
  if (paused) return;
  try {
    const d = await fetchJSON("/api/logs");
    renderLogs(d.logs || []);
  } catch (e) { /* 忽略 */ }
}

export async function init() {
  el("log-pause").addEventListener("click", () => {
    paused = !paused;
    el("log-pause").textContent = paused ? "▶ 继续" : "⏸ 暂停";
  });
  el("log-clear").addEventListener("click", () => {
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
