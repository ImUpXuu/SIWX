# 插件开发指南

> **API 版本**: 1 | **适用**: stories-in-wx 5.x | **语言**: Python 3.10+
>
> 插件的目标：**不用读宿主源码、不用 import siwx、不用改一行宿主代码**，
> 就能增加导出格式、页面、设置项、MCP 工具、CLI 命令与数据钩子。

---

## 一、60 秒上手

### 1. 放进插件目录

```
%LOCALAPPDATA%\stories-in-wx\plugins\
```

（macOS / Linux 为 `$LOCALAPPDATA` 缺省时的 `~/stories-in-wx/plugins/`，
实际路径见 `siwx.plugins.loader.plugins_root()`。启动时会自动创建该目录。）

### 2. 写一个单文件插件

`plugins/hello.py`：

```python
PLUGIN = {
    "name": "hello",
    "version": "1.0.0",
    "description": "最小示例",
    "pages": [{"name": "hello", "title": "你好", "icon": "👋"}],
    "settings": [{"key": "who", "type": "str", "default": "世界",
                  "label": "打招呼对象"}],
}
```

### 3. 重启应用

左侧菜单栏内建项**之后**会出现「👋 你好」，点进去就是插件页。
设置页会多出「hello」分组与「打招呼对象」输入框。

没有页面时插件依然生效 —— 只要声明了 `settings` / `mcp_tools` /
`export_formats` 中任意一项即可。

---

## 二、两种插件形态

| 形态 | 路径 | 页面资源目录 |
|---|---|---|
| **单文件** | `plugins/<name>.py` | `plugins/<name>.pages/` |
| **包** | `plugins/<name>/__init__.py` | `plugins/<name>/pages/` |

也可以在 `PLUGIN` 里显式指定（相对插件文件解析）：

```python
PLUGIN = {"name": "x", "pages_hint": "ui"}    # → plugins/ui/
PLUGIN = {"name": "x", "pages_dir": "C:/abs/path"}
```

**跳过规则**：以 `_` 或 `.` 开头、`__pycache__`、以 `.disabled` 结尾。
把插件改名为 `myplugin.py.disabled` 即可临时停用。

**两个插件同名** → 名字字典序在前的先加载，后者被跳过并在
`GET /api/plugins` 中记为 `degraded`（原因："与已加载插件同名"）。

---

## 三、PLUGIN 字典速查

```python
PLUGIN = {
    # ── 元信息（name 必需，其余可选）──────────────────
    "name": "myplugin",           # 唯一标识，[a-zA-Z0-9_.-]
    "version": "1.2.0",
    "author": "", "description": "", "homepage": "",
    "requires": ["pyyaml>=6"],    # 缺失 → 插件标记 degraded（仍加载）
    "api_version": 1,             # 高于宿主支持则拒绝加载

    # ── 页面资源定位（可选）─────────────────────────────
    "pages_hint": "pages",        # 相对插件文件
    "pages_dir": "",              # 绝对或相对路径，优先级最高

    # ── 17 类 hook（全部可选）───────────────────────────
    "pages": [...],               # 左侧菜单平级页
    "settings": [...],            # 设置页表单
    "renderers": [...],           # 消息渲染（按 local_type 单赢家）
    "message_decorators": [...],  # 逐条消息打补丁
    "session_decorators": [...],  # 逐会话打补丁
    "content_transformers": [...],# 批量改写正文
    "session_filters": [...],     # 会话列表筛选
    "avatar_resolvers": [...],    # 头像来源
    "media_providers": [...],     # 媒体来源
    "mcp_tools": [...],           # MCP 工具
    "export_formats": [...],      # 新增导出格式
    "after_export": [...],        # 导出后处理
    "cli": [...],                 # CLI 子命令
    "task_listeners": [...],      # 任务生命周期
    "themes": [...],              # 注入 CSS
    "key_strategies": [...],      # 密钥提取策略
    "routes": [...],              # 轻量路由
    "api_blueprints": [...],      # 完整 Flask 蓝图
}
```

### 函数一律用字符串名引用

