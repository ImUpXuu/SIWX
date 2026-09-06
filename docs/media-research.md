# 媒体解密研究笔记（2026-09-06，WeChat 4.1.13.63 实测）

## 结论（一句话）

**V2 媒体（朋友圈 + 聊天图片）用一把"账号级密钥"即可全量按需解密，
密钥可从本机 MMKV 离线派生，不需要动态内存提取，不需要全量解密。**

## 实测数据

| 验证项 | 结果 |
|---|---|
| 朋友圈图片缓存位置 | `xwechat_files/<wxid>/cache/<YYYY-MM>/Sns/Img/<xx>/<md5>` |
| 朋友圈图片加密格式 | 184/184 全部 V2（`\x07\x08V2\x08\x07`） |
| 聊天图片格式 | `msg/attach/**/Img/*.dat` 367/400 采样为 V2，其余为 V0/其它 |
| MMKV code 位置 | `AppData/Roaming/Tencent/xwechat/net/kvcomm/key_<code>_<...>.statistic`（另有 ilink/kvcomm、Tencent/WeChat/*/kvcomm） |
| 账号级 AES key | `MD5(str(code) + 清洗后wxid).hexdigest()[:16]` 的 16 个 ASCII 字节（AES-128-ECB） |
| 账号级 XOR key | `code & 0xFF`（0xC9 兜底） |
| 朋友圈解密 | **40/40 成功**（离线派生，未运行微信内存扫描） |
| 聊天图片解密 | **15/15 成功**（同一把 key） |
| 完整性 | FFD8 头 / FFD9 尾，contact 之外又添实证 |

实测 code = 123456789，key = MD5("123456789" + "wxid_demo")[:16] = e89c7031xxxxxxxx…

## V2 文件格式（与原项目记录一致，4.1.13.63 验证）

```
0   6   魔数 \x07\x08V2\x08\x07
6   4   AES 段长度 (LE u32)   实测 0x400 = 1024
10  4   XOR 段长度 (LE u32)
14  1   标志字节（实测 0x01）
15  N   AES-128-ECB 加密头部图像数据
15+N 16 常量尾（跳过）
之后 M   XOR(单字节) 加密的图像剩余数据
```

解密 = AES-ECB 解头部 + 单字节 XOR 解尾部，两段拼接即为完整 JPEG/PNG。

## 原项目"动态提取"的定位

pc_wechat_exp 的 harvest-keys（V2 魔数邻近窗口扫描 + 32hex 正则）是在**没有账号 key 时**
的兜底：浏览图片瞬间密钥在微信内存，边看边抓。实测发现 MMKV 离线派生覆盖同一需求
且更稳（不依赖浏览时机），因此动态提取降级为兜底策略保留（代码骨架已在
`v2_key_extract` 研究脚本中验证可行性：本轮魔数扫描 + 3.2 万 hex 候选测试跑通，仅当时
密钥不在内存）。

## 待解之谜（后续研究）

- `msg/attach` 中 32 个 `95b2` 开头的未知签名文件（疑似视频/缩略图变体）。
- MMKV code 的有效期：换号/重装后 code 是否变化（预期：每个账号一个 code，稳定）。
- 视频文件（msg/video）的加密方式（大概率同 V2，待采样）。

## 媒体解密模块设计（待实施，按需解密不全量）

```
siwx/media.py
├─ account_media_key()      # kvcomm 派生账号级 (aes_key, xor_key)，结果进 keystore 缓存
├─ decrypt_media(path)      # 按头分派：V2(账号key) / V1(固定key cfcd208495d565ef) / V0(XOR自动检测)
└─ 解密产物缓存             # output/<wxid>/media_decrypted/<md5>.<ext> + manifest，命中秒回

Web：
├─ GET /api/media?account=&md5=     # 按需解密单张，返回 image/jpeg（聊天查看器图片点击时调用）
└─ 兜底链：账号key → 内存动态收割（引导用户浏览对应聊天）→ 失败占位图
```

**不做全量解密**：184 张朋友圈 + 367 张聊天样本合计仅 ~60MB，但聊天历史可达数 GB——
按需 + 缓存才是正确姿势。
