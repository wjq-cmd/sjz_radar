"""核心业务：批次生成 + 原子核销。

整个系统唯一必须做对的事情就是核销的原子性。见 redeem() 里的说明。
"""

import csv
import hmac
import io
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import STATUS_DISABLED, STATUS_UNUSED, STATUS_USED, Batch, Card, RedeemLog
from .security import decrypt_secret, encrypt_secret, gen_card_no, normalize_card_no

STATUS_TEXT = {STATUS_UNUSED: "未使用", STATUS_USED: "已使用", STATUS_DISABLED: "已作废"}

# 生成卡号时按这个粒度查库去重，避免 10 万张卡发 10 万条 SELECT
_DEDUP_CHUNK = 500


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class RedeemResult:
    success: bool
    reason: str
    product: str = ""
    used_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    product: str = ""
    expires_at: datetime | None = None
    used_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def _gen_batch_no() -> str:
    return f"B{utcnow():%Y%m%d}-{secrets.token_hex(3).upper()}"


def _gen_unique_card_nos(db: Session, count: int) -> list[str]:
    """批量生成卡号，内存 + 数据库双重去重。"""
    result: list[str] = []
    seen: set[str] = set()

    while len(result) < count:
        need = count - len(result)
        # 多生成 5%，抵消极小概率的重复
        pool = [gen_card_no() for _ in range(int(need * 1.05) + 8)]
        fresh = [n for n in pool if n not in seen]
        if not fresh:
            continue

        for i in range(0, len(fresh), _DEDUP_CHUNK):
            chunk = fresh[i : i + _DEDUP_CHUNK]
            existing = set(db.scalars(select(Card.card_no).where(Card.card_no.in_(chunk))))
            for no in chunk:
                if no in existing or no in seen:
                    continue
                seen.add(no)
                result.append(no)
                if len(result) >= count:
                    break
            if len(result) >= count:
                break

    return result[:count]


def create_batch(
    db: Session,
    product: str,
    count: int,
    note: str = "",
    with_pwd: bool = False,
    pwd_length: int = 8,
    duration_days: int = 0,
) -> tuple[Batch, list[dict]]:
    """生成一个批次。

    duration_days > 0 表示时长卡：从核销时刻起算 N 天；0 = 永久。
    返回 (批次, 明文卡密列表)。明文卡密**只有这一次**能拿到，调用方必须自己存下来。
    """
    batch = Batch(
        batch_no=_gen_batch_no(),
        product=product,
        note=note,
        total=count,
        duration_days=duration_days,
    )
    db.add(batch)
    db.flush()  # 拿到 batch.id

    card_nos = _gen_unique_card_nos(db, count)

    db.add_all(
        [
            Card(
                card_no=no,
                card_pwd_enc=encrypt_secret(pwd),
                batch_id=batch.id,
                product=product,
                status=STATUS_UNUSED,
                duration_days=duration_days,
            )
            for no in card_nos
            for pwd in [gen_card_no(pwd_length, group=0) if (with_pwd and pwd_length) else ""]
        ]
    )
    db.commit()

    plain = [
        {
            "card_no": c.card_no,
            "card_pwd": decrypt_secret(c.card_pwd_enc),
            "duration_days": c.duration_days,
        }
        for c in db.scalars(select(Card).where(Card.batch_id == batch.id).order_by(Card.id))
    ]
    return batch, plain


# --------------------------------------------------------------------------- #
# 核销
# --------------------------------------------------------------------------- #


def _log_attempt(
    db: Session, card_no: str, ip: str, device_id: str, success: bool, reason: str
) -> None:
    db.add(
        RedeemLog(
            card_no=card_no,
            ip=ip[:64],
            device_id=device_id[:128],
            success=success,
            reason=reason,
        )
    )
    db.commit()


