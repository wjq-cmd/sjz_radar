"""数据模型。"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# 卡密状态
STATUS_UNUSED = 0
STATUS_USED = 1
STATUS_DISABLED = 2


def utcnow() -> datetime:
    """朴素 UTC 时间（不带 tzinfo），避免 SQLite / MySQL 时区存储不一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Batch(Base):
    """一次生成的批次，方便按批管理和统计。"""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    product: Mapped[str] = mapped_column(String(64), index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    # 时长卡：从激活时刻起算 N 天；0 = 永久
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cards: Mapped[list["Card"]] = relationship(back_populates="batch")


class Card(Base):
    """卡密本体。

    card_no 明文存储 + 唯一索引：查询和防重都必须靠它，无法哈希化。
    card_pwd（可选的第二因子）用 Fernet 加密存储，后台可解密用于补发。
    """

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    card_pwd_enc: Mapped[str] = mapped_column(String(255), default="")
    batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("batches.id"), index=True, nullable=True
    )
    product: Mapped[str] = mapped_column(String(64), index=True, default="")
    status: Mapped[int] = mapped_column(Integer, default=STATUS_UNUSED, index=True)

    # 时长卡：duration_days 在核销时写入 expires_at（0 = 永久，expires_at 为空）
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    used_by: Mapped[str] = mapped_column(String(128), default="")  # 绑定的设备/账号
    used_ip: Mapped[str] = mapped_column(String(64), default="")

    batch: Mapped[Optional["Batch"]] = relationship(back_populates="cards")

    __table_args__ = (Index("ix_cards_product_status", "product", "status"),)


class RedeemLog(Base):
    """每一次核销尝试都落库，失败也要记 —— 风控和排查全靠它。"""

    __tablename__ = "redeem_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_no: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True, default="")
    device_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
