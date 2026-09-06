# module-media.py — 媒体解密引擎

> **文件**: `siwx/media.py` | **角色**: 按需解密聊天图片/朋友圈 + 多级图片源 + wxgf 转码

---

## 职责

1. **V0/V1/V2 三代加密格式**按文件头自动分派
2. **账号级密钥派生**（MMKV 离线，无需微信进程）
3. **三级图片源**：attach 原图目录 → Bubble 气泡缓存 → Thumb 明文缩略图
4. **wxgf 转码**：调用微信自带 VoipEngine.dll
5. **内存 LRU 缓存**：200 张，程序关闭释放

---

## 加密三代

| 版本 | 头签名 | 加密方式 | 密钥来源 |
|---|---|---|---|
| V0 | 无签名 | 整文件单字节 XOR | 自动检测（JPEG 首字节 0xFF ⇒ key = data[0] ^ 0xFF） |
| V1 | `\x07\x08V1\x08\x07` | AES-128-ECB + XOR | 固定 key `cfcd208495d565ef` |
| V2 | `\x07\x08V2\x08\x07` | AES-128-ECB（头部）+ XOR（尾部） | 账号级密钥，MMKV 离线派生 |

---

## V2 文件格式

```
偏移   长度   内容
0      6      魔数 \x07\x08V2\x08\x07
6      4      AES 段长度 (LE u32)
10     4      XOR 段长度 (LE u32)
14     1      标志字节
15     N      AES-128-ECB 加密的图像头部
15+N   16     分隔尾（跳过，不硬编码）
15+N+16 M    单字节 XOR 混淆的图像剩余部分
```

**总长校验**: `file_size == 15 + aes_size + 16 + xor_size`

---

## 账号级密钥派生

```python
def candidate_keys(wxid_full: str):
    # 1. 持久缓存优先
    cache = _load_key_cache().get(clean_wxid(wxid_full))
    if cache:
        out.append((bytes.fromhex(cache["aes"]), int(cache["xor"], 16)))
    
    # 2. kvcomm 派生
    for code in find_kvcomm_codes():
        for wx in (clean_wxid(wxid_full), wxid_full):
            aes = MD5(f"{code}{wx}").hexdigest()[:16].encode()
            out.append((aes, code & 0xFF))
```

**code 来源**:
```
C:/Users/*/AppData/Roaming/Tencent/xwechat/net/kvcomm/key_<code>_*.statistic
C:/Users/*/AppData/Roaming/Tencent/xwechat/ilink/kvcomm/key_<code>_*.statistic
C:/Users/*/AppData/Roaming/Tencent/WeChat/*/kvcomm/key_<code>_*.statistic
```

---

## 多级图片源

### get_image() 查找顺序

```
⓪ attach 原图目录直查（msg/attach/<md5(chat)>/**/Img/<md5>*.dat）
   hq=True 时优先 _h 高清版

① hardlink 原图
   md5 → hardlink.db → 存储根 + msg/attach/.../Img/<file_name>

② Bubble 气泡缓存
   cache/<月>/Message/<md5(chat)>/Bubble/<bubble_md5|xml_md5|local_id>*.dat

③ Thumb 明文缩略图
   cache/<月>/Message/<md5(chat)>/Thumb/<local_id>_*.jpg
```

### 质量变体

| 后缀 | 说明 |
|---|---|
| `<md5>.dat` | 聊天显示版 |
| `<md5>_h.dat` | 高清原图 |
| `<md5>_t.dat` | 缩略图 |

---

## 关键函数

### `get_image(account, md5, acc_out_dir, ...) → (bytes, content_type)`

**主入口**: 按需解密一张图。

```
参数:
- account: 账号 wxid
- md5: 消息 XML 中的 md5
- acc_out_dir: 解密产物目录
- chat: 会话 username
- local_id: 消息 local_id
- ts: 时间戳
- bubble_md5: packed_info 内嵌 md5（精确气泡映射）
- hq: 是否优先高清版

返回: (image_bytes, "image/jpeg") 或 (None, error_reason)
```

**LRU 缓存**: 200 张，OrderedDict 实现，程序关闭释放。

---

### `resolve_image_path(acc_out_dir, md5, wxid) → list[Path]`

**hardlink 链路**: md5 → 绝对路径。

