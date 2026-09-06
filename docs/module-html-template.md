# module-html-template.py — HTML 导出模板

> **文件**: `siwx/html_template.py` | **角色**: 数据驱动交互式聊天查看器

---

## 职责

1. **数据转换**: 内部消息格式 → CHAT_DATA JSON
2. **HTML 生成**: 自包含 HTML（CSS + JS 内联）
3. **JS 渲染器**: 纯前端渲染，支持所有消息类型 + 交互功能

---

## 关键函数

### `build_chat_data(session, msgs, avatar_map) → dict`

**内部格式 → CHAT_DATA 嵌入格式**。

```python
{
  "meta": {
    "sessionId": "wxid_xxx",
    "sessionName": "张三",
    "isGroup": false,
    "exportTime": 1725600000000,
    "messageCount": 1234,
    "dateRange": {"start": 1720000000, "end": 1725600000},
    "ownerId": "wxid_xxx",
  },
  "members": [{"id": "wxid_yyy", "name": "李四", "avatar": "avatars/xxx.jpg"}],
  "avatarFiles": {"wxid_yyy": "avatars/xxx.jpg"},
  "messages": [{
    "timestamp": 1725600000,
    "sender": "wxid_yyy",
    "senderName": "李四",
    "type": 1,
    "content": "你好",
    "rawContent": "你好",
    "isSend": 0,
    "mediaPath": "media/0001_xxx.jpg",   # 图片消息
    "quote": {"sender": "...", "content": "..."},  # 引用消息
    "link": {"title": "...", "url": "..."},         # 链接消息
  }],
}
```

---

### `render_html(chat_data: dict) → str`

**生成自包含 HTML**。

```python
def render_html(chat_data):
    data_json = json.dumps(chat_data, ensure_ascii=False)
    return _HTML_HEAD + f"<script>window.CHAT_DATA = {data_json};</script>" + _HTML_RENDERER
```

---

## JS 渲染器功能

### 消息类型渲染

| 类型 | 渲染方式 |
|---|---|
| 文本 (1) | 纯文本 |
| 图片 (3) | `<img>` + 灯箱 |
| 语音 (34) | `<audio>` 播放器 |
| 视频 (43) | `<video>` 播放器 |
| 名片 (42) | 卡片样式 |
| 动画表情 (47) | `<img>` + 灯箱 |
| 位置 (48) | 卡片样式 |
| 链接 (49) | 卡片样式 + 可点击 |
| 引用 (57) | 引用块 + 回复内容 |
| 转账 | 橙色卡片 |
| 红包 | 橙色卡片 |
| 系统 (10000/100002) | 居中灰色文字 |

### 交互功能

- **统计面板**: 消息数 / 成员数 / 天数 / 类型分布
- **类型筛选**: 点击标签筛选消息类型
- **日期范围**: 起止日期筛选
- **搜索**: 内容或发送者搜索
- **明暗主题**: 点击切换
- **灯箱**: 点击图片放大
- **无限滚动**: 滚动到底部自动加载（每批 50 条）

---

## 使用示例

```python
from siwx.html_template import build_chat_data, render_html

chat_data = build_chat_data(session, msgs, avatar_map)
html = render_html(chat_data)
with open("chat.html", "w", encoding="utf-8") as f:
    f.write(html)
```

---

## 设计决策

### 为什么数据驱动？

- HTML 模板固定，数据通过 JSON 嵌入
- 同一模板可渲染任意会话
- 前端纯渲染，无需后端

### 为什么自包含？

- CSS + JS 全部内联
- 单个 HTML 文件即可查看
- 便于分享和存档
