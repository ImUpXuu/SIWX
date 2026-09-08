/* 日志页 —— 实时拉取服务端环形日志缓冲 */
const { esc, fetchJSON } = window.SX;

let paused = false;
let autoScroll = true;
let lastLen = 0;

function el(id) { return document.getElementById(id); }

function levelClass(level, msg) {
  // 使用日志级别（新格式）
  if (level === "ERROR") return "log-line err";
  if (level === "WARN") return "log-line warn";
  if (level === "DEBUG") return "log-line dim";
  // INFO: 根据内容判断
  if (msg.includes("✔") || msg.includes("成功") || msg.includes("已验证") || msg.includes("完成")) return "log-line ok";
  if (msg.includes("✗") || msg.includes("失败") || msg.includes("错误")) return "log-line err";
  return "log-line";
}

function renderLogs(logs) {
  const box = el("log-box");
  if (logs.length === lastLen) return;
  // 只追加新增条目（避免全量重绘）
  for (let i = lastLen; i < logs.length; i++) {
    const entry = logs[i];
    // 兼容旧格式 [ts, msg] 和新格式 [ts, level, module, msg]
    let level, module, msg;
    if (entry.length >= 4) {
      [, level, module, msg] = entry;
    } else {
      [, msg] = entry;
      level = "INFO";
      module = "";
    }
    const div = document.createElement("div");
    div.className = levelClass(level, msg);
    const timeStr = new Date(entry[0]).toLocaleTimeString("zh-CN", { hour12: false });
    div.textContent = `${timeStr} [${module}] ${msg}`;
    box.appendChild(div);
  }
  // 限制 DOM 节点数
  while (box.children.length > 2000) box.removeChild(box.firstChild);
  lastLen = logs.length;
  el("log-count").textContent = `${logs.length} 条`;
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

async function poll() {
  if (paused) return;
  try {
    const d = await fetchJSON("/api/logs?limit=2000");
    renderLogs(d.logs || []);
    // 显示当前日志模式
    if (d.level) {
      const modeEl = el("log-mode");
      if (modeEl) modeEl.textContent = d.level === "detailed" ? "详细模式" : "粗略模式";
    }
  } catch (e) { /* 忽略 */ }
}

export async function init() {
  el("log-pause").addEventListener("click", () => {
    paused = !paused;
    el("log-pause").textContent = paused ? "▶ 继续" : "⏸ 暂停";
  });
  el("log-clear").addEventListener("click", () => {
    el("log-box").innerHTML = "";
    lastLen = 0;
  });
  el("log-auto").addEventListener("change", (e) => {
    autoScroll = e.target.checked;
    if (autoScroll) el("log-box").scrollTop = el("log-box").scrollHeight;
  });
  await poll();
  setInterval(poll, 1000);
}

export function destroy() { /* 由 router 清理 */ }