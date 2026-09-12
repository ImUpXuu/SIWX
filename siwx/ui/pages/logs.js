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

function renderLogs(logs) {
  const box = el("log-box");
  const shown = logs.slice(-1000);
  const sig = shown.length ? `${shown.length}:${shown[shown.length - 1][0]}:${shown[shown.length - 1][1]}` : "0";
  if (sig === lastSig) return;
  lastSig = sig;
  box.innerHTML = shown.map(([, m]) => {
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
