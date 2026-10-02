# module-plugins — 插件系统架构

> **目录**: `siwx/plugins/` | **角色**: 中央注册表 + 17 类 hook 命名空间 + 目录自动发现
>
> 面向插件**开发者**的使用说明见 [plugin-development.md](./plugin-development.md)；
> 本文面向宿主的**维护者**，解释内部结构与设计取舍。

---

## 设计目标

1. **内置不动** —— 内建 8 种导出格式、4 个密钥策略、6 个 MCP 工具、6 个 UI 页面
   全部留在原处，作为"默认层"。注册表**只装插件层**。
2. **零插件零开销** —— 消费方按「内置 → 插件」合并，零插件时行为与不引入
   插件系统时逐字节一致。
3. **最小摩擦** —— 插件不 import siwx，用纯声明式 `PLUGIN` 字典 +
   字符串函数名引用。
4. **失败可见但不致命** —— 任何插件异常都降级为日志 + 加载报告条目。

---

## 模块布局

```
siwx/plugins/
├── __init__.py       # 对外导出（registry / load_all / ensure_loaded ...）
├── registry.py       # 中央注册表 + 数据结构 + 命名空间
├── contract.py       # PLUGIN 字典校验 + 字符串函数名解析
├── loader.py         # 用户级目录扫描 + importlib 装载 + 隔离
├── conditions.py     # 声明式显示条件求值器
├── config.py         # 每插件配置存储（原子写 + 互斥锁 + schema 校验）
├── report.py         # 加载报告（ok / degraded / error 三态）
└── chat_bridge.py    # registry ↔ api_chat 的桥接（热路径守卫集中处）
```

**依赖方向（严禁打破）**：

```
contract  ← loader
registry  ← contract, loader, conditions, chat_bridge
```

`loader.py` **不得在顶层 import 任何业务模块**（`strategies` / `exporter` /
`server` / `mcp_server` / `api_*`），否则会形成循环依赖。它只依赖
`paths` + `logger` + `report`。

---

## 中央注册表

```python
# siwx/plugins/registry.py —— 模块级单例
registry = PluginRegistry()
```

全系统唯一实例。宿主各处（`api_chat` / `exporter` / `mcp_server` /
`cli` / `server`）都持有同一个引用，因此**重置内容而非重建对象**是
测试夹具的正确做法。

### 数据结构

| 数据类 | 用途 | 去重键 |
|---|---|---|
| `PluginMeta` | 插件元信息 | `name` |
| `ExportFormat` | 导出格式 | `fmt` |
| `KeyStrategy` | 密钥策略 | `name` |
| `McpTool` | MCP 工具 | `name` |
| `ApiBlueprint` | Flask 蓝图 | 蓝图名 |
| `UiPage` | 菜单页 | `name` |
| `SettingItem` | 设置项 | `plugin` + `key` |
| `Renderer` | 消息渲染器 | `插件名:函数名` |
| `Decorator` | 消息/会话装饰器 | `插件名:函数名` |
| `CliCommand` | CLI 命令 | `name` |
| `AfterExport` | 导出后处理 | `插件名:函数名` |
| `GenericHook` | 薄 hook 通用载体 | `name` 或 `key` 或函数标识 |

### 命名空间

全部继承 `_Namespace`，提供：

```python
ns.add(item)          # 追加
ns.append(item)       # add 的别名（列表式写法）
ns.entries()          # 全部条目（声明顺序，未排序）
len(ns) / bool(ns)    # 计数 / 是否为空
ns.sorted_items()     # priority 降序 → 插件名 → 声明索引（三级稳定排序）
```

`bool(ns)` 是所有热路径短路的依据：

```python
if not registry.pages:
    return <原逻辑>          # 零插件时直接走原路径
```

特化命名空间：

