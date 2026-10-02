#!/bin/bash
# stories-in-wx macOS 自动更新脚本
# 用法: bash scripts/update_mac.sh

set -euo pipefail

REPO="ImUpXuu/SIWX"
RAW="https://raw.gh.1s.fan/${REPO}/main"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "============================================"
echo "  stories-in-wx 自动更新 (macOS)"
echo "============================================"
echo ""

# ── 1. 获取远程版本信息 ──────────────────────
echo "[1/5] 检查新版本..."
REMOTE_JSON=$(curl -fsSL "${RAW}/version.json" 2>/dev/null) || {
    echo "[错误] 无法获取版本信息，请检查网络连接"
    exit 1
}
NEW_VER=$(echo "$REMOTE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])")
echo "      新版本: v${NEW_VER}"

# ── 2. 下载新产物 ──────────────────────────────
ASSET_URL="https://github.com/${REPO}/releases/download/v${NEW_VER}/stories-in-wx-v${NEW_VER}-macos.dmg"
DMG_FILE="${DIR}/stories-in-wx-v${NEW_VER}-macos.dmg"

echo "[2/5] 下载新产物..."
curl -fsSL -L "${ASSET_URL}" -o "${DMG_FILE}" 2>/dev/null || {
    echo "[错误] 下载失败，请检查网络或手动下载"
    exit 1
}
echo "      下载完成: ${DMG_FILE}"

# ── 3. 校验 SHA-256 ────────────────────────────
echo "[3/5] 校验文件完整性..."
SHA_URL=$(echo "$REMOTE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('sha256',''))")
EXPECTED_SHA=$(curl -fsSL "${SHA_URL}" 2>/dev/null | grep -i "dmg" | awk '{print $1}')
ACTUAL_SHA=$(shasum -a 256 "${DMG_FILE}" | awk '{print $1}')
if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
    echo "[警告] SHA-256 校验失败"
    echo "  期望: $EXPECTED_SHA"
    echo "  实际: $ACTUAL_SHA"
    exit 1
fi
echo "      校验通过"

# ── 4. 挂载 DMG 并替换 ────────────────────────
echo "[4/5] 替换旧版本..."
# 杀掉旧进程
pkill -f "stories-in-wx" 2>/dev/null || true
sleep 2

# 挂载
MOUNT_POINT=$(hdiutil attach "${DMG_FILE}" -readonly -nobrowse | tail -1 | awk '{print $3}')
sleep 1

# 备份旧版本
if [ -d "${DIR}/stories-in-wx.app" ]; then
    cp -R "${DIR}/stories-in-wx.app" "${DIR}/stories-in-wx.backup.app" 2>/dev/null || true
fi

# 替换
rm -rf "${DIR}/stories-in-wx.app"
cp -R "${MOUNT_POINT}/stories-in-wx.app" "${DIR}/"

# 卸载
hdiutil detach "${MOUNT_POINT}" -quiet 2>/dev/null || true
echo "      替换完成"

# ── 5. 清理并启动 ──────────────────────────────
echo "[5/5] 启动新版本..."
rm -f "${DMG_FILE}"

echo ""
echo "============================================"
echo "  更新完成！正在启动 stories-in-wx..."
echo "============================================"
open "${DIR}/stories-in-wx.app" --args serve 2>/dev/null || true
sleep 3
exit 0