**这是最重要的约定**。插件里不要 import siwx、不要把函数对象塞进字典，
而是写同模块顶层函数的**字符串名**：

```python
def my_render(msg, ctx):
    return {"kind": "custom", "render": [...]}

PLUGIN = {"name": "p", "renderers": [
    {"local_types": [50], "render": "my_render"},   # ← 字符串，不是 my_render
]}
```

宿主在注册时才把字符串解析成函数。这样：

- 插件无需 `import siwx`，不会产生循环依赖
- 字典字面量在模块 import 期求值，不会遇到"引用了后面才定义的函数"

解析失败（名字拼错）只跳过该条目并记 warn，同插件其它 hook 照常生效。

---

## 四、hook 详解

### 4.1 `pages` —— 左侧菜单平级页

```python
"pages": [{
    "name": "stats",                     # 插件内唯一
    "title": "统计面板",
    "icon": "📊",
    "order": 10,                         # 越小越靠前
    "entry": "index",                    # → index.html/.js/.css
    "badge": "NEW",                      # 菜单右侧小徽标（可选）
    "tip": "鼠标悬停提示",                # 可选
    "group": "分析",                      # 可选，写到 data-group
    "condition": {"requires_decrypted": True},
}]
```

- 菜单项**追加在内建 6 项之后**，样式与高亮逻辑与内建项完全一致
- 页面 id 全局化为 `<插件名>:<name>`（前端路由 `#/demo_stats:stats`）
- 内建页名（`guide` / `chat` / `export` / `mcp` / `logs` / `settings`）被保留，插件不得占用

页面三件套的模块契约与内建页完全相同：

```js
// index.js
export async function init(view) { /* view 是容器元素 */ }
export function destroy() { /* 页面切走时调用，清理定时器 */ }
```

`index.html` 只写内容片段（无 `<html>/<head>`），`index.css` 自动注入。

**显示条件**（服务端求值，见 §4.9）：

| 键 | 含义 |
|---|---|
| `requires_decrypted` | 存在至少一个已解密账号 |
| `requires_account` | 指定账号名必须已解密 |
| `wechat_running` | 微信进程在线 |
| `platform` | `"windows"` / `"macos"` / `"linux"`，或列表（任一） |
| `env` | 环境变量存在且为真；或 `{"NAME": "期望值"}` |
| `min_version` | 宿主版本下限 |
| `config` | 插件自身配置项等于期望值 |
| `any_of` | 子条件列表，任一满足即可 |

所有原子条件之间是 **AND**；`any_of` 内部是 OR。

### 4.2 `settings` —— 设置项

```python
"settings": [{
    "key": "refresh_seconds", "group": "统计面板",
    "type": "int",                      # str/int/float/bool/choice/text
    "label": "自动刷新间隔（秒）",
    "default": 30, "min": 5, "max": 600,
    "help": "统计页轮询频率",
}]
```

| type | 控件 | 说明 |
|---|---|---|
| `str` | 单行文本 | 缺省类型 |
| `text` | 多行文本 | |
| `int` / `float` | 数字输入 | `min` / `max` 越界 → 回退默认值 |
| `bool` | 开关 | |
| `choice` | 下拉 | 需给 `choices: [...]`，非法值回退默认 |

配置存储在 `%LOCALAPPDATA%\stories-in-wx\plugin_config\<插件名>.json`
（原子写、每插件互斥锁、懒创建）。

插件代码里读取自己的配置：

```python
from siwx.plugins import registry
from siwx.plugins import config as pcfg

schema = registry.schema_for("myplugin")
values = pcfg.load("myplugin", schema)      # {key: value}，已按 schema 校验
```

或走 HTTP（插件页 JS 常用）：

```js
const { values } = await (await fetch('/api/plugins/config?plugin=myplugin')).json();
```

写入只接受 schema 里声明过的键，未声明的字段被忽略并记 warn。

### 4.3 `renderers` —— 消息渲染器

按 `local_type` 匹配，**单赢家**（多个插件声明同一 type 时 priority 高者胜）。