| 类 | 额外能力 |
|---|---|
| `KeyStrategyNS` | `process_dependent()` → 需读进程内存的策略函数集合 |
| `RendererNS` | `for_type(local_type)` → 按类型取 priority 最高的单赢家 |
| `DecoratorNS` | `apply(target, ctx, hot)` → 带超时 + 熔断的补丁合并 |
| `BlueprintNS` | 蓝图命名空间 |
| `PageNS` | `after_builtin(ctx)` / `all_pages()` / `get(name)` / `owner_of(name)` |

### 确定性排序

```python
sorted(items, key=lambda pair: (
    -priority,                      # 高优先级在前
    meta.name if meta else "",      # 同优先级按插件名字典序
    declaration_index,              # 再按声明顺序（稳定）
))
```

三级排序保证：同样的插件集合 → 同样的执行顺序，与扫描顺序、文件系统
返回顺序无关。这对"单赢家"语义（渲染器）是必需的。

---

## 契约层

### 字符串函数名解析

```python
def resolve_callable(module, name):
    """字符串 → 同模块顶层可调用；失败返回 None。"""
```

**为什么不直接放函数对象**：`PLUGIN` 是模块级字典字面量，在 import 期求值。
若写 `{"render": my_render}`，而 `my_render` 定义在字典之后，会立刻 NameError。
用字符串名把解析推迟到注册期，同时让插件无需 import siwx。

### 注册流程

```
register_plugin(module, registry, data)
  ├── meta_from_dict(data)          # 支持扁平与 {"meta": {...}} 两种写法
  ├── api_version 检查              # 高于宿主支持 → ValueError
  ├── _resolve_pages_root(...)      # pages_dir > pages_hint > 自动探测
  ├── 逐 hook 调 builder（try/except 隔离）
  │     ├── 去重检查 _is_duplicate
  │     └── target.add(item)
  └── api_blueprints 单独处理（支持字符串/对象/工厂/dict 包壳）
```

单个 hook 的 builder 抛异常 → 记 warn，继续下一个 hook。

### 去重语义

`_dup_key(item)` 决定"什么算重复"：

1. 有显式名字（`name` / `fmt` / `key`）→ 用名字
2. 否则用**函数标识** `插件名:函数名`
3. 都没有 → 用对象 `id()`，即**不去重**（例如函数用 lambda 且无名）

> 历史坑：早期版本第 2 步只取 `__name__`，导致没有 `name` 的
> `Renderer` / `Decorator` / `AfterExport` 全部退化成同一个 `"?"`，
> 不同插件的无关 hook 被误判为冲突而丢弃。
> `tests/test_plugins.py` 的 `test_renderer_single_winner_by_priority`
> 覆盖此回归。

内建页名（`guide` / `chat` / `export` / `mcp` / `logs` / `settings`）在
`_is_duplicate` 里额外硬挡一层，服务端保证插件无法占用。

---

## 加载层

### 扫描位置

```
%LOCALAPPDATA%\stories-in-wx\plugins\
```

与 `keystore.bin` / `mcp_config.json` / `media_key.json` 同源，跨安装持久，
PyInstaller 打包后依然可用（`LOCALAPPDATA` 在打包环境里仍可读）。

### 支持形态

| 形态 | 判定 | 页面资源目录 |
|---|---|---|
| 单文件 | `*.py` | `<file>.pages/` |
| 包 | 目录含 `__init__.py` | `<pkg>/pages/` |

跳过：以 `_` / `.` 开头、`__pycache__`、`*.disabled`。

### 模块命名

```python
mod_name = f"siwx_plugin_{name}_{md5(str(path))[:8]}"
```

路径哈希避免与 `sys.modules` 里同名包冲突（插件名可能与已安装的
第三方包重名）。装载前先塞进 `sys.modules` 以支持包内相对导入，
`exec_module` 失败则回滚。

### 元信息注入

装载后往模块写两个私有属性，供契约层使用：

- `__siwx_source__` = `"dir:<abs path>"` —— 报告里的来源，也是
  `pages_hint` 相对路径的解析基准
- `__siwx_pages_dir__` —— loader 自动探测到的页面目录

### 幂等与开关

```python
load_all(reset=False)   # 已加载则直接返回缓存报告
ensure_loaded()         # 供 server / mcp / cli 入口调用
```

