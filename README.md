# stories-in-wx (Python)

微信 4.x（Weixin 4.1.x，已在 4.1.13.63 实测）数据库密钥提取与解密工具。
Python 自研重写：架构自研，提取/解密核心逻辑参考原 pc_wechat_exp 的已验证实现。

## 特性

- **全自动**：`python run.py auto` 一条命令完成
  全盘目录扫描（A-Z 盘符 + 用户目录）→ 全局收割密钥 → DPAPI 加密保存 → 数据库解密
- **全局收割**（优于原项目的设计）：微信内存只扫一次，用全部账号 salt 的
  联合集做 HMAC 验证，密钥再分发给各账号
- **只读提取**：WCDB Config.Cipher 两遍扫描（4.1.10+ 免管理员、免重启），
  自研 crib-drag 掩码恢复兜底（微信升级换掩码时可自动求解新掩码）
- **安全**：密钥按 salt 索引、DPAPI 加密落盘（含熵绑定）、日志只输出打码密钥、
  进程句柄只读（VM_READ，无注入）
- **现代化轻量 UI**：Flask + 原生 HTML/CSS/JS，明暗双主题、适度圆角，仅绑定 127.0.0.1
- **插件缝**：`siwx/strategies/STRATEGY_REGISTRY`，向列表追加同签名
  `extract(ctx)` 函数即可接入新策略

## 使用

```bash
pip install -r requirements.txt

python run.py auto                     # 全自动（推荐）
python run.py keys extract [--json]    # 仅提取密钥（全局收割）
python run.py keys list                # 查看密钥库（打码）
python run.py decrypt [--db-dir X] [--out DIR]
python run.py serve [--port 8787]      # Web 控制台
```

## 实测记录（2026-09-06，WeChat 4.1.13.63）

- 全局收割：79 个唯一 salt，一次内存扫描联合验证，**32 个密钥 HMAC 验证通过**
- 当前登录账号 32/32 全覆盖，密钥存入 DPAPI 密钥库
- 解密：32/32 数据库成功（556MB，19.8s），contact.db 读出 3864 个真实联系人

## 架构

```
siwx/
├─ sqlcipher.py            # HMAC 验证原语（全系统咽喉）+ 页级解密
├─ keystore.py             # salt 索引密钥库（DPAPI 加密）
├─ discover.py             # 全盘自动目录发现 + 进程发现（psutil）
├─ winproc.py              # 只读跨进程内存原语（ctypes）
├─ strategies/             # 插件缝：keystore / mmkv / config_cipher / memscan
├─ extract.py              # 编排：全局收割 → 策略链 → 交叉验证 → 解密
├─ server.py               # Flask 控制台（/api/status /api/run /api/job）
└─ ui/                     # 现代化前端（无构建步骤）
```

## 说明

- 未登录账号的密钥不在微信内存中（微信按需懒加载数据库），切换登录后
  重新运行即可提取——这是微信本身的密钥生命周期，原项目同样存在。
- 仅供个人数据备份与研究使用，严禁用于侵犯他人隐私。
