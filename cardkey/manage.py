#!/usr/bin/env python
"""卡密系统命令行工具。

    python manage.py init                     # 生成 .env + 建表
    python manage.py migrate                  # 已有库升级（时长卡字段）
    python manage.py gen -p vip -n 100        # 生成 100 张，控制台打印
    python manage.py gen -p tq -n 100 --days 30       # 生成 100 张 30 天时长卡
    python manage.py gen -p vip -n 10000 -o vip.csv   # 大批量导出 CSV
    python manage.py stats                    # 总览
    python manage.py batch B20260928-A1B2C3   # 单批次统计
    python manage.py disable XXXX-XXXX-...    # 作废
    python manage.py export -b B2026... -o out.csv    # 导出批次（含明文）
"""

import argparse
import csv
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def ensure_env(force: bool = False) -> bool:
    """首次运行时生成 .env。

    必须在 import app.* 之前调用 —— app.config 在导入时就会读取 .env。
    """
    if ENV_PATH.exists() and not force:
        return False

    from cryptography.fernet import Fernet

    admin_key = secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode()

    ENV_PATH.write_text(
        "# 由 manage.py init 自动生成。请勿提交到 git，请勿泄露。\n"
        f"ADMIN_API_KEY={admin_key}\n"
        f"CARD_FERNET_KEY={fernet_key}\n"
        "TRUST_PROXY=false\n"
        "\n"
        "# 上生产换成 MySQL / PostgreSQL（记得先 pip install pymysql）：\n"
        "# DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/cardkey?charset=utf8mb4\n",
        encoding="utf-8",
    )
    print(f"[+] 已生成 {ENV_PATH}")
    print(f"[+] 管理密钥 ADMIN_API_KEY = {admin_key}")
    print("    请把它保存到安全的地方，之后只显示这一次。")
    return True


def cmd_init(args: argparse.Namespace) -> None:
    ensure_env(force=args.force)

    from app import models  # noqa: F401  导入以注册模型
    from app.config import settings
    from app.database import Base, engine

    Base.metadata.create_all(engine)
    print("[+] 数据表已就绪")
    print(f"    数据库: {settings.database_url}")
    cmd_migrate(args)


def cmd_migrate(args: argparse.Namespace | None = None) -> None:
    """给已有数据库补列。幂等，可重复执行。"""
    from sqlalchemy import inspect, text

    from app.database import engine

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    stmts: list[str] = []

    if "batches" in tables:
        cols = {c["name"] for c in insp.get_columns("batches")}
        if "duration_days" not in cols:
            stmts.append(
                "ALTER TABLE batches ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0"
            )
    if "cards" in tables:
        cols = {c["name"] for c in insp.get_columns("cards")}
        if "duration_days" not in cols:
            stmts.append(
                "ALTER TABLE cards ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0"
            )
        if "expires_at" not in cols:
            stmts.append("ALTER TABLE cards ADD COLUMN expires_at DATETIME")

    if not stmts:
        print("[+] 数据表已是最新，无需迁移")
        return

    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    print(f"[+] 已执行 {len(stmts)} 条迁移")
    for stmt in stmts:
        print("    " + stmt)


def cmd_gen(args: argparse.Namespace) -> None:
    from app import service
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        batch, cards = service.create_batch(
            db,
            product=args.product,
            count=args.count,
            note=args.note or "",
            with_pwd=args.with_pwd,
            pwd_length=args.pwd_length,
            duration_days=args.days,
        )
    finally:
        db.close()

    duration = f"{batch.duration_days} 天" if batch.duration_days else "永久"
    print(
        f"[+] 批次 {batch.batch_no}  产品 {batch.product}  共 {batch.total} 张  有效期 {duration}"
    )

    if args.out:
        _write_csv(Path(args.out), cards, with_pwd=args.with_pwd)
        print(f"[+] 已导出到 {args.out}")
    else:
        for c in cards:
            line = c["card_no"]
            if c["card_pwd"]:
                line += f"  {c['card_pwd']}"
            print(line)


