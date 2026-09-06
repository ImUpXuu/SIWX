/* 设置页 —— 缓存总览 / 清除 / 重新运行引导 */
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

function confirmThen(msg, fn) {
  if (window.confirm(msg)) fn();
}

export async function init() {
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
}

export function destroy() { /* 无常驻定时器 */ }
