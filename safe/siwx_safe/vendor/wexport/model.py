"""跨平台统一的数据模型：账号、会话、联系人、消息、附件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Account:
    """一个微信账号及其数据源。"""

    key: str  # 形如 "win4:wxid_xxx" / "win3:wxid_xxx" / "android:<dir>"
    kind: str  # win4 | win3 | android
    display_name: str = ""
    wxid: str = ""
    root: Path | None = None  # 数据根目录
    version: str = ""  # 微信版本号（如 4.1.15.13）
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.wxid or self.key


@dataclass
class Contact:
    username: str
    display_name: str = ""
    alias: str = ""
    remark: str = ""
    nick_name: str = ""
    is_group: bool = False
    members: dict[str, str] = field(default_factory=dict)  # username -> 群内显示名

    def name_for(self, username: str) -> str:
        """群聊里按 username 找群内显示名，缺失时回退。"""
        if username in self.members:
            return self.members[username]
        return self.display_name or username


@dataclass
class Attachment:
    kind: str  # image | voice | video | file | emoji | other
    name: str = ""
    size: int = 0
    duration: int = 0  # 秒
    ext: str = ""
    source: Path | None = None  # 源文件（明文或 .dat 加密文件）
    data: bytes | None = None  # 直接来自 BLOB 的原始数据
    encrypted: str = ""  # "dat-v1" | "dat-v2" | ""
    unavailable: str = ""  # 不可用原因（用于导出时标注）
    extra: dict[str, Any] = field(default_factory=dict)  # md5 / aeskey / cdn 等原始信息


@dataclass
class Message:
    local_id: int
    server_id: int = 0
    timestamp: int = 0  # Unix 秒
    sender: str = ""  # 发送者 username
    sender_name: str = ""
    is_sender: bool = False
    type: int = 0  # 原始消息类型
    subtype: str = ""  # 解析后的类别：text/image/voice/video/file/...
    text: str = ""  # 可读正文（Markdown 已转义由导出层负责）
    raw: str = ""  # 原始 XML/纯文本
    attachments: list[Attachment] = field(default_factory=list)
    quoted: str = ""  # 引用内容的纯文本摘要
    quoted_sender: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    username: str
    display_name: str = ""
    is_group: bool = False
    last_time: int = 0
    message_count: int = 0
    media_root_hint: str = ""  # 安卓: /sdcard 相对路径映射提示
    detail: dict[str, Any] = field(default_factory=dict)