`SIWX_NO_PLUGINS=1` → `plugins_enabled()` 为假，`load_all` 返回空报告，
注册表保持空。用于回归测试与排障（隔离用户已装插件的影响）。

### 进阶形态：`register(registry)`

除声明式 `PLUGIN` 字典外，也支持模块提供 `register(reg)` 函数直接调用
注册 API。此时插件自行负责构造数据结构。

---

## 显示条件

**为什么不用谓词函数**：若让插件提供 `lambda: ...` 判断菜单项是否显示，
就等于在**每个 HTTP 请求路径里执行插件代码** —— 既慢（每次请求都要跑），
又凭空多一个任意代码执行面。改为声明式条件 dict，由宿主结构化求值：
成本极低（只读环境/目录），结果可缓存，且能在设置页解释"这页为什么被隐藏"。

```python
ConditionContext.build(plugin_config)   # 从当前运行环境采集
```

原子条件（全部 AND）：

| 键 | 求值依据 |
|---|---|
| `requires_decrypted` | 输出根下存在含 `message/` 的账号目录 |
| `requires_account` | 指定账号名在已解密列表中 |
| `wechat_running` | `discover.find_wechat_pids()` 非空（仅 Windows） |
| `platform` | `platform.system()`，支持 `win`/`win32`/`darwin`/`osx` 别名 |
| `env` | 环境变量存在且非 `0/false/no/空`；或 `{NAME: 期望值}` |
| `min_version` | 与 `siwx.__version__` 比较（数字段解析） |
| `config` | 插件自身配置值等于期望值 |
| `any_of` | 子条件列表，任一满足 |

`evaluate()` 任何意外都保守返回 `False`（宁可页面不显示，也不要抛异常
打断整个菜单接口）。`explain()` 给出人类可读原因，供 `?all=1` 调试。

---

## 配置存储

```
%LOCALAPPDATA%\stories-in-wx\plugin_config\<插件名>.json
```

- **原子写**：临时文件 + `os.replace`（沿用项目既有模式）
- **每插件互斥锁**：避免并发 POST 设置互相覆盖
- **懒创建**：无实际变更时不落盘
- **损坏容错**：读取失败（缺失/JSON 损坏）回退 schema 默认值，不抛异常
- **插件名净化**：只保留 `[a-zA-Z0-9_.-]`，防目录穿越
- **未声明字段拒绝**：`apply_update` 只接受 schema 里声明过的键

类型强制（`_coerce`）：`bool` 接受 `1/true/yes/on` 等字符串；
`int`/`float` 越界返回 `None` → 回退默认值；`choice` 不在 `choices` 内 → 回退默认值。
`str` / `text` / `color` 一律转字符串。

---

## 加载报告

```python
class PluginStatus:
    name, version, source, status, hooks, missing_requires, error
```

| status | 触发条件 |
|---|---|
| `ok` | 正常加载 |
| `degraded` | `requires` 里有探测不到的包；或与已加载插件同名被跳过 |
| `error` | import / 契约 / 注册阶段抛异常（`error` 字段记 `[阶段] 摘要`） |

- **同名先到先得**：`_add` 按名字去重，加载顺序在先者胜
- **脱敏 + 截断**：错误信息过 `logger.desensitize_msg()` 并截到 300 字符
- 供 `GET /api/plugins` 与设置页可视化消费

---

## chat_bridge —— 热路径守卫集中处

把所有"作用于聊天数据"的 hook 收敛到一个模块，便于统一加保护：

| 函数 | 行为 |
|---|---|
| `apply_renderer(msg, ctx)` | 按 `msg["type"]` 取单赢家，采纳 `kind/render/text/extra` |
| `decorate_message(msg, ctx, hot)` | 浅合并补丁，`hot=False` 时只跑 `hot=True` 的装饰器 |
| `decorate_session(session, ctx, hot)` | 同上，作用于会话 |
| `transform_content(text, msg, ctx)` | 按 priority 串联，非字符串结果跳过 |
| `filter_sessions(sessions, ctx)` | 返回 `False` 剔除；异常保守保留 |
| `resolve_avatar(username, account, ctx)` | 返回 `bytes` 采用，`None` 交给内建 |
| `has_message_hooks()` / `has_session_hooks()` | 供调用方提前短路 |

