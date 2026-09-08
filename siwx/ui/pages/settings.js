/* 设置页 —— 版本与更新 / 缓存总览 / 清除 / 重新运行引导 */
const { esc, fetchJSON, go, resetSetup } = window.SX;

function el(id) { return id ? document.getElementById(id) : null; }

async function loadOverview() {
  const box = el('s-overview');
  try {
    const o = await fetchJSON('/api/settings/overview');
    const rows = o.outputs.length ? o.outputs.map(x => `
      <div class="ov-row">
        <span>${esc(x.wxid)}</span>
        <span class="dim">${x.size_mb} MB · ${x.manifest ? '解密缓存 ✓' : '无缓存清单'}</span>
        <span class="pill pill-gray">明文产物</span>
      </div>`).join('')
      : '<div class="empty">还没有解密输出</div>';
    box.innerHTML = `
      <div class="ov-row">
        <span>密钥库（DPAPI 加密）</span>
        <span class="dim">${esc(o.keystore.path)}</span>
        <span class="pill pill-green">${o.keystore.count} 条密钥</span>
      </div>
      ${rows}`;
  } catch (e) {
    box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

async function loadVersion() {
  const box = el('s-version');
  const meta = el('s-version-meta');
  try {
    const cur = await fetchJSON('/api/update/current');
    const chk = await fetchJSON('/api/update/check');
    const lines = [];
    lines.push(`<div class="ov-row"><span>当前版本</span><span class="pill pill-green">v${esc(cur.version)}</span></div>`);
    lines.push(`<div class="ov-row"><span>运行模式</span><span class="dim">${cur.frozen ? '打包产物' : '源码运行'}</span></div>`);
    lines.push(`<div class="ov-row"><span>平台</span><span class="dim">${esc(cur.platform)}</span></div>`);

    if (chk.has_update && chk.remote) {
      const rv = chk.remote.version;
      const notes = esc((chk.remote.notes || "").slice(0, 200));
      lines.push(`<div class="ov-row"><span>最新版本</span><span class="pill pill-amber">v${esc(rv)} ↗</span></div>`);
      lines.push(`<div class="ov-row"><span>更新内容</span><span class="dim">${notes}</span></div>`);
      if (chk.update_available) {
        lines.push(`<div class="ov-row"><button class="btn btn-primary" id="s-do-update">⬆ 更新到 v${esc(rv)}</button></div>`);
      } else {
        lines.push(`<div class="ov-row"><span class="dim">源码运行模式，请手动 git pull 更新</span></div>`);
      }
    } else {
      lines.push(`<div class="ov-row"><span>状态</span><span class="pill pill-green">✓ 已是最新</span></div>`);
    }
    box.innerHTML = lines.join('');
    meta.textContent = `打包产物: ${cur.frozen} · 平台: ${cur.platform}`;

    const btn = el('s-do-update');
    if (btn) {
      btn.addEventListener('click', async () => {
        if (!window.confirm('确定更新？应用将自动重启。')) return;
        btn.disabled = true;
        btn.textContent = '⏳ 更新中...';
        try {
          const r = await fetchJSON('/api/update/do', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify(chk.remote || {}),
          });
          if (r.ok) {
            window.alert(r.message || '更新已启动，应用将重启');
          } else {
            window.alert('更新失败: ' + (r.message || '未知错误'));
            btn.disabled = false;
            btn.textContent = `⬆ 更新到 v${esc(chk.remote?.version || '')}`;
          }
        } catch (e) {
          window.alert('更新失败: ' + e.message);
          btn.disabled = false;
        }
      });
    }
  } catch (e) {
    box.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    meta.textContent = '版本检测失败';
  }
}

function confirmThen(msg, fn) {
  if (window.confirm(msg)) fn();
}

export async function init() {
  await loadVersion();
  await loadOverview();

  el('s-clear-output').addEventListener('click', () =>
    confirmThen('确定删除全部解密输出？\n（密钥保留，重跑解密即可恢复）', async () => {
      await fetchJSON('/api/settings/clear', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ kind: 'output' }),
      });
      window.alert('已清除解密输出');
      loadOverview();
    }));

  el('s-clear-keys').addEventListener('click', () =>
    confirmThen('确定清除密钥库？\n下次提取需要微信在线重新收割。', async () => {
      await fetchJSON('/api/settings/clear', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ kind: 'keys' }),
      });
      window.alert('已清除密钥库');
      loadOverview();
    }));

  el('s-rerun-guide').addEventListener('click', () => {
    resetSetup();
    go('#/guide');
  });

  // ── 日志模式 ──────────────────────────────────────────
  async function loadLogSettings() {
    try {
      const s = await fetchJSON('/api/logs/settings');
      el('s-log-level').value = s.level || 'rough';
    } catch (e) { /* ignore */ }
  }

  el('s-log-level').addEventListener('change', async (e) => {
    await fetchJSON('/api/logs/settings', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ level: e.target.value }),
    });
    window.alert(`日志模式已切换为: ${e.target.value === 'detailed' ? '详细' : '粗略'}`);
  });

  // ── 脱敏日志导出 ──────────────────────────────────────
  el('s-export-log').addEventListener('click', async () => {
    const desensitize = el('s-log-desensitize').checked;
    const url = `/api/logs/export?desensitize=${desensitize ? '1' : '0'}`;
    try {
      const r = await fetch(url);
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `siwx_log_${Date.now()}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      window.alert('导出失败: ' + e.message);
    }
  });

  await loadLogSettings();
}

export function destroy() { /* no timers */ }