def cmd_export(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Batch, Card
    from app.security import decrypt_secret

    db = SessionLocal()
    try:
        batch = db.scalar(select(Batch).where(Batch.batch_no == args.batch_no))
        if batch is None:
            sys.exit(f"[!] 批次不存在: {args.batch_no}")
        cards = list(db.scalars(select(Card).where(Card.batch_id == batch.id).order_by(Card.id)))
        rows = [
            {
                "card_no": c.card_no,
                "card_pwd": decrypt_secret(c.card_pwd_enc),
                "status": service_status_text(c.status),
                "used_at": c.used_at.isoformat() if c.used_at else "",
                "used_by": c.used_by,
            }
            for c in cards
        ]
    finally:
        db.close()

    out = Path(args.out or f"{args.batch_no}.csv")
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["card_no", "card_pwd", "status", "used_at", "used_by"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"[+] 已导出 {len(rows)} 行到 {out}")


def service_status_text(status: int) -> str:
    from app.service import STATUS_TEXT

    return STATUS_TEXT.get(status, "未知")


def cmd_stats(args: argparse.Namespace) -> None:
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import STATUS_DISABLED, STATUS_UNUSED, STATUS_USED, Card, RedeemLog

    db = SessionLocal()
    try:
        rows = db.execute(select(Card.status, func.count()).group_by(Card.status)).all()
        counts = {s: n for s, n in rows}
        failed = db.scalar(
            select(func.count()).select_from(RedeemLog).where(RedeemLog.success.is_(False))
        )
    finally:
        db.close()

    print(f"卡密总数   : {sum(counts.values())}")
    print(f"  未使用   : {counts.get(STATUS_UNUSED, 0)}")
    print(f"  已使用   : {counts.get(STATUS_USED, 0)}")
    print(f"  已作废   : {counts.get(STATUS_DISABLED, 0)}")
    print(f"核销失败次数: {failed or 0}")


def cmd_batch(args: argparse.Namespace) -> None:
    from app import service
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        stats = service.batch_stats(db, args.batch_no)
        logs = service.redeem_log_summary(db, args.card_no) if args.card_no else []
    finally:
        db.close()

    if stats is None:
        sys.exit(f"[!] 批次不存在: {args.batch_no}")

    print(f"批次       : {stats['batch_no']}")
    print(f"产品       : {stats['product']}")
    print(f"备注       : {stats['note']}")
    print(f"创建时间   : {stats['created_at']}")
    print(f"总数       : {stats['total']}")
    print(f"  未使用   : {stats['unused']}")
    print(f"  已使用   : {stats['used']}")
    print(f"  已作废   : {stats['disabled']}")

    if logs:
        print("\n最近核销记录:")
        for log in logs:
            flag = "OK " if log.success else "FAIL"
            print(f"  [{flag}] {log.created_at}  {log.ip}  {log.device_id}  {log.reason}")


def cmd_disable(args: argparse.Namespace) -> None:
    from app import service
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if service.disable_card(db, args.card_no):
            print("[+] 已作废")
        else:
            sys.exit("[!] 作废失败：卡密不存在，或已被使用/已作废")
    finally:
        db.close()


def cmd_revoke(args: argparse.Namespace) -> None:
    from app import service
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if service.revoke_card(db, args.card_no):
            print("[+] 已撤销（已激活的卡也会立即失效）")
        else:
            sys.exit("[!] 撤销失败：卡密不存在或已作废")
    finally:
        db.close()


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app", host=args.host, port=args.port, reload=args.reload, workers=args.workers
    )


def _write_csv(path: Path, cards: list[dict], with_pwd: bool) -> None:
    # utf-8-sig 带 BOM，Excel 打开中文不乱码
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        header = ["card_no", "duration_days"] + (["card_pwd"] if with_pwd else [])
        writer.writerow(header)
        for c in cards:
            row = [c["card_no"], c.get("duration_days", 0)]
            if with_pwd:
                row.append(c["card_pwd"])
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py", description="卡密系统命令行工具", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="生成 .env 并建表")
    p.add_argument("--force", action="store_true", help="覆盖已有的 .env（会换掉管理密钥！）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("migrate", help="已有数据库补列（时长卡字段），幂等")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("gen", help="生成一个批次的卡密")
    p.add_argument("-p", "--product", required=True, help="产品/套餐标识")
    p.add_argument("-n", "--count", type=int, required=True, help="生成数量")
    p.add_argument("--note", default="", help="备注")
    p.add_argument("-o", "--out", help="导出 CSV 路径；不填则打印到控制台")
    p.add_argument("--with-pwd", action="store_true", help="同时生成卡密密码（第二因子）")
    p.add_argument("--pwd-length", type=int, default=8, help="密码长度，默认 8")
    p.add_argument(
        "--days", type=int, default=0, help="时长卡天数，从激活起算；0 = 永久（默认）"
    )
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("export", help="导出批次卡密（含明文，注意保管）")
    p.add_argument("-b", "--batch-no", required=True)
    p.add_argument("-o", "--out", help="输出 CSV 路径")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("stats", help="全库统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("batch", help="单批次统计")
    p.add_argument("batch_no")
    p.add_argument("--card-no", help="顺带打印这张卡的核销记录")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("disable", help="作废一张未使用的卡")
    p.add_argument("card_no")
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("revoke", help="撤销一张卡（已激活的也会立即失效）")
    p.add_argument("card_no")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--workers", type=int, default=1, help=">1 时限流需换 Redis 版本")
    p.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    # init 之外的命令都依赖 .env 已经存在
    args = build_parser().parse_args()
    if args.command != "init" and not ENV_PATH.exists():
        sys.exit("[!] 未找到 .env，请先运行：python manage.py init")
    args.func(args)


if __name__ == "__main__":
    main()
