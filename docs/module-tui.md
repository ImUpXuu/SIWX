# module-tui.py — rich 驱动的终端 UI

> **文件**: `siwx/tui.py` | **角色**: 可扩展的终端 UI 组件层

---

## 职责

1. **彩色日志**: `[tag]` 前缀着色 + 语义着色
2. **表格组件**: salt 状态表、账号总览表
3. **状态栏**: serve 模式常驻状态栏
4. **可扩展**: TAG_STYLES 注册表，新策略加一行即可

---

## 主题

```python
console = Console(theme=Theme({
    "tag.cipher": "bold cyan",
    "tag.mmkv": "bold magenta",
    "tag.memscan": "bold blue",
    "tag.keystore": "bold yellow",
    "tag.交叉验证": "bold green",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "dim": "dim",
    "accent": "bold #2f6fdb",
}))
```

---

## 关键函数

### `log(msg: str) → None`

**分层彩色日志**。

```python
# [tag] 前缀 → 着色
log("[cipher] 扫描完成")    # cyan
log("[mmkv] 解密成功")      # magenta

# 语义着色
log("✔ 验证通过")           # green
log("✗ 失败")               # red
log("缓存命中")             # yellow
```

**TAG_RE**: `^\[([a-zA-Z\u4e00-\u9fff]+)\]\s*(.*)$`

---

### `banner() → None`

打印 ASCII banner。

```
╭──────────────────────────────────────────╮
│   ✦ stories-in-wx   微信密钥提取 · 解密  ✦   │
╰──────────────────────────────────────────╯
```

---

### `salt_table(report: dict) → None`

**salt 状态表**。

```
 ✓  salt=abcdef1234567890…   3  cipher      abcdef…7890
 ✗  salt=fedcba9876543210…   1  -           -
```

---

### `summary_line(verified, total, ms) → None`

**密钥摘要行**。

```
▸ 密钥 32/32 已验证 (1234 ms)    # green
▸ 密钥 5/79 已验证 (5678 ms)     # yellow
```

---

### `decrypt_summary(ok, failed, skipped, cached, ms, out) → None`

**解密摘要行**。

```
▸ 解密完成 31 成功（缓存命中 1） (5678 ms) → output/wxid_xxx
▸ 解密完成 30 成功（缓存命中 5） 2 失败 1 缺密钥 (9012 ms) → output/wxid_xxx
```

---

### `make_status_bar(getter) → Text`

**底部常驻状态栏**。

```
 ● http://127.0.0.1:8787  │  微信: 运行中(2)  │  wxid: wxid_xxx  │  密钥: 32  │  任务: 空闲
```

**getter 返回**: `{url, wechat, wxid, keys, job}`

---

### `run_live_status(getter, on_start) → None`

**常驻状态栏循环**。

```python
def run_live_status(getter, on_start):
    # Windows: 启用 ANSI 转义码
    # 1 秒刷新
    # Ctrl+C 退出
```

---

## 扩展方式

### 添加新策略的日志配色

```python
# 在 Theme 中添加
"tag.my_strategy": "bold red",

# 在 tag_style() 中添加
if t == "my_strategy":
    return "tag.my_strategy"
```

### 添加新组件

```python
def my_component(data):
    """新组件"""
    t = Table(...)
    console.print(t)
```

---

## 使用示例

```python
from siwx import tui

tui.banner()
tui.step("密钥提取")
tui.log("[cipher] 扫描完成")
tui.summary_line(32, 32, 1234)
tui.decrypt_summary(31, 0, 0, 1, 5678, "output/wxid_xxx")
```
