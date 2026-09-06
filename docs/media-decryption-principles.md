# 微信 4.x 媒体解密技术原理


> ⚠️ 所有数据均为脱敏示例。
> 版本：1.0 | 2026-09-06 | 实测环境：WeChat (Weixin.exe) 4.1.13.63 / Windows 11
> 本文是 stories-in-wx 媒体解密模块的权威技术依据，所有"实测"结论均在本机验证通过。

---

## 1. 概述

微信 4.x 将聊天图片、朋友圈图片等媒体以**加密文件**形式落盘，扩展名 `.dat`
或无扩展名（按内容 md5 命名）。加密分三代（V0/V1/V2），4.1.x 主流为 **V2：
"AES-ECB 加密头部 + 单字节 XOR 混淆尾部" 的混合方案**。

V2 的密钥体系是**账号级**（不是逐文件）：

- AES-128 密钥 = 从本机 MMKV 文件派生（离线可得，无需微信进程在线）；
- XOR 字节 = 由同一 code 推导。

因此**解密媒体不需要动态提取微信内存**——原项目 pc_wechat_exp 的
"harvest-keys 动态抓取"只是没有账号密钥时的兜底手段。本文实测：
朋友圈图片 40/40、聊天图片 15/15 用同一把离线派生的账号级密钥解密成功。

## 2. 媒体落盘布局（实测）

| 媒体类型 | 路径 | 命名 |
|---|---|---|
| 聊天图片 | `xwechat_files/<wxid>/msg/attach/<目录1>/<目录2>/Img/*.dat` | 内容 md5（可带 `_h`/`_t` 缩略图后缀） |
| 朋友圈图片（浏览缓存） | `xwechat_files/<wxid>/cache/<YYYY-MM>/Sns/Img/<2位hex>/<md5>` | 内容 md5，无扩展名 |
| 朋友圈背景/自己发布 | `xwechat_files/<wxid>/business/sns/{bkg,publish}/...` | md5 |
| 聊天视频 | `xwechat_files/<wxid>/msg/video/...`（加密方式待采样） | — |

路径根目录可能在 `C:\Users\<user>\xwechat_files`、`D:\xwechat_files` 或
`C:\Users\<user>\Documents\xwechat_files`（多实例并存，需全盘发现）。

## 3. 加密三代（V0 / V1 / V2）

按文件头 6 字节区分：

| 版本 | 头签名 | 加密方式 | 密钥来源 |
|---|---|---|---|
| V0 | 无签名 | 整文件单字节 XOR | 自动检测（明文 JPEG 首字节 0xFF ⇒ key = 首字节 ^ 0xFF） |
| V1 | `07 08 56 31 08 07`（`\x07\x08V1\x08\x07`） | AES-128-ECB + XOR | 固定 key `cfcd208495d565ef`（来自原项目记录，本环境未采到 V1 样本复验） |
| V2 | `07 08 56 32 08 07`（`\x07\x08V2\x08\x07`） | AES-128-ECB（头部）+ 单字节 XOR（尾部） | **账号级密钥，MMKV 离线派生**（见 §5） |

实测采样（4.1.13.63）：朋友圈 184/184 为 V2；聊天 `msg/attach` 采样 400 个
中 367 个 V2，其余为 V0 或未知（见 §9）。

## 4. V2 文件格式（逐字节，实测校验）

```
偏移   长度   内容
0      6      魔数 \x07\x08V2\x08\x07
6      4      AES 段长度（LE u32）          实测 0x400 = 1024
10     4      XOR 段长度（LE u32）
14     1      标志字节                      实测恒为 0x01
15     N      AES-128-ECB 加密的图像头部    N = AES 段长度
15+N   16     分隔尾（见下方注意）
15+N+16 M     单字节 XOR 混淆的图像剩余部分  M = XOR 段长度
```

**总长校验公式（实测 3/3 精确成立）：**
`file_size == 15 + aes_size + 16 + xor_size`

示例（43008B JPEG）：
`43039 == 15 + 1024 + 16 + 41984` ✓