```python
# SQL 查询
SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4
WHERE md5=? OR file_name LIKE ?

# 目录解析
SELECT username FROM dir2id WHERE rowid=?

# 存储根
SELECT ValueStdStr FROM db_info WHERE Key='uuid'

# 最终路径
storage_root / wxid / msg / attach / dir1 / dir2 / Img / file_name
```

---

### `bubble_paths(wxid_full, chat, local_id, ts, bubble_md5, xml_md5) → list[Path]`

**气泡缓存查找**。

```
目录名 = md5(会话username)
文件名:
1. packed_info_data 内嵌 md5（精确映射，优先）
2. xml_md5
3. local_id 消息定位

glob: cache/<月>/Message/<md5(chat)>/Bubble/<stem>*.dat
```

---

### `attach_paths(wxid_full, chat, xml_md5) → list[Path]`

**attach 原图目录直查**（不依赖 hardlink）。

```
glob: cache_root / msg / attach / <md5(chat)> / ** / Img / <md5>*.dat

排序: 聊天显示版(.dat) → 高清(_h) → 缩略(_t)
```

---

### `decrypt_v2_body(data, aes_key, xor_key) → (bytes, ctype)`

V2 整文件解密。

```python
aes_size = struct.unpack("<I", data[6:10])[0]
head = AES.new(aes_key, AES.MODE_ECB).decrypt(data[15:15+aes_size])
ext, ctype = _image_sig(head)
tail = data[15+aes_size+16 : 15+aes_size+16+xor_size]
table = bytes(i ^ xor_key for i in range(256))
return head + tail.translate(table), ctype
```

---

### `convert_wxgf(data) → bytes`

**wxgf → JPEG/PNG**（微信官方解码器）。

```python
# 调用 VoipEngine.dll 的 wxam_dec_wxam2pic_5
# DLL 全局单例 + 串行锁（防并发踩崩）
for mode in (0, 3):
    ret = fn(in_buf, in_sz, out_buf, out_sz, cfg)
    if ret == 0 and out_sz > 0:
        return out_buf[:out_sz]
```

**DLL 查找路径**:
```
C:\Program Files\Tencent\Weixin\*\VoipEngine.dll
C:\Program Files\Tencent\WeChat\*\VoipEngine.dll
C:\Program Files (x86)\Tencent\WeChat\*\VoipEngine.dll
```

---

## 缓存机制

### 内存 LRU 缓存

```python
_IMG_CACHE: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_IMG_CACHE_MAX = 200  # 最多 200 张

# 命中 → move_to_end → 返回
# 未命中 → 解密 → 存入 → 超限时 popitem(last=False)
```

**缓存 key**: `f"{account}:{chat}:{local_id}:{md5}:{bubble_md5}:{hq}"`

### 派生密钥持久缓存

```python
# %LOCALAPPDATA%\stories-in-wx\media_key.json
{"wxid_clean": {"aes": "hex", "xor": "0xc9"}}
```

只存密钥，不存明文。

---

## 事件钩子

```python
# serve 模式下由 server 注入 tui.log
# CLI 下默认静默
event = lambda msg: None

# server.py 中:
media.event = tui.log
```

---

## wxgf 处理

微信自研动图格式，用微信官方 DLL 转码：

```python
# 单例 + 串行锁
_VOIP_FN = None
_VOIP_LOCK = threading.Lock()

def _get_voip_fn():
    global _VOIP_FN
    if _VOIP_FN is not None:
        return _VOIP_FN
    # 加载 DLL → 缓存
    ...

def convert_wxgf(data):
    fn = _get_voip_fn()
    with _VOIP_LOCK:
        for mode in (0, 3):
            # 调用 DLL
            ...
```

**为什么要单例 + 锁？**
浏览器并发请求时重复 LoadLibrary 会互相踩崩。

---

## 使用示例

```python
from siwx import media
from pathlib import Path

# 解密一张图
body, ctype = media.get_image(
    account="wxid_xxx",
    md5="abcdef1234567890abcdef1234567890",
    acc_out_dir=Path("output/wxid_xxx"),
    chat="wxid_yyy",
    local_id=123,
    ts=1725600000,
    hq=True  # 优先高清版
)

if body:
    with open("image.jpg", "wb") as f:
        f.write(body)
else:
    print(f"解密失败: {ctype}")  # ctype 这里是错误原因
```
