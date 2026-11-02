"""数据源抽象：不同平台的微信数据最终都归一到 Conversation / Message。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..model import Account, Attachment, Contact, Conversation, Message


class Source(ABC):
    """一个已打开的微信数据源。"""

    kind: str = "base"

    def __init__(self, account: Account) -> None:
        self.account = account
        self.load_errors: list[str] = []

    @property
    def root(self) -> Path:
        return self.account.root

    @property
    def media_root(self) -> Path | None:
        """图片/视频/文件所在的媒体根目录（子类按平台覆盖）。"""
        return None

    # 生命周期
    def open(self) -> None:  # pragma: no cover - 子类覆盖
        pass

    def close(self) -> None:  # pragma: no cover - 子类覆盖
        pass

    def __enter__(self) -> "Source":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # 能力
    @abstractmethod
    def conversations(self) -> list[Conversation]:
        """列出所有会话（含联系人资料）。"""

    @abstractmethod
    def contacts(self) -> dict[str, Contact]:
        """username -> Contact。"""

    @abstractmethod
    def iter_messages(
        self,
        username: str,
        since: int | None = None,
        until: int | None = None,
    ) -> Iterator[Message]:
        """按时间升序产出某个会话的消息。"""

    def read_attachment(self, attachment: Attachment) -> bytes | None:
        """读取附件原始字节；不可用时返回 None。"""
        return None

    def attachment_path(self, attachment: Attachment) -> Path | None:
        """附件在磁盘上的位置（如果存在）。"""
        return None

    def summarize(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "root": str(self.root),
            "display_name": self.account.display_name,
        }