> ⚠️ **关于 15+N 处的 16 字节**：原项目记录为全局常量
> `56fbf486095aa2eed54a41405dffe35d`。**本机（4.1.13.63）实测为
> `a2b382a8394dfe19eb926c16b3719f7e`** —— 它不是解密输入，跳过即可，
> 但说明该值随版本/设备变化，解析时**绝不能硬编码这个值**，只能按长度跳过。

**解密算法：**

```python
pt = AES.new(key16, AES.MODE_ECB).decrypt(data[15:15+aes_size]) \
   + bytes(b ^ xor_key for b in data[15+aes_size+16 : 15+aes_size+16+xor_size])
# pt 即完整图像字节流（JPEG/PNG/GIF/WebP/wxgf）
```

> ⚠️ **本文所有 wxid / code / 密钥 / md5 均为脱敏占位符，非真实数据。**

## 5. 密钥体系：账号级密钥的离线派生（核心）

### 5.1 code 的存放位置（实测）

MMKV 统计文件（key/value 混合二进制），文件名携带 code：

```
C:\Users\<user>\AppData\Roaming\Tencent\xwechat\net\kvcomm\key_<code>_<...>.statistic
C:\Users\<user>\AppData\Roaming\Tencent\xwechat\ilink\kvcomm\key_<code>_<...>.statistic
C:\Users\<user>\AppData\Roaming\Tencent\WeChat\<n>\kvcomm\key_<code>_<...>.statistic   (旧版 3.x 遗留)
```

文件名样例：`key_123456789_4065598783_1_1788664282_29215_3600_ready.statistic`
→ code = **123456789**（第一个下划线字段；正则 `key_(\d+)_`）。

### 5.2 派生算法（py_wx_key / H3CoF6 算法，实测验证）

```python
def clean_wxid(wxid: str) -> str:
    """去掉账号后缀：wxid_demo_1234 -> wxid_demo"""
    parts = wxid.split('_')
    return '_'.join(parts[:2]) if wxid.startswith('wxid_') and len(parts) >= 3 else wxid

code  = 123456789                       # 来自 kvcomm 文件名
wxid  = clean_wxid('wxid_demo_1234')

aes_key = MD5(f"{code}{wxid}").hexdigest()[:16].encode()   # 16 个 ASCII 字节 = AES-128 密钥
xor_key = code & 0xFF                                      # 单字节 XOR（0xC9 为常见兜底值）
# 实测: aes_key = b'e89c7031xxxxxxxx', xor_key = 0x2a
```

要点：
- AES key 是 **hex 字符串的前 16 个 ASCII 字符**直接作为 16 字节密钥，
  **不是** hex 解码后的 8 字节；
- wxid 必须清洗（去掉 `_数字` 后缀），否则派生错误；
- 候选变体（wxid+code 顺序、sha256 截断等）可一并尝试以增强兼容，
  但主形态即上式。

### 5.3 验证方法

AES-ECB 解密任一 V2 文件的 15:31（首 16 字节密文），结果必须命中已知图像魔数：

```
JPEG: FF D8 FF | PNG: 89 50 4E 47 | GIF: 47 49 46 38
WebP: 52 49 46 46 … 57 45 42 50 | 微信自研 wxgf: 77 78 67 66
```

整文件解密后进一步校验 `FFD8 … FFD9`（JPEG 头尾）。

### 5.4 实测数据（2026-09-06）

| 实验 | 结果 |
|---|---|
| 朋友圈图片（cache/2026-09/Sns/Img） | **40/40 解密成功** |
| 聊天图片（msg/attach V2 .dat） | **15/15 解密成功**（同一把 key） |
| JPEG 完整性 | FFD8 头 / FFD9 尾 ✓ |

## 6. 密钥获取策略链（模块设计依据）

```
1. 密钥库缓存        # keyst 索引，秒回
2. MMKV 离线派生     # §5，无需微信进程 —— 主力方案
3. 动态内存收割      # 兜底：浏览图片瞬间密钥在微信内存
   3a. V2 魔数邻近扫描：内存中找 \x07\x08V2\x08\x07，±256B 窗口滑 16B 逐个试
   3b. 全内存 32hex 正则：hex 解码后作候选 key
   （每候选用 1 个密文样本试解，命中魔数再对全样本确认）
4. 引导用户          # 提示"在微信中打开对应图片后重试"
```