```python
def render_call(msg, ctx):
    return {
        "kind": "plugin-call",
        "render": [                          # 结构化节点树，不是 HTML！
            {"tag": "span", "cls": "plg-call", "c": [
                {"t": "text", "v": "📞 "},
                {"tag": "b", "v": "语音通话"},
            ]},
        ],
    }

"renderers": [{"local_types": [50], "render": "render_call",
               "kind": "plugin-call", "priority": 10}]
```

- `local_types`：int 或 int 列表
- `msg` 是**前端形状**完整消息：`id` / `ts` / `type` / `kind` /
  `sender_wxid` / `sender_name` / `text` / `md5` / ...
- 返回 dict，只有 `kind` / `render` / `text` / `extra` 四个键会被采纳
  （防止插件污染整条消息）
- 返回非 dict（如 HTML 字符串）→ 忽略

#### 节点树语法（`render` 字段）

```js
{t: 'text',  v: '纯文本'}                       // 自动转义
{t: 'el',    tag: 'div', cls: 'x', v: '文本',
             a: {title: '...'}, c: [子节点]}     // 元素
{t: 'img',   src: '/api/chat/media/image?...'}  // src 仅允许同源/相对
{t: 'a',     href: 'https://...', v: '链接'}     // 仅 http/https/相对
{t: 'raw',   v: '<b>x</b>'}                     // 显式 HTML —— 被忽略
```

数组即多个节点。**标签白名单**：`span div b i em strong code pre p br
ul ol li small table thead tbody tr th td`；不在白名单内的标签降级为 `span`。
**属性白名单**：`class title style colspan rowspan`；`on*` 事件属性与
`href`/`src` 一律拒绝，`style` 里含 `url()`/`expression`/`javascript:` 时被清空。

这套设计从根上杜绝了插件注入 HTML 造成的 XSS —— 插件**没有**输出 HTML 的通道。

### 4.4 `message_decorators` / `session_decorators`

对每条消息 / 每个会话做**浅合并补丁**（fan-out，所有插件都会跑）：

```python
def tag(msg, ctx):
    return {"plugin_mine": True}          # 合并进 msg

"message_decorators": [{"name": "tag", "decorate": "tag",
                        "priority": 0, "timeout_ms": 50, "hot": False}]
```

**热路径守卫（重要）**：

- 默认 `hot=False` —— 只在非交互热路径运行，**不拖慢聊天页**
- 需要每条消息实时生效才声明 `hot=True`，代价是每条消息都跑一次插件
- 无论是否 hot，都受 `timeout_ms`（默认 50ms，上限 200ms）约束；
  超时则该插件被 **熔断 60 秒**（期间跳过，不再排队），并记录 warn
- 插件抛异常只丢失该条数据的补丁，不会 500

### 4.5 `content_transformers` —— 正文改写

```python
def polish(text, msg, ctx):
    return text.replace("哈哈哈哈", "[狂笑]").replace("哈哈", "[笑]")

"content_transformers": [{"name": "polish", "transform": "polish",
                          "priority": 0}]
```

按 priority 降序串联，前一层的输出是后一层的输入。返回非字符串则跳过该层。
适合术语替换、脱敏、繁简转换。

### 4.6 `session_filters` —— 会话筛选

```python
def keep(session, ctx):
    return not session.get("is_official")     # False = 剔除

"session_filters": [{"name": "no_official", "fn": "keep"}]
```

返回 `False` 剔除，`None` / `True` 保留。插件异常时**保守保留**会话
（宁可多显示也不要莫名丢数据）。

### 4.7 `export_formats` —— 新增导出格式

```python
def write_tsv(path, ctx):
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("localId\tcreateTime\tcontent\n")
        for m in ctx["stream"]:
            f.write(f"{m['localId']}\t{m['createTime']}\t"
                    f"{(m['content'] or '').replace(chr(9), ' ')}\n")
            n += 1
    return n

"export_formats": [{"fmt": "tsv", "ext": "tsv",
                    "label": "TSV（制表符，插件提供）",
                    "writer": "write_tsv"}]
```

**写入器契约**：`writer(path, ctx) -> int`（返回写入条数）。

`ctx` 关键字段：