def redeem(
    db: Session,
    card_no_raw: str,
    card_pwd: str = "",
    ip: str = "",
    device_id: str = "",
) -> RedeemResult:
    """核销一张卡。

    并发安全的关键在第 2 步：不要在应用层写 `if card.status == 0: card.status = 1`，
    两个并发请求会同时通过判断，同一张卡被核销两次。

    正确做法是把状态判断下推到 SQL 的 WHERE 里，让数据库来裁决：

        UPDATE cards SET status=1 WHERE card_no=? AND status=0

    受影响行数为 1 才算抢到，为 0 说明已被别人用掉。InnoDB / SQLite 对这条
    语句都会加行级写锁，天然互斥。
    """
    card_no = normalize_card_no(card_no_raw)

    # 1) 前置校验。这步只读、不上锁，用来给出准确的错误信息、以及校验第二因子。
    #    就算这里读到的是过期状态也没关系 —— 真正的把关在第 2 步。
    card = db.scalar(select(Card).where(Card.card_no == card_no))
    if card is None:
        _log_attempt(db, card_no, ip, device_id, False, "卡密不存在")
        return RedeemResult(False, "卡密不存在")

    if card.card_pwd_enc:
        if not hmac.compare_digest(decrypt_secret(card.card_pwd_enc), card_pwd):
            _log_attempt(db, card_no, ip, device_id, False, "卡密密码错误")
            return RedeemResult(False, "卡密密码错误")

    # 2) 原子核销 —— 唯一的竞态裁决点
    now = utcnow()
    expires_at = now + timedelta(days=card.duration_days) if card.duration_days else None
    result = db.execute(
        update(Card)
        .where(Card.card_no == card_no, Card.status == STATUS_UNUSED)
        .values(
            status=STATUS_USED,
            used_at=now,
            used_by=device_id[:128],
            used_ip=ip[:64],
            expires_at=expires_at,
        )
    )

    if result.rowcount != 1:
        # 抢输了（或本来就是已用/作废）。回滚这次 UPDATE，再查一次真实状态给用户看
        db.rollback()
        current = db.scalar(select(Card.status).where(Card.card_no == card_no))
        reason = {
            STATUS_USED: "卡密已被使用",
            STATUS_DISABLED: "卡密已作废",
            None: "卡密不存在",
        }.get(current, "核销失败")
        _log_attempt(db, card_no, ip, device_id, False, reason)
        return RedeemResult(False, reason)

    _log_attempt(db, card_no, ip, device_id, True, "")
    return RedeemResult(
        True, "核销成功", product=card.product, used_at=now, expires_at=expires_at
    )


# --------------------------------------------------------------------------- #
# 查询 / 运维
# --------------------------------------------------------------------------- #


def get_card(db: Session, card_no_raw: str) -> Card | None:
    return db.scalar(select(Card).where(Card.card_no == normalize_card_no(card_no_raw)))


def log_verify_failure(
    db: Session, card_no_raw: str, ip: str, device_id: str, reason: str
) -> None:
    """只记录校验失败，避免每次正常启动都写一条日志。

    复用 RedeemLog 表，reason 前缀 verify: 便于和核销日志区分。
    """
    _log_attempt(
        db,
        normalize_card_no(card_no_raw),
        ip,
        device_id,
        False,
        ("verify:" + reason)[:64],
    )


def verify_card(db: Session, card_no_raw: str) -> VerifyResult:
    """校验一张已激活的卡是否仍然可用（启动时调用，幂等，不写卡密状态）。

    不绑定设备：device_id 只在核销时记录，这里不做一致性校验。
    """
    card = get_card(db, card_no_raw)
    if card is None:
        return VerifyResult(False, "卡密不存在")
    if card.status == STATUS_DISABLED:
        return VerifyResult(False, "卡密已作废")
    if card.status == STATUS_UNUSED:
        return VerifyResult(False, "卡密尚未激活")
    if card.expires_at is not None and utcnow() >= card.expires_at:
        return VerifyResult(False, "卡密已过期")
    return VerifyResult(
        True,
        "ok",
        product=card.product,
        expires_at=card.expires_at,
        used_at=card.used_at,
    )


