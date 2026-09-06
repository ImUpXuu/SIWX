/* 引导页 —— 3 个独立分步屏；第 3 步一键提取+解密（合并） */
const { esc, fetchJSON, startJob, renderLog, go, setSetupDone } = window.SX;

let state = { step: 1, account: null, running: false };

function show(step) {
  state.step = step;
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`gs${i}`).classList.toggle('hidden', i !== step);
    const chip = document.querySelector(`.g-chip[data-s="${i}"]`);
    chip.classList.toggle('on', i === step);
    chip.classList.toggle('done', i < step);
  }
}

function badge(cached, total) {
  if (!total) return '<span class="badge badge-none">无数据</span>';
  if (cached >= total) return `<span class="badge badge-full">缓存 ✓ ${cached}/${total}</span>`;
  if (cached > 0) return `<span class="badge badge-part">缓存 ${cached}/${total}</span>`;
  return `<span class="badge badge-none">未提取</span>`;
}

async function screen1() {
  show(1);
  const el = document.getElementById('g-check');
  el.textContent = '正在检测环境…';
  try {
    const s = await fetchJSON('/api/status');
    const wx = s.wechat_running ? `✅ 微信运行中（${s.pids.length} 进程）`
                                : `⚠️ 微信未运行 —— 提取密钥需要微信在线，请先启动并登录`;
    const accs = s.accounts.length
      ? `✅ 发现 ${s.accounts.length} 个微信号数据目录`
      : `❌ 未找到微信数据目录 —— 请确认本机登录过微信`;
    const ks = s.stored_salts ? `✅ 密钥库已有 ${s.stored_salts} 条缓存`
                              : `ℹ️ 密钥库为空（首次运行）`;
    el.innerHTML = `<div>${wx}</div><div>${accs}</div><div>${ks}</div>`;
    document.getElementById('g-start').disabled = !s.accounts.length;
  } catch (e) {
    el.innerHTML = `❌ 后端离线：${e.message}`;
  }
}

async function screen2() {
  show(2);
  const el = document.getElementById('g-accounts');
  const s = await fetchJSON('/api/status');
  state.accounts = s.accounts;
  if (!s.accounts.length) {
    el.innerHTML = '<div class="empty">未找到账号</div>';
    return;
  }
  el.innerHTML = s.accounts.map((a, i) => `
    <div class="acc ${state.account && state.account.db_dir === a.db_dir ? 'sel' : ''}" data-i="${i}">
      <div class="avatar">${esc(a.wxid.replace(/^wxid_/, '').slice(0, 2).toUpperCase())}</div>
      <div class="acc-main"><b>${esc(a.wxid)}</b><span>${a.db_count} 个数据库</span></div>
      ${badge(a.keys_cached, a.total_salts)}
    </div>`).join('');
  el.querySelectorAll('.acc').forEach(card => {
    card.addEventListener('click', () => {
      state.account = s.accounts[Number(card.dataset.i)];
      el.querySelectorAll('.acc').forEach(c => c.classList.remove('sel'));
      card.classList.add('sel');
      document.getElementById('g-to3').disabled = false;
      document.getElementById('g3-hint').textContent =
        `对 ${state.account.wxid} 一键提取密钥并解密（${state.account.keys_cached}/${state.account.total_salts} 已缓存）。`;
    });
  });
  if (state.account) {
    const idx = s.accounts.findIndex(a => a.db_dir === state.account.db_dir);
    if (idx >= 0) el.querySelector(`.acc[data-i="${idx}"]`)?.classList.add('sel');
    document.getElementById('g-to3').disabled = false;
  }
}

function screen3() {
  show(3);
  if (state.account) {
    document.getElementById('g3-hint').textContent =
      `对 ${state.account.wxid} 一键提取密钥并解密（${state.account.keys_cached}/${state.account.total_salts} 已缓存）。` +
      `请保持微信在线；已有缓存则秒回。`;
  }
}

function setBusy(b) {
  state.running = b;
  for (const id of ['g-run', 'g-to3']) document.getElementById(id).disabled = b || !state.account;
  if (b) document.getElementById('g-finish').classList.add('hidden');
}

export async function init(view) {
  screen1();

  document.getElementById('g-start').addEventListener('click', () => screen2());
  document.querySelectorAll('[data-goto]').forEach(b => {
    b.addEventListener('click', () => {
      const s = Number(b.dataset.goto);
      (s === 1 ? screen1 : screen2)();
    });
  });
  document.getElementById('g-to3').addEventListener('click', screen3);

  document.getElementById('g-run').addEventListener('click', () => {
    if (state.running || !state.account) return;
    setBusy(true);
    document.getElementById('g3-progress').classList.remove('hidden');
    const logEl = document.getElementById('g3-log');
    logEl.classList.remove('hidden');
    logEl.innerHTML = '';
    document.getElementById('g3-result').innerHTML = '';
    startJob({ mode: 'auto', db_dir: state.account.db_dir },
      (logs) => renderLog(logEl, logs),
      (job) => {
        setBusy(false);
        document.getElementById('g3-progress').classList.add('hidden');
        if (job.error) {
          document.getElementById('g3-result').innerHTML =
            `<span class="badge badge-none">❌ ${esc(job.error)}</span>`;
          return;
        }
        const accs = (job.report && job.report.accounts) || [];
        const mine = accs.find(a => state.account && a.db_dir === state.account.db_dir) || accs[0];
        if (mine) {
          const full = mine.verified >= mine.total_salts;
          const dec = mine.decrypt;
          document.getElementById('g3-result').innerHTML =
            `<span class="badge ${full ? 'badge-full' : 'badge-part'}">密钥 ${mine.verified}/${mine.total_salts}</span>` +
            (dec ? ` <span class="badge badge-full">解密 ${dec.ok} 库（缓存 ${dec.cached || 0}）</span>` : '');
        }
        document.getElementById('g-finish').classList.remove('hidden');
      });
  });

  document.getElementById('g-finish').addEventListener('click', () => {
    setSetupDone();
    go('#/chat');
  });
}

export function destroy() { /* 无常驻定时器 */ }