| 键 | 说明 |
|---|---|
| `stream` | **惰性消息生成器**，只能迭代一次（需多次遍历请自行 `list()` 缓存） |
| `session` | 会话元信息 |
| `names` | `{username: display}` |
| `media_map` | `{localId: 相对媒体路径}` |
| `account` / `chat` / `start_ts` / `end_ts` | 导出参数 |
| `out_dir` | 账号输出目录 |
| `progress` | `progress(pct, msg)` |
| `fmt` / `ext` | 本次格式与扩展名 |

**`stream` 里是「导出形状」的键名，不是前端形状**，切勿混用：

```
localId / platformMessageId / createTime / localType / typeName /
rawContent / content / isSend / senderUsername / senderDisplayName /
md5 / bubbleMd5 / voice / quote / link / mediaFile
```

`content` 已按类型格式化（图片为 `"[图片]"` 等）。

插件格式**追加在内建 8 种之后**；与内建同名的格式（如 `json`）由内建实现优先，
插件声明会被忽略。`GET /api/export/formats` 会返回 `owner` 标注来源。

### 4.8 `after_export` —— 导出后处理

```python
def write_stats(ctx):
    import json, os
    out_dir = ctx.get("out_dir") or ctx.get("export_dir")
    if not out_dir or not os.path.isdir(out_dir):
        return
    with open(os.path.join(out_dir, "_stats.json"), "w", encoding="utf-8") as f:
        json.dump({"count": ctx.get("message_count")}, f, ensure_ascii=False)

"after_export": [{"name": "stats", "run": "write_stats", "when": "before_zip"}]
```

| `when` | 时机 | `ctx` 里有什么 |
|---|---|---|
| `before_zip`（默认） | 文件写完、**打包 zip 之前** | `export_dir` / `file` / `format` / `message_count` / `account` / `chat` / `display` / `names` / `media_map` / `progress` |
| `after_zip` | zip 生成且 **export_dir 已被删除**之后 | `export_dir=None`、`zip=<zip路径>`、其余同上 |

**时序陷阱**：`pack=zip` 时宿主会在生成 zip 后 `rmtree` 掉导出目录。
想在导出目录里落文件，必须用 `when="before_zip"`；`after_zip` 阶段只剩 zip 文件本身。

### 4.9 `mcp_tools` —— MCP 工具

```python
def greet(args):
    who = (args or {}).get("who") or "世界"
    return {"content": [{"type": "text", "text": f"你好，{who}！"}]}

"mcp_tools": [{
    "name": "plugin_greet",
    "description": "打招呼",
    "input_schema": {"type": "object",
                     "properties": {"who": {"type": "string"}}},
    "handler": "greet",
}]
```

- `input_schema`（也接受 `inputSchema`）
- `handler(args) -> dict | str`：返回 MCP `content` 结构或纯字符串，两者都支持
- 与内建 6 个工具**重名则跳过**（内建优先），`/api/mcp/info` 返回
  `owner: "plugin"` 便于前端分组
- 工具受 MCP 配置页开关控制，与内建工具同一套机制

### 4.10 `cli` —— CLI 子命令

```python
def cli_stats(args):
    print("limit =", getattr(args, "limit", 10))
    return 0

"cli": [{
    "name": "demo-stats",
    "help": "[插件] 打印账号概览",
    "handler": "cli_stats",
    "args": [
        {"name": "--account", "type": "str", "default": "", "help": "账号"},
        {"name": "--limit", "type": "int", "default": 10, "help": "条数"},
    ],
}]
```

`type` 支持 `str` / `int` / `float` / `flag`（store_true）/ `count`。
命令名与内建（`auto` / `keys` / `decrypt` / `serve` / `mcp`）冲突时跳过。
`handler(args) -> int` 返回退出码，异常被捕获并转为退出码 1。

```
python run.py demo-stats --limit 5
```

### 4.11 `task_listeners` —— 任务生命周期