def disable_card(db: Session, card_no_raw: str) -> bool:
    """作废一张未使用的卡（防止被误发出去）。"""
    result = db.execute(
        update(Card)
        .where(Card.card_no == normalize_card_no(card_no_raw), Card.status == STATUS_UNUSED)
        .values(status=STATUS_DISABLED)
    )
    db.commit()
    return result.rowcount == 1


def revoke_card(db: Session, card_no_raw: str) -> bool:
    """撤销一张卡（未使用或已使用都可），用于退款/滥用封禁。

    撤销后 /verify 会立即返回「卡密已作废」。
    """
    result = db.execute(
        update(Card)
        .where(
            Card.card_no == normalize_card_no(card_no_raw),
            Card.status != STATUS_DISABLED,
        )
        .values(status=STATUS_DISABLED)
    )
    db.commit()
    return result.rowcount == 1


def batch_stats(db: Session, batch_no: str) -> dict | None:
    batch = db.scalar(select(Batch).where(Batch.batch_no == batch_no))
    if batch is None:
        return None

    rows = db.execute(
        select(Card.status, func.count())
        .where(Card.batch_id == batch.id)
        .group_by(Card.status)
    ).all()
    counts = {status: n for status, n in rows}

    return {
        "batch_no": batch.batch_no,
        "product": batch.product,
        "note": batch.note,
        "total": batch.total,
        "duration_days": batch.duration_days,
        "created_at": batch.created_at,
        "unused": counts.get(STATUS_UNUSED, 0),
        "used": counts.get(STATUS_USED, 0),
        "disabled": counts.get(STATUS_DISABLED, 0),
    }


def redeem_log_summary(db: Session, card_no_raw: str, limit: int = 20) -> list[RedeemLog]:
    card_no = normalize_card_no(card_no_raw)
    return list(
        db.scalars(
            select(RedeemLog)
            .where(RedeemLog.card_no == card_no)
            .order_by(RedeemLog.id.desc())
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------- #
# 管理后台统计
# --------------------------------------------------------------------------- #


def overall_stats(db: Session, now: datetime | None = None) -> dict:
    """概览统计。now 由调用方传入，保证同一响应内的时间口径一致。"""
    now = now or utcnow()
    horizon = now + timedelta(days=7)

    rows = db.execute(select(Card.status, func.count()).group_by(Card.status)).all()
    counts = {status: n for status, n in rows}
    used = counts.get(STATUS_USED, 0)

    expired = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(Card.status == STATUS_USED, Card.expires_at.isnot(None), Card.expires_at <= now)
    ) or 0
    expiring = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            Card.status == STATUS_USED,
            Card.expires_at.isnot(None),
            Card.expires_at > now,
            Card.expires_at <= horizon,
        )
    ) or 0
    batches = db.scalar(select(func.count()).select_from(Batch)) or 0

    verify_failed = db.scalar(
        select(func.count())
        .select_from(RedeemLog)
        .where(RedeemLog.success.is_(False), RedeemLog.reason.like("verify:%"))
    ) or 0
    redeem_failed = db.scalar(
        select(func.count())
        .select_from(RedeemLog)
        .where(RedeemLog.success.is_(False), ~RedeemLog.reason.like("verify:%"))
    ) or 0

    return {
        "cards_total": sum(counts.values()),
        "unused": counts.get(STATUS_UNUSED, 0),
        "used": used,
        "disabled": counts.get(STATUS_DISABLED, 0),
        "expired": expired,
        "expiring_7d": expiring,
        "redeem_failed": redeem_failed,
        "verify_failed": verify_failed,
        "redeem_failed_attempts": redeem_failed + verify_failed,
        "batches": batches,
        "server_time": int(time.time()),
    }


def _empty_product(product: str) -> dict:
    return {
        "product": product,
        "total": 0,
        "unused": 0,
        "used": 0,
        "disabled": 0,
        "expired": 0,
    }