### 超时与熔断

`DecoratorNS.apply` 的实现细节（**踩过的坑**）：

```python
ex = ThreadPoolExecutor(max_workers=1)
patch = ex.submit(...).result(timeout=budget)
ex.shutdown(wait=False)                    # ← 不能 wait=True
```

**不要**写成 `with ThreadPoolExecutor(...) as ex:` —— `__exit__` 会
`shutdown(wait=True)`，把已经超时的调用重新阻塞回来，让 `timeout` 形同虚设。

超时后：登记该线程池为"卡住"，对插件**熔断 60 秒**。
若卡住的线程池超过 `_MAX_STUCK`（8 个），整体暂停装饰器调用，
避免插件把宿主线程数拖爆。熔断到期自动恢复（`_tripped` 会清理过期条目）。

## 节点树安全（前端配合）

插件渲染器返回**结构化节点树**而非 HTML 字符串，
由 `siwx/ui/common.js` 的 `SX.renderNodes` 渲染：

- 标签白名单：`span div b i em strong code pre p br ul ol li small table thead tbody tr th td`
- 属性白名单：`class title style colspan rowspan`
- `on*` 事件属性、`href`/`src` 属性一律拒绝
- `style` 含 `url()` / `expression` / `javascript:` 时清空
- `{t:'raw'}` 显式被忽略
- URL 只允许 `http(s):` / 相对路径 / `#`

服务端侧（`chat_bridge.apply_renderer`）只采纳 `kind/render/text/extra`
四个键，返回非 dict 直接忽略 —— 双端各自独立防御。

---

## 宿主集成点

| 宿主位置 | 接入的 hook |
|---|---|
| `server.py` —— 蓝图挂载、主题注入、任务事件、插件页静态路由 | `api_blueprints` / `themes` / `task_listeners` / `pages` |
| `api_chat.py` —— 会话列表、消息列表、头像 | `session_decorators` / `session_filters` / `message_decorators` / `renderers` / `content_transformers` / `avatar_resolvers` |
| `api_export.py` / `exporter.py` | `export_writers` / `after_export` |
| `mcp_server.py` / `api_mcp.py` | `mcp_tools` |
| `cli.py` | `cli_commands` |
| `strategies/__init__.py` | `key_strategies` |
| `api_plugins.py` | 加载报告 / 页面 / 设置 / 主题的 HTTP 接口 |

每个接入点都写成 `if not registry.<ns>: <原逻辑>` 的短路形式，
保证零插件时零开销。

---

## 测试

`tests/test_plugins.py`（103 个用例）覆盖：

- 契约解析：字符串函数名、页面字段、主题两种形态、蓝图工厂、
  重名去重、`api_version` 校验、坏 hook 不连坐
- 逐 hook 隔离：import 崩溃 / 契约非法 / 注册异常
- 目录发现：单文件 / 包 / 跳过规则 / `pages_hint` / 幂等 / 环境开关
- 显示条件：全部原子条件 + `any_of` + AND 语义 + `explain`
- 配置存储：schema 校验、类型强制、上下界、原子写、路径净化、损坏容错
- 加载报告：三态、同名先到先得、脱敏截断
- `chat_bridge`：渲染器白名单键、装饰器热路径守卫与熔断、转换器串联、
  过滤器保守语义、头像回退
- 导出格式 / MCP 工具的内建优先
- 节点树契约（含 `common.js` 白名单未混入危险标签）
- 零插件向后兼容

`tests/test_regressions.py`（45 个用例）在导入前设 `SIWX_NO_PLUGINS=1`，
并在 `TempRootCase` 里清空注册表 —— 两个模块**顺序无关**，
单独跑或一起跑（`unittest discover`）都全绿。

```bash
python -m unittest discover -s tests -v
```