```python
def on_task(event, ctx):
    from siwx import logger as log
    if event == "start":
        log.info("plugin", f"任务开始 mode={ctx.get('mode')}")
    elif event == "done":
        log.info("plugin", f"完成 ok={ctx.get('ok')} 耗时={ctx.get('duration_ms')}ms")

"task_listeners": [{"name": "tasklog", "on_event": "on_task"}]
```

| event | ctx |
|---|---|
| `start` | `mode` / `db_dir` / `out_dir` / ... |
| `done` | `mode` / `ok` / `report` 或 `error` / `duration_ms` |

**同步执行**：回调在任务线程里跑，务必快进快出。异常只记日志，不影响任务。

### 4.12 `themes` —— 注入 CSS

```python
# 形态一：文件名（宿主注入 <link>，文件需在插件页面资源目录内）
"themes": [{"name": "skin", "css": "theme.css", "priority": 0}]

# 形态二：函数返回 CSS 文本（内联注入）
"themes": [{"name": "inline", "css": "make_css"}]

# 简写：字符串即文件名
"themes": ["theme.css"]
```

- 文件名形态只允许**纯文件名**，含 `/` `\` `..` 会被拒绝（防目录穿越）
- 注入在首页 `<head>` 内、内建样式之后
- 建议只用 CSS 变量做轻度品牌化，不要改布局（会影响内建页）

### 4.13 `key_strategies` —— 密钥提取策略

```python
def my_extract(ctx) -> int:
    from siwx.sqlcipher import verify_enc_key
    found = 0
    for salt, page1 in ctx["page1_by_salt"].items():
        if salt in ctx["key_map"]:
            continue
        cand = my_logic(salt)
        if cand and verify_enc_key(bytes.fromhex(cand), page1):
            ctx["key_map"][salt] = cand.lower()
            ctx["attrib"][salt] = "myplugin"
            found += 1
    return found

"key_strategies": [{"name": "mine", "fn": "my_extract",
                    "process_dependent": False}]
```

- 与内建策略同签名：`extract(ctx) -> int`，**追加在内建 4 个策略之后**
- `ctx`：`db_dir` / `entries` / `page1_by_salt` / `key_map` / `attrib` /
  `log` / `use_memory`
- **propose-verify 分离**：策略只把候选写进 `key_map`，宿主统一用
  `verify_enc_key()` 做 HMAC 验证。请不要跳过验证直接写库。
- `process_dependent: True` 表示需要读微信进程内存；`use_memory=False`
  时该策略被跳过

### 4.14 `avatar_resolvers` / `media_providers`

```python
def my_avatar(username, ctx):
    data = lookup(username)          # 返回图片字节
    return data or None              # None → 交回内建实现

"avatar_resolvers": [{"name": "a", "resolve": "my_avatar"}]
```

头像解析器在**内建实现查不到时**才被询问（内建优先）。
返回 `bytes` 则采用，`None` 表示交给下一个或内建实现。

媒体来源同构：`{"kind": "image", "provide": "fn"}`，用 `kind` 区分媒体类型。

### 4.15 `routes` / `api_blueprints`

**轻量路由**（`routes`，函数形态）与**完整 Flask 蓝图**（`api_blueprints`）二选一。
需要 `url_prefix`、多方法、`jsonify` 等完整能力时用蓝图：

```python
def make_blueprint():
    from flask import Blueprint, jsonify
    bp = Blueprint("my_api", __name__, url_prefix="/api/myplugin")

    @bp.get("/ping")
    def _ping():
        return jsonify({"ok": True})

    return bp

