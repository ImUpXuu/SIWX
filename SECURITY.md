# 安全策略（Security Policy）

## 支持的版本

| 版本 | 支持状态 |
|---|---|
| 5.0.7+ | ✅ 支持（含 2026-10-03 安全审计修复：S1/S2/S5/S6 等） |
| < 5.0.7 | ❌ 不支持，请升级 |

## 如何报告漏洞

本项目涉及**密钥提取与调试器注入**等敏感能力，安全漏洞请**不要**直接开公开
issue 附带可利用细节。请优先使用 GitHub 的 [Private vulnerability reporting]
（仓库页 → Security → Report a vulnerability）私下报告。

请在报告中包含：

1. 受影响的版本 / 提交号；
2. 复现步骤或 PoC（仅在私有渠道提供）；
3. 影响面评估（是否涉及密钥泄露、任意代码执行）。

收到报告后我们会尽快确认并修复，修复发布后在 release notes 中致谢（除非你
希望匿名）。

## 安全设计要点（与 docs/audit-siwx-issues-2026-10-03.md 对应）

- **密钥存储**：Windows 上经 DPAPI（CryptProtectData + 项目熵）加密落盘；
  非 Windows 兜底为明文 JSON 但权限收紧至 0600（见 `siwx/keystore.py`）。
- **缓存清单**：`output/<wxid>/.siwx_cache.json` 不再持久化 SQLCipher 明文
  密钥（S6）；旧版落盘文件在下次解密时自动剥离重写。
- **自动更新链**（S1/S5）：
  - 更新 manifest 仅从服务端白名单域名拉取（`VERSION_URLS`）；
  - `POST /api/update/do` 不接受客户端提交的 manifest；
  - 下载/哈希 URL 受域名白名单约束；SHA-256 期望值缺失时**拒绝安装**（fail-closed）。
- **Web 控制台**（S2）：默认绑定 127.0.0.1 并校验 Host 头（缓解 DNS
  rebinding）；`--host` 指向非回环地址时必须显式 `--trust-lan` 确认。
- **威胁模型**：见 [THREAT_MODEL.md](THREAT_MODEL.md)。