实测教训：**动态收割强依赖浏览时机**——图片关闭后密钥即从内存消失
（一轮实验中 764MB 内存仅剩 1 处 V2 魔数、3.2 万 hex 候选无一命中）。
这就是离线派生为主、动态收割为辅的原因。

## 7. 已知边界与待研究

| 项 | 状态 |
|---|---|
| ~~Bubble 缓存文件名映射~~ | **已破解（2026-09-06）**：消息行 `packed_info_data` protobuf 内嵌 32hex ASCII = Bubble 缓存文件名（如 `cac6158axx…_b.dat`），实测与磁盘文件精确对应。文件名非 local_id/时间戳命名。 |
| 三个质量变体 | `msg/attach/<md5(会话)>/<年-月>/Img/` 下同名三档：`<md5>.dat`（聊天显示版）、`<md5>_h.dat`（**高清原图**）、`<md5>_t.dat`（缩略图），同一把账号 key 全部可解。会话目录名 = `md5(会话username)`，与 cache/Message 同规则。 |
| 原图从未下载的消息 | 微信只自动下载气泡/缩略质量（Bubble 230×500 级别）；原图要点开才从 CDN 拉取。本地不存在的无法凭空解密。 |
| `msg/attach` 中 32 个 `95b2` 开头签名的 .dat | 未识别（疑似视频/缩略图变体），待研究 |
| 视频（msg/video）加密方式 | 待采样（大概率同 V2，未验证） |
| MMKV code 的稳定性 | 单账号预期稳定；换号/重装是否变化待观察（多账号各有一个 code） |
| wxgf 格式 | 微信自研动图，用微信自带 `VoipEngine.dll` 的 `wxam_dec_wxam2pic_5`（mode 0/3）转 JPEG——DLL 就在微信安装目录，ctypes 直接调 |

## 7.5 聊天图片完整解析链（实现版）

```
消息(含 md5 / bubble_md5[packed_info] / local_id / ts / chat)
1. attach 直查:  msg/attach/<md5(chat)>/**/Img/<xml_md5>*.dat     （hq=1 优先 _h 高清版）
2. hardlink:     image_hardlink_info_v4 + dir2id + db_info 存储根
3. Bubble:       cache/<月>/Message/<md5(chat)>/Bubble/<bubble_md5|xml_md5|local_id>*.dat
4. Thumb:        cache/<月>/Message/<md5(chat)>/Thumb/<local_id>_*.jpg   （明文 JPEG）
全部解密统一走账号级 key，wxgf 统一经 VoipEngine 转码。
缓存为内存 LRU（200 张），程序关闭即释放，明文不落盘。
```

## 8. 落地模块设计（与实现一致的原则）

```
siwx/media.py
├─ account_media_key()   # 读 kvcomm → 派生 (aes_key, xor_key) → 存密钥库缓存
├─ decrypt_media(path)   # 读 6 字节头分派 V0/V1/V2；V2 按 §4 算法；产出图像字节流
└─ 产物缓存              # output/<wxid>/media_decrypted/<md5>.<ext> + manifest（mtime/size）

Web API
├─ GET /api/media?account=&md5=   # 按需解密单张 → image/jpeg（聊天查看器点击图片时调用）
└─ 失败兜底：账号key失败 → 动态收割 → 占位图 + 引导提示
```

**设计红线：按需解密 + 缓存，永远不做全量解密。**
（实测样本：朋友圈 184 张约 60MB；但聊天历史媒体可达数 GB。）

## 9. 参考与来源

- 原项目 `pc_wechat_exp`：V2 布局、V1 固定 key、动态收割三策略、wxgf 处理
- py_wx_key (H3CoF6)：MMKV 派生算法
- ZedeX/weixin-decrypte-script：32hex 内存正则扫描思路
- 本项目实测：`diag_media.py`（研究脚本）、`media_research/`（解密样张）、
  `docs/media-research.md`（研究记录）