"api_blueprints": ["make_blueprint"]     # 字符串 → 解析为工厂函数后调用
```

也接受直接给 `Blueprint` 对象或 `{"bp": ...}` 包壳。
蓝图名与已注册者冲突则跳过并记 warn。

> 插件蓝图挂在**同一个 Flask 应用**上，与宿主共享进程。
> 请自行保证路由前缀不与宿主冲突（建议统一用 `/api/<插件名>/`）。

---

## 五、隔离与错误处理

设计原则：**插件出问题，最坏情况只是这个插件自己不工作。**

| 阶段 | 隔离方式 | 后果 |
|---|---|---|
| 导入（`import`） | 逐插件 `try/except BaseException` | 报告记 `error`，其余插件照常 |
| 契约解析 | 逐 hook `try/except` | 该 hook 条目跳过 + warn |
| hook 调用 | 逐条目 `try/except` | 该条数据少一个补丁，不 500 |
| 装饰器超时 | `timeout_ms` + 60s 熔断 | 超时插件被临时停用 |
| 依赖缺失 | `importlib.util.find_spec` 探测 | 标记 `degraded`，hook 仍注册 |

加载状态查询：

```bash
GET /api/plugins
→ {"plugins": [{"name","version","source","status","hooks",
                "missing_requires","error"}],
   "counts": {"ok":1,"degraded":0,"error":0},
   "hooks": {"pages":2,"settings":3,...}}
```

`status` 三态：`ok` / `degraded`（依赖缺失或名字冲突被跳过）/ `error`（加载失败）。
`error` 字段是 `[阶段] 异常摘要`，已脱敏并截断到 300 字符。

**排障开关**：设 `SIWX_NO_PLUGINS=1` 可以完全禁用插件加载（零插件模式），
用于确认某个异常是否由插件引起。

---

## 六、开发者提示

### 热路径开销

聊天页每次翻页都会跑该页所有消息的渲染器、装饰器与内容转换器。三条建议：

1. 不需要每条消息实时生效的装饰器，**不要**声明 `hot=True`
2. 渲染器里避免网络/磁盘 IO
3. 内容转换器对长文本用字符串方法而非正则回溯

### 零插件零开销

宿主每个调用点都写成 `if not registry.<hook>: <原逻辑>`，因此
**没有插件时行为与不引入插件系统时逐字节一致**。你的插件不应假设
"一定有别的插件在场"。

### 不要 import siwx

契约层的字符串引用已经避免了这一点（见 §3）。确实需要在运行时用宿主能力
（如 `verify_enc_key`、`logger`、`paths`）时，在**函数体内**延迟 import：

```python
def my_handler(args):
    from siwx import logger as log       # 函数体内，不是模块顶层
    log.info("plugin", "hello")
```

模块顶层的 `import siwx` 可能在插件加载阶段引发循环依赖。

### 命名建议

- 插件名加前缀避免撞车：`myname.stats` / `myname_export`
- MCP 工具名建议 `plugin_<插件>_<动作>`
- 页面 CSS 用页面级选择器或 `--<插件名>-*` 变量，避免污染宿主

---

## 七、完整示例

仓库 `examples/plugins/demo_stats/` 是一个包形态插件，演示了全部 13 类已实现 hook：

```
examples/plugins/demo_stats/
├── __init__.py     # PLUGIN 字典 + 全部回调函数
└── pages/
    ├── index.html  # 页面片段
    ├── index.js    # init/destroy 模块
    ├── index.css   # 页面样式
    └── theme.css   # 注入式主题
```

安装到本机试用：

```bash
# Windows
xcopy /E /I examples\plugins\demo_stats "%LOCALAPPDATA%\stories-in-wx\plugins\demo_stats"

# macOS / Linux
cp -R examples/plugins/demo_stats "$LOCALAPPDATA/stories-in-wx/plugins/"
```

重启后可见：左侧「📊 统计面板」（带 DEMO 徽标）、设置页「统计面板」分组
3 项配置、导出下拉多出「TSV（制表符，插件提供）」、
`python run.py demo-stats --limit 5`、MCP 工具 `plugin_demo_greet`。

---

## 八、测试自己的插件

宿主回归套件通过 `SIWX_NO_PLUGINS=1` 与用户插件解耦；
插件自身的测试可以这样写（参考 `tests/test_plugins.py`）：

```python
import os, sys
sys.path.insert(0, ".")                      # 项目根

from siwx.plugins import registry, contract

def test_my_plugin_registers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("myplug", "myplug.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    counts = contract.register_plugin(mod, registry, mod.PLUGIN)
    assert counts.get("pages") == 1
```

关键点：`contract.register_plugin(module, registry, PLUGIN_dict)` 是
**纯函数式的注册入口** —— 不需要真去扫描目录、不需要重启应用。
