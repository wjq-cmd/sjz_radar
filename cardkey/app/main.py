"""HTTP 接口层。

启动：
    python manage.py init          # 首次：生成 .env + 建表
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

接口文档：http://127.0.0.1:8000/docs
"""

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import service
from .config import settings
from .database import get_db
from .models import STATUS_DISABLED, STATUS_UNUSED, STATUS_USED, Batch, Card, RedeemLog
from .ratelimit import SlidingWindowLimiter
from .schemas import CreateBatchIn, RedeemIn, VerifyIn, fail, ok
from .security import decrypt_secret, mask_card_no, verify_admin

_docs = (
    {}
    if settings.enable_docs
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(
    title="卡密系统", version="0.2.0", description="卡密生成 / 分发 / 核销", **_docs
)

redeem_limiter = SlidingWindowLimiter(
    window=settings.redeem_rate_window, limit=settings.redeem_rate_attempts
)
verify_limiter = SlidingWindowLimiter(
    window=settings.verify_rate_window, limit=settings.verify_rate_attempts
)


WEB_DIR = Path(__file__).resolve().parent / "web"


def _unix_utc(value) -> int | None:
    """朴素 UTC datetime -> Unix 秒。不能用 .timestamp()，它会按本地时区解释。"""
    return int(value.replace(tzinfo=timezone.utc).timestamp()) if value else None


def _now_utc() -> datetime:
    """一次请求内共用同一个时间基准，避免 expired / expiring_7d 边界抖动。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # /docs 需要外部与内联资源，跳过严格 CSP；后台页面本身不使用内联脚本/样式。
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
    return response


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    if not settings.admin_api_key:
        raise HTTPException(500, "服务端未配置 ADMIN_API_KEY，请先运行：python manage.py init")
    if not verify_admin(x_admin_key):
        raise HTTPException(401, "管理员密钥无效")


def client_ip(request: Request) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["公共"])
def health() -> dict:
    return ok({"status": "up"})


# --------------------------------------------------------------------------- #
# 管理后台静态页面
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
def web_index():
    return FileResponse(
        WEB_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------- #
# 核销（给客户端调用）
# --------------------------------------------------------------------------- #


@app.post("/redeem", tags=["公共"])
def redeem(payload: RedeemIn, request: Request, db: Session = Depends(get_db)):
    """核销卡密。

    返回体里 code=0 才是成功；其余都是业务失败，HTTP 状态码仍为 200，
    方便客户端统一处理。限流触发时返回 HTTP 429。
    """
    ip = client_ip(request)

    allowed, retry_after = redeem_limiter.allow(ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content=fail(4290, f"请求过于频繁，请 {retry_after} 秒后再试"),
            headers={"Retry-After": str(retry_after)},
        )

    result = service.redeem(
        db,
        card_no_raw=payload.card_no,
        card_pwd=payload.card_pwd,
        ip=ip,
        device_id=payload.device_id,
    )

    if not result.success:
        return fail(1001, result.reason)

    return ok(
        {
            "product": result.product,
            "used_at": _unix_utc(result.used_at),
            "expires_at": _unix_utc(result.expires_at),
            "server_time": int(time.time()),
        },
        msg="核销成功",
    )


@app.post("/verify", tags=["公共"])
def verify(payload: VerifyIn, request: Request, db: Session = Depends(get_db)):
    """校验卡密是否仍然可用。客户端每次启动调用；幂等，不消耗卡。

    业务失败返回 code=1002（与核销失败的 1001 区分），HTTP 仍为 200；
    限流返回 HTTP 429 + code=4290。
    """
    ip = client_ip(request)

    allowed, retry_after = verify_limiter.allow(ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content=fail(4290, f"请求过于频繁，请 {retry_after} 秒后再试"),
            headers={"Retry-After": str(retry_after)},
        )

    result = service.verify_card(db, payload.card_no)
    if not result.ok:
        service.log_verify_failure(db, payload.card_no, ip, payload.device_id, result.reason)
        return fail(1002, result.reason)

    return ok(
        {
            "product": result.product,
            "expires_at": _unix_utc(result.expires_at),
            "used_at": _unix_utc(result.used_at),
            "server_time": int(time.time()),
        }
    )


# --------------------------------------------------------------------------- #
# 管理端
# --------------------------------------------------------------------------- #


@app.get("/admin/products", tags=["管理"], dependencies=[Depends(require_admin)])
def list_products(db: Session = Depends(get_db)):
    return ok({"items": service.product_stats(db, now=_now_utc())})


@app.post("/admin/batches", tags=["管理"], dependencies=[Depends(require_admin)])
def create_batch(payload: CreateBatchIn, db: Session = Depends(get_db)):
    """生成一个批次。

    ⚠️ 返回的明文卡密只在这里出现这一次。系统不保存明文，请立刻落库/导出。
    """
    batch, cards = service.create_batch(
        db,
        product=payload.product,
        count=payload.count,
        note=payload.note,
        with_pwd=payload.with_pwd,
        pwd_length=payload.pwd_length,
        duration_days=payload.duration_days,
    )
    return ok(
        {
            "batch_no": batch.batch_no,
            "product": batch.product,
            "total": batch.total,
            "duration_days": batch.duration_days,
            "cards": cards,
        },
        msg=f"已生成 {batch.total} 张卡密",
    )


@app.get("/admin/batches", tags=["管理"], dependencies=[Depends(require_admin)])
def list_batches(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(Batch)) or 0
    batches = list(
        db.scalars(
            select(Batch).order_by(Batch.id.desc()).offset((page - 1) * size).limit(size)
        )
    )
    counts = service.batch_status_counts(db, [b.id for b in batches], now=_now_utc())
    items = []
    for batch in batches:
        item = {
            "batch_no": batch.batch_no,
            "product": batch.product,
            "note": batch.note,
            "total": batch.total,
            "duration_days": batch.duration_days,
            "created_at": batch.created_at.isoformat(),
        }
        item.update(counts.get(batch.id, {"unused": 0, "used": 0, "disabled": 0, "expired": 0}))
        items.append(item)
    return ok({"total": total, "page": page, "size": size, "items": items})


@app.get("/admin/batches/{batch_no}/stats", tags=["管理"], dependencies=[Depends(require_admin)])
def batch_stats(batch_no: str, db: Session = Depends(get_db)):
    stats = service.batch_stats(db, batch_no)
    if stats is None:
        raise HTTPException(404, "批次不存在")
    stats["created_at"] = stats["created_at"].isoformat()
    return ok(stats)


@app.get(
    "/admin/batches/{batch_no}/export",
    tags=["管理"],
    dependencies=[Depends(require_admin)],
)
def export_batch(
    batch_no: str,
    include_pwd: bool = False,
    db: Session = Depends(get_db),
):
    """导出整批明文卡密（可选含第二因子密码）。"""
    result = service.build_batch_csv(db, batch_no, include_pwd=include_pwd)
    if result is None:
        raise HTTPException(404, "批次不存在")
    batch, payload = result
    filename = f"{batch.batch_no}.csv"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    }
    return Response(
        content=payload, media_type="text/csv; charset=utf-8", headers=headers
    )


@app.get("/admin/cards", tags=["管理"], dependencies=[Depends(require_admin)])
def list_cards(
    batch_no: str | None = None,
    product: str | None = None,
    status: int | None = Query(None, ge=0, le=2),
    card_no: str | None = Query(None, max_length=64),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """卡号默认打码返回。需要完整卡号走 /admin/cards/{card_no}/reveal。"""
    stmt = select(Card)
    count_stmt = select(func.count()).select_from(Card)

    if batch_no:
        batch = db.scalar(select(Batch).where(Batch.batch_no == batch_no))
        if batch is None:
            raise HTTPException(404, "批次不存在")
        stmt = stmt.where(Card.batch_id == batch.id)
        count_stmt = count_stmt.where(Card.batch_id == batch.id)
    if product:
        stmt = stmt.where(Card.product == product)
        count_stmt = count_stmt.where(Card.product == product)
    if status is not None:
        stmt = stmt.where(Card.status == status)
        count_stmt = count_stmt.where(Card.status == status)
    if card_no and card_no.strip():
        normalized = service.normalize_card_no(card_no)
        if not normalized:
            raise HTTPException(422, "卡号无效")
        stmt = stmt.where(Card.card_no == normalized)
        count_stmt = count_stmt.where(Card.card_no == normalized)

    total = db.scalar(count_stmt) or 0
    cards = list(
        db.scalars(stmt.order_by(Card.id.desc()).offset((page - 1) * size).limit(size))
    )

    now = _now_utc()
    batch_ids = {c.batch_id for c in cards if c.batch_id}
    batch_nos: dict[int, str] = {}
    if batch_ids:
        for batch_id, batch_no_value in db.execute(
            select(Batch.id, Batch.batch_no).where(Batch.id.in_(batch_ids))
        ).all():
            batch_nos[batch_id] = batch_no_value

    items = []
    for card in cards:
        expired = (
            card.status == STATUS_USED
            and card.expires_at is not None
            and card.expires_at <= now
        )
        items.append(
            {
                "id": card.id,
                "card_no": mask_card_no(card.card_no),
                "batch_no": batch_nos.get(card.batch_id, ""),
                "product": card.product,
                "status": card.status,
                "status_text": service.STATUS_TEXT.get(card.status, "未知"),
                "has_pwd": bool(card.card_pwd_enc),
                "duration_days": card.duration_days,
                "expires_at": _unix_utc(card.expires_at),
                "is_expired": expired,
                "created_at": card.created_at.isoformat(),
                "used_at": card.used_at.isoformat() if card.used_at else None,
                "used_by": card.used_by,
                "used_ip": card.used_ip,
            }
        )
    return ok({"total": total, "page": page, "size": size, "items": items})


def _card_by_id(db: Session, card_id: int) -> Card:
    card = db.get(Card, card_id)
    if card is None:
        raise HTTPException(404, "卡密不存在")
    return card


@app.post(
    "/admin/cards/by-id/{card_id}/reveal",
    tags=["管理"],
    dependencies=[Depends(require_admin)],
)
def reveal_card_by_id(card_id: int, db: Session = Depends(get_db)):
    """后台列表里的卡号是打码的，按 id 查明文。"""
    card = _card_by_id(db, card_id)
    return ok(
        {
            "card_no": card.card_no,
            "card_pwd": decrypt_secret(card.card_pwd_enc),
            "product": card.product,
            "status": card.status,
            "status_text": service.STATUS_TEXT.get(card.status, "未知"),
            "duration_days": card.duration_days,
            "expires_at": _unix_utc(card.expires_at),
        }
    )


@app.post(
    "/admin/cards/by-id/{card_id}/disable",
    tags=["管理"],
    dependencies=[Depends(require_admin)],
)
def disable_card_by_id(card_id: int, db: Session = Depends(get_db)):
    card = _card_by_id(db, card_id)
    if not service.disable_card(db, card.card_no):
        raise HTTPException(409, "作废失败：卡密已被使用或已作废")
    return ok(msg="已作废")


@app.post(
    "/admin/cards/by-id/{card_id}/revoke",
    tags=["管理"],
    dependencies=[Depends(require_admin)],
)
def revoke_card_by_id(card_id: int, db: Session = Depends(get_db)):
    card = _card_by_id(db, card_id)
    if not service.revoke_card(db, card.card_no):
        raise HTTPException(409, "撤销失败：卡密不存在或已作废")
    return ok(msg="已撤销")


@app.post("/admin/cards/{card_no}/reveal", tags=["管理"], dependencies=[Depends(require_admin)])
def reveal_card(card_no: str, db: Session = Depends(get_db)):
    """解密返回卡密明文，用于用户丢卡后补发。这个接口要重点审计。"""
    card = service.get_card(db, card_no)
    if card is None:
        raise HTTPException(404, "卡密不存在")
    return ok(
        {
            "card_no": card.card_no,
            "card_pwd": decrypt_secret(card.card_pwd_enc),
            "product": card.product,
            "status": card.status,
            "status_text": service.STATUS_TEXT.get(card.status, "未知"),
            "duration_days": card.duration_days,
            "expires_at": _unix_utc(card.expires_at),
        }
    )


@app.post("/admin/cards/{card_no}/disable", tags=["管理"], dependencies=[Depends(require_admin)])
def disable_card(card_no: str, db: Session = Depends(get_db)):
    if not service.disable_card(db, card_no):
        raise HTTPException(409, "作废失败：卡密不存在，或已被使用/已作废")
    return ok(msg="已作废")


@app.post("/admin/cards/{card_no}/revoke", tags=["管理"], dependencies=[Depends(require_admin)])
def revoke_card(card_no: str, db: Session = Depends(get_db)):
    """撤销已激活的卡（退款/封禁）。撤销后客户端下次 /verify 即失效。"""
    if not service.revoke_card(db, card_no):
        raise HTTPException(409, "撤销失败：卡密不存在或已作废")
    return ok(msg="已撤销")


@app.get("/admin/logs", tags=["管理"], dependencies=[Depends(require_admin)])
def list_logs(
    card_no: str | None = None,
    success: bool | None = None,
    reason_prefix: str | None = Query(None, max_length=16),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(RedeemLog)
    count_stmt = select(func.count()).select_from(RedeemLog)
    if card_no:
        normalized = service.normalize_card_no(card_no)
        stmt = stmt.where(RedeemLog.card_no == normalized)
        count_stmt = count_stmt.where(RedeemLog.card_no == normalized)
    if success is not None:
        stmt = stmt.where(RedeemLog.success == success)
        count_stmt = count_stmt.where(RedeemLog.success == success)
    if reason_prefix:
        escaped = (
            reason_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = escaped + "%"
        stmt = stmt.where(RedeemLog.reason.like(pattern, escape="\\"))
        count_stmt = count_stmt.where(RedeemLog.reason.like(pattern, escape="\\"))

    total = db.scalar(count_stmt) or 0
    logs = list(
        db.scalars(stmt.order_by(RedeemLog.id.desc()).offset((page - 1) * size).limit(size))
    )
    return ok(
        {
            "total": total,
            "page": page,
            "size": size,
            "items": [
                {
                    "card_no": mask_card_no(log.card_no),
                    "ip": log.ip,
                    "device_id": log.device_id,
                    "success": log.success,
                    "reason": log.reason,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }
    )


@app.get("/admin/stats", tags=["管理"], dependencies=[Depends(require_admin)])
def overall_stats(db: Session = Depends(get_db)):
    return ok(service.overall_stats(db, now=_now_utc()))


# 静态资源放在最后挂载，确保 /、/admin/* 等路由先匹配。
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