def product_stats(db: Session, now: datetime | None = None) -> list[dict]:
    """产品维度统计，供后台下拉框和概览表使用。"""
    now = now or utcnow()
    products: dict[str, dict] = {}

    rows = db.execute(
        select(Card.product, Card.status, func.count()).group_by(Card.product, Card.status)
    ).all()
    for product, status, count in rows:
        item = products.setdefault(product, _empty_product(product))
        item["total"] += count
        if status == STATUS_UNUSED:
            item["unused"] += count
        elif status == STATUS_USED:
            item["used"] += count
        elif status == STATUS_DISABLED:
            item["disabled"] += count

    expired_rows = db.execute(
        select(Card.product, func.count())
        .where(Card.status == STATUS_USED, Card.expires_at.isnot(None), Card.expires_at <= now)
        .group_by(Card.product)
    ).all()
    for product, count in expired_rows:
        products.setdefault(product, _empty_product(product))["expired"] = count

    return sorted(products.values(), key=lambda item: (-item["total"], item["product"]))


def batch_status_counts(
    db: Session, batch_ids: list[int] | None = None, now: datetime | None = None
) -> dict[int, dict]:
    """批次维度的状态计数；没有卡的批次也要返回全 0，避免前端 N+1。"""
    now = now or utcnow()
    result: dict[int, dict] = {}
    if batch_ids is not None and not batch_ids:
        return result

    stmt = select(Card.batch_id, Card.status, func.count()).group_by(Card.batch_id, Card.status)
    if batch_ids is not None:
        stmt = stmt.where(Card.batch_id.in_(batch_ids))
    for batch_id, status, count in db.execute(stmt).all():
        if batch_id is None:
            continue
        item = result.setdefault(
            batch_id, {"unused": 0, "used": 0, "disabled": 0, "expired": 0}
        )
        if status == STATUS_UNUSED:
            item["unused"] += count
        elif status == STATUS_USED:
            item["used"] += count
        elif status == STATUS_DISABLED:
            item["disabled"] += count

    exp_stmt = (
        select(Card.batch_id, func.count())
        .where(Card.status == STATUS_USED, Card.expires_at.isnot(None), Card.expires_at <= now)
        .group_by(Card.batch_id)
    )
    if batch_ids is not None:
        exp_stmt = exp_stmt.where(Card.batch_id.in_(batch_ids))
    for batch_id, count in db.execute(exp_stmt).all():
        if batch_id is None:
            continue
        result.setdefault(batch_id, {"unused": 0, "used": 0, "disabled": 0, "expired": 0})[
            "expired"
        ] = count
    return result


# --------------------------------------------------------------------------- #
# CSV 导出
# --------------------------------------------------------------------------- #

_CSV_COLUMNS = [
    "card_no",
    "card_pwd",
    "status",
    "status_code",
    "duration_days",
    "expires_at_utc",
    "created_at_utc",
    "used_at_utc",
    "used_by",
    "used_ip",
]


def _fmt_utc(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _csv_cell(value) -> str:
    """防 CSV 公式注入：以 = + - @ 或控制字符开头的值前置制表符。"""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "\t" + text
    return text


def build_batch_csv(
    db: Session, batch_no: str, include_pwd: bool = False
) -> tuple[Batch, bytes] | None:
    """返回 (批次, UTF-8 CSV 字节)。一次性生成，避免流式响应跨线程使用会话。"""
    batch = db.scalar(select(Batch).where(Batch.batch_no == batch_no))
    if batch is None:
        return None

    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 BOM，Excel 打开中文不乱码
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)

    for card in db.scalars(select(Card).where(Card.batch_id == batch.id).order_by(Card.id)):
        writer.writerow(
            [
                _csv_cell(card.card_no),
                _csv_cell(decrypt_secret(card.card_pwd_enc) if include_pwd else ""),
                STATUS_TEXT.get(card.status, "未知"),
                card.status,
                card.duration_days,
                _fmt_utc(card.expires_at),
                _fmt_utc(card.created_at),
                _fmt_utc(card.used_at),
                _csv_cell(card.used_by),
                _csv_cell(card.used_ip),
            ]
        )

    return batch, buffer.getvalue().encode("utf-8")
