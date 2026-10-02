#!/usr/bin/env bash
# 一键推送 SIWX issue #3 修复并创建 PR
#
# 前置条件：GitHub 连接器已授权（或已 export GITHUB_TOKEN）
#   CodeBuddy 设置页 →「连接器」→ GitHub 授权
#
# 用法： bash /workspace/siwx/push_and_pr.sh
set -euo pipefail

REPO="ImUpXuu/SIWX"
BRANCH="fix/macos-key-extraction-0n"
BASE="main"
TITLE="fix: 修复 macOS 端密钥提取失败（0/N）"

cd "$(dirname "$0")"

# 1) 取 token
if [ -z "${GITHUB_TOKEN:-}" ]; then
  source /root/.codebuddy/skills/github-connector/scripts/get_token.sh github
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "✗ GITHUB_TOKEN 为空：请在 CodeBuddy 设置页的「连接器」处授权 GitHub" >&2
  exit 1
fi

# 2) 建分支并推送（仅代码修复进 PR，PR 描述作为 PR body）
git checkout -B "$BRANCH"
git remote set-url origin "https://oauth2:${GITHUB_TOKEN}@github.com/${REPO}.git"
git push -u origin "$BRANCH"

# 3) 创建 PR，body 取 PR_DESCRIPTION.md
BODY=$(python3 -c "import json,sys;print(json.dumps(open('PR_DESCRIPTION.md',encoding='utf-8').read()))")
curl -s -X POST \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/${REPO}/pulls" \
  -d "{\"title\":$(python3 -c "import json;print(json.dumps('$TITLE'))"),\"head\":\"${BRANCH}\",\"base\":\"${BASE}\",\"body\":${BODY}}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('PR:', d.get('html_url') or d)"

# 4) 在 issue #3 下留言
curl -s -X POST \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/${REPO}/issues/3/comments" \
  -d '{"body":"问题已定位并修复，PR 见上方关联提交。根因是 macOS 端密钥提取脚本在加载阶段即中断，与微信登录状态无关。新版日志会输出断点位置数，方便区分失败环节。"}' \
  > /dev/null && echo "已在 issue #3 留言"
