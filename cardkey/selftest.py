#!/usr/bin/env python
"""端到端自测。不依赖 pytest：

    python selftest.py

跑在一个独立的 selftest.db 上，不碰正式库。
"""

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEST_DB = BASE_DIR / "selftest.db"

# 必须在 import app.* 之前设置 —— app.config 在导入时就固化配置了
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["REDEEM_RATE_ATTEMPTS"] = "100000"  # 常规用例先关掉限流干扰
os.environ["VERIFY_RATE_ATTEMPTS"] = "100000"
os.environ["TRUST_PROXY"] = "false"

if TEST_DB.exists():
    TEST_DB.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from app import models, service  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app, redeem_limiter, verify_limiter  # noqa: E402
from app.security import CARD_ALPHABET_SET  # noqa: E402

Base.metadata.create_all(engine)
client = TestClient(app)
ADMIN = {"X-Admin-Key": settings.admin_api_key}

_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(f"{name} {detail}")
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #

section("1. 健康检查与管理端鉴权")

r = client.get("/health")
check("GET /health 返回 200", r.status_code == 200, f"got {r.status_code}")

r = client.get("/admin/stats")
check("无密钥访问 /admin/* → 401", r.status_code == 401, f"got {r.status_code}")

r = client.get("/admin/stats", headers={"X-Admin-Key": "wrong-key"})
check("错误密钥 → 401", r.status_code == 401, f"got {r.status_code}")

r = client.get("/admin/stats", headers=ADMIN)
check("正确密钥 → 200", r.status_code == 200, f"got {r.status_code}")


section("2. 生成批次")

r = client.post(
    "/admin/batches", headers=ADMIN, json={"product": "vip", "count": 200, "note": "自测"}
)
body = r.json()
check("生成 200 张成功", r.status_code == 200 and body["code"] == 0, str(body)[:200])
cards = body["data"]["cards"]
batch_no = body["data"]["batch_no"]
card_nos = [c["card_no"] for c in cards]

check("返回数量正确", len(card_nos) == 200, f"got {len(card_nos)}")
check("卡号无重复", len(set(card_nos)) == 200, f"unique={len(set(card_nos))}")
check(
    "卡号只含 31 个安全字符",
    all(ch in CARD_ALPHABET_SET or ch == "-" for no in card_nos for ch in no),
)
check("卡号不含易混字符 0/O/1/I/L", not any(ch in "0O1IL" for no in card_nos for ch in no))
check("卡号格式为 5 组 4 位", all(len(no) == 24 and no.count("-") == 4 for no in card_nos))
check("未设密码时 card_pwd 为空", all(c["card_pwd"] == "" for c in cards))

r = client.get(f"/admin/batches/{batch_no}/stats", headers=ADMIN)
stats = r.json()["data"]
check("批次统计 total=200 / unused=200", stats["total"] == 200 and stats["unused"] == 200, str(stats))


section("3. 核销主流程")

target = card_nos[0]

r = client.post("/redeem", json={"card_no": target, "device_id": "pc-001"})
body = r.json()
check("首次核销成功", body["code"] == 0 and body["msg"] == "核销成功", str(body)[:200])

r = client.post("/redeem", json={"card_no": target, "device_id": "pc-002"})
body = r.json()
check("重复核销被拒", body["code"] == 1001 and "已被使用" in body["msg"], str(body)[:200])

r = client.post("/redeem", json={"card_no": target, "device_id": "pc-003"})
check("第二次重复核销仍被拒", r.json()["code"] == 1001, str(r.json())[:200])

r = client.post("/redeem", json={"card_no": "ZZZZ-ZZZZ-ZZZZ-ZZZZ-ZZZZ"})
check("不存在的卡密被拒", r.json()["code"] == 1001 and "不存在" in r.json()["msg"], str(r.json())[:200])

# 大小写 / 分隔符容错
loose = card_nos[1].lower().replace("-", " ")
r = client.post("/redeem", json={"card_no": loose, "device_id": "pc-004"})
check("小写+空格输入的卡密能正常核销", r.json()["code"] == 0, str(r.json())[:200])

# 状态回写检查
r = client.get("/admin/cards", headers=ADMIN, params={"status": 1, "size": 500})
used = r.json()["data"]["items"]
check("已用卡统计为 2 张", r.json()["data"]["total"] == 2, f"got {r.json()['data']['total']}")
check("已用卡记录了 device_id", any(i["used_by"] == "pc-001" for i in used), str(used)[:300])
check("列表接口返回的卡号是打码的", all("*" in i["card_no"] for i in used), str(used)[:200])


section("3b. 时长卡与 /verify")

r = client.post(
    "/admin/batches",
    headers=ADMIN,
    json={"product": "tq", "count": 3, "duration_days": 30},
)
td = r.json()["data"]
td_cards = [c["card_no"] for c in td["cards"]]
check("时长卡批次 duration_days=30", td["duration_days"] == 30, str(td)[:200])

r = client.post("/verify", json={"card_no": td_cards[0]})
check(
    "未激活 → 1002 尚未激活",
    r.json()["code"] == 1002 and "尚未激活" in r.json()["msg"],
    str(r.json())[:200],
)

r = client.post("/redeem", json={"card_no": td_cards[0], "device_id": "pc-tq"})
body = r.json()
check("时长卡激活成功", body["code"] == 0, str(body)[:200])
exp = body["data"]["expires_at"]
check(
    "redeem 返回 expires_at ≈ now + 30 天",
    exp is not None and abs(exp - (int(time.time()) + 30 * 86400)) < 120,
    f"expires_at={exp}",
)

r = client.post("/verify", json={"card_no": td_cards[0], "device_id": "another-pc"})
body = r.json()
check("已激活卡可 verify（不绑定设备）", body["code"] == 0, str(body)[:200])
check("verify 返回同一 expires_at", body["data"]["expires_at"] == exp, str(body)[:200])
check("verify 幂等，可重复调用", client.post("/verify", json={"card_no": td_cards[0]}).json()["code"] == 0)

# 过期：直接把 expires_at 改到过去
db = SessionLocal()
try:
    card = service.get_card(db, td_cards[0])
    card.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db.commit()
finally:
    db.close()
r = client.post("/verify", json={"card_no": td_cards[0]})
check(
    "已过期 → 1002 已过期",
    r.json()["code"] == 1002 and "过期" in r.json()["msg"],
    str(r.json())[:200],
)

# 撤销已激活的卡
client.post("/redeem", json={"card_no": td_cards[1]})
r = client.post(f"/admin/cards/{td_cards[1]}/revoke", headers=ADMIN)
check("撤销已激活卡成功", r.status_code == 200 and r.json()["code"] == 0, str(r.json())[:200])
r = client.post("/verify", json={"card_no": td_cards[1]})
check(
    "已撤销 → 1002 已作废",
    r.json()["code"] == 1002 and "作废" in r.json()["msg"],
    str(r.json())[:200],
)

# 作废未激活的卡
client.post(f"/admin/cards/{td_cards[2]}/disable", headers=ADMIN)
r = client.post("/verify", json={"card_no": td_cards[2]})
check("未使用即作废 → 1002 已作废", r.json()["code"] == 1002, str(r.json())[:200])

r = client.post("/verify", json={"card_no": "ZZZZ-ZZZZ-ZZZZ-ZZZZ-ZZZZ"})
check(
    "不存在的卡 → 1002 不存在",
    r.json()["code"] == 1002 and "不存在" in r.json()["msg"],
    str(r.json())[:200],
)


section("4. 第二因子（card_pwd）")

r = client.post(
    "/admin/batches",
    headers=ADMIN,
    json={"product": "svip", "count": 5, "with_pwd": True, "pwd_length": 12},
)
pwd_batch = r.json()["data"]
pwd_batch_no = pwd_batch["batch_no"]
pwd_cards = pwd_batch["cards"]
c = pwd_cards[0]
check("生成了 12 位卡密密码", len(c["card_pwd"]) == 12, f"got {len(c['card_pwd'])}")

r = client.post("/redeem", json={"card_no": c["card_no"], "card_pwd": "WRONG"})
check("密码错误被拒", r.json()["code"] == 1001 and "密码错误" in r.json()["msg"], str(r.json())[:200])

r = client.get("/admin/cards", headers=ADMIN, params={"product": "svip"})
check(
    "密码错误后卡片仍是未使用（没有被消耗）",
    r.json()["data"]["items"][0]["status"] == 0,
    str(r.json()["data"]["items"][0])[:200],
)

r = client.post("/redeem", json={"card_no": c["card_no"], "card_pwd": c["card_pwd"]})
check("密码正确核销成功", r.json()["code"] == 0, str(r.json())[:200])

r = client.post(f"/admin/cards/{c['card_no']}/reveal", headers=ADMIN)
check("reveal 能取回明文密码", r.json()["data"]["card_pwd"] == c["card_pwd"], str(r.json())[:200])


section("5. 作废")

d = card_nos[2]
r = client.post(f"/admin/cards/{d}/disable", headers=ADMIN)
check("作废成功", r.status_code == 200 and r.json()["code"] == 0, str(r.json())[:200])

r = client.post(f"/admin/cards/{d}/disable", headers=ADMIN)
check("重复作废 → 409", r.status_code == 409, f"got {r.status_code}")

r = client.post("/redeem", json={"card_no": d})
check("已作废的卡不能核销", r.json()["code"] == 1001 and "作废" in r.json()["msg"], str(r.json())[:200])

r = client.post(f"/admin/cards/{target}/disable", headers=ADMIN)
check("已使用的卡不能作废 → 409", r.status_code == 409, f"got {r.status_code}")


section("6. 并发核销（原子性）—— 最关键的一项")

race_card = card_nos[3]
N = 16
barrier = threading.Barrier(N)
outcomes: list[tuple[str, str]] = []
outcome_lock = threading.Lock()


def worker(idx: int) -> None:
    db = SessionLocal()
    try:
        barrier.wait(timeout=30)
        result = service.redeem(db, race_card, ip="10.0.0.1", device_id=f"race-{idx}")
        with outcome_lock:
            outcomes.append(("ok" if result.success else "fail", result.reason))
    except Exception as exc:  # noqa: BLE001
        with outcome_lock:
            outcomes.append(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        db.close()


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)

ok_count = sum(1 for kind, _ in outcomes if kind == "ok")
err_count = sum(1 for kind, _ in outcomes if kind == "error")
check(f"{N} 线程并发核销同一张卡，恰好 1 次成功", ok_count == 1, f"成功 {ok_count} 次 / {outcomes}")
check("并发过程中无异常", err_count == 0, str([o for o in outcomes if o[0] == "error"]))

db = SessionLocal()
try:
    card = service.get_card(db, race_card)
    check("数据库中该卡状态为已使用", card.status == models.STATUS_USED, f"status={card.status}")
    log_count = service.redeem_log_summary(db, race_card, limit=100)
    check("每次尝试都有日志落库", len(log_count) == N, f"got {len(log_count)}")
finally:
    db.close()


section("7. 限流")

redeem_limiter.limit = 3
redeem_limiter.window = 60
redeem_limiter._hits.clear()

codes = [
    client.post("/redeem", json={"card_no": card_nos[10 + i]}).status_code for i in range(5)
]
check("超出阈值后返回 429", codes[-1] == 429, f"codes={codes}")
check("阈值内放行", codes[0] == 200 and codes[1] == 200 and codes[2] == 200, f"codes={codes}")

r = client.post("/redeem", json={"card_no": card_nos[99]})
check("429 响应带 Retry-After 头", "retry-after" in {k.lower() for k in r.headers}, str(dict(r.headers)))

verify_limiter.limit = 2
verify_limiter.window = 60
verify_limiter._hits.clear()
codes = [client.post("/verify", json={"card_no": td_cards[0]}).status_code for _ in range(4)]
check("verify 超出阈值 → 429", codes[-1] == 429, f"codes={codes}")
check("verify 阈值内放行", codes[0] == 200 and codes[1] == 200, f"codes={codes}")


section("8. 统计与日志")

r = client.get("/admin/stats", headers=ADMIN)
s = r.json()["data"]
check("统计接口可用且数字自洽", s["cards_total"] == s["unused"] + s["used"] + s["disabled"], str(s))

r = client.get("/admin/logs", headers=ADMIN, params={"success": False, "size": 5})
check("能查到失败日志", r.json()["data"]["total"] > 0, str(r.json()["data"])[:200])
check("日志里的卡号已打码", all("*" in i["card_no"] for i in r.json()["data"]["items"]))

r = client.get("/admin/batches", headers=ADMIN)
check("批次列表可用", r.json()["data"]["total"] == 3, str(r.json()["data"])[:200])

r = client.get("/admin/cards", headers=ADMIN, params={"batch_no": "NOPE-123"})
check("不存在的批次 → 404", r.status_code == 404, f"got {r.status_code}")


section("9. 管理后台与新增接口")

# --- 静态页面与安全头 ---
r = client.get("/")
check(
    "GET / 返回后台页面",
    r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
    f"status={r.status_code}",
)
check("首页 no-cache", r.headers.get("cache-control") == "no-cache", str(dict(r.headers))[:200])
csp = r.headers.get("content-security-policy", "")
check(
    "首页带严格 CSP",
    "default-src 'self'" in csp and "frame-ancestors 'none'" in csp,
    csp[:200],
)
check("首页不泄露管理员密钥", (settings.admin_api_key or "NONE") not in r.text)
check("首页无内联 onclick", "onclick=" not in r.text)
check(
    "安全头覆盖全部响应",
    client.get("/health").headers.get("x-content-type-options") == "nosniff"
    and client.get("/health").headers.get("x-frame-options") == "DENY",
)

r = client.get("/assets/app.js")
check(
    "静态 JS 可访问",
    r.status_code == 200 and len(r.content) > 1000,
    f"got {r.status_code}",
)
etag = r.headers.get("etag")
check("静态资源带 ETag", bool(etag), str(dict(r.headers))[:200])
if etag:
    check(
        "If-None-Match 命中 304",
        client.get("/assets/app.js", headers={"If-None-Match": etag}).status_code == 304,
    )
check("不存在的静态资源 → 404", client.get("/assets/nope.js").status_code == 404)

# --- 产品统计 ---
check("无密钥访问 /admin/products → 401", client.get("/admin/products").status_code == 401)
r = client.get("/admin/products", headers=ADMIN)
product_items = r.json()["data"]["items"]
check(
    "产品统计自洽",
    all(i["total"] == i["unused"] + i["used"] + i["disabled"] for i in product_items),
    str(product_items)[:300],
)

# --- 批次计数 ---
r = client.get("/admin/batches", headers=ADMIN)
batch_items = r.json()["data"]["items"]
check(
    "批次列表带状态计数",
    all({"unused", "used", "disabled", "expired"} <= set(i) for i in batch_items),
    str(batch_items)[:300],
)
target_batch = batch_items[0]["batch_no"]
r = client.get("/admin/cards", headers=ADMIN, params={"batch_no": target_batch, "size": 500})
batch_card_total = r.json()["data"]["total"]
target_item = next(i for i in batch_items if i["batch_no"] == target_batch)
check(
    "批次计数合计等于卡数",
    target_item["unused"] + target_item["used"] + target_item["disabled"] == batch_card_total,
    f"{target_item} total={batch_card_total}",
)

# --- 卡号搜索 ---
probe = card_nos[30]
r = client.get(
    "/admin/cards", headers=ADMIN, params={"card_no": probe.lower().replace("-", " ")}
)
check("卡号精确搜索（小写+空格）命中", r.json()["data"]["total"] >= 1, str(r.json())[:200])
check("搜索结果带 id", "id" in r.json()["data"]["items"][0])
check(
    "纯分隔符卡号 → 422",
    client.get("/admin/cards", headers=ADMIN, params={"card_no": "___"}).status_code == 422,
)

# --- CSV 导出 ---
r = client.get(f"/admin/batches/{target_batch}/export", headers=ADMIN)
check(
    "批次导出 CSV",
    r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""),
    f"status={r.status_code}",
)
check("CSV 带 UTF-8 BOM", r.content[:3] == b"\xef\xbb\xbf", str(r.content[:6]))
check(
    "CSV 行数 = 卡数 + 1",
    len(r.text.strip().splitlines()) == batch_card_total + 1,
    f"{len(r.text.strip().splitlines())} vs {batch_card_total + 1}",
)
check(
    "CSV 带 attachment 头",
    "attachment" in r.headers.get("content-disposition", ""),
    str(dict(r.headers))[:200],
)
check(
    "不存在的批次导出 → 404",
    client.get("/admin/batches/NOPE-123/export", headers=ADMIN).status_code == 404,
)

r = client.get(
    f"/admin/batches/{pwd_batch_no}/export?include_pwd=true", headers=ADMIN
)
check("含密码导出包含明文密码", pwd_cards[0]["card_pwd"] in r.text)
r = client.get(f"/admin/batches/{pwd_batch_no}/export", headers=ADMIN)
check("默认导出不含密码", pwd_cards[0]["card_pwd"] not in r.text)

# --- 统计扩展 ---
s = client.get("/admin/stats", headers=ADMIN).json()["data"]
check(
    "stats 新字段齐全",
    {"expired", "expiring_7d", "redeem_failed", "verify_failed", "batches", "server_time"}
    <= set(s),
    str(sorted(s.keys())),
)
check("stats 总数自洽", s["cards_total"] == s["unused"] + s["used"] + s["disabled"])
check("过期 + 即将到期 <= 已使用", s["expired"] + s["expiring_7d"] <= s["used"], str(s))
check(
    "失败次数拆分自洽",
    s["redeem_failed"] + s["verify_failed"] == s["redeem_failed_attempts"],
    str(s),
)

# --- 日志前缀过滤 ---
r = client.get("/admin/logs", headers=ADMIN, params={"reason_prefix": "verify:"})
check(
    "日志前缀过滤",
    all(item["reason"].startswith("verify:") for item in r.json()["data"]["items"]),
    str(r.json()["data"]["items"])[:300],
)

# --- 按 id 操作 ---
first = client.get("/admin/cards", headers=ADMIN, params={"status": 0, "size": 1}).json()[
    "data"
]["items"][0]
r = client.post(f"/admin/cards/by-id/{first['id']}/reveal", headers=ADMIN)
check("按 id 查看明文", r.status_code == 200 and bool(r.json()["data"]["card_no"]))
r = client.post(f"/admin/cards/by-id/{first['id']}/disable", headers=ADMIN)
check("按 id 作废", r.status_code == 200 and r.json()["code"] == 0)
r = client.post(f"/admin/cards/by-id/{first['id']}/revoke", headers=ADMIN)
check("按 id 重复撤销 → 409", r.status_code == 409)


# --------------------------------------------------------------------------- #

print("\n" + "=" * 60)
if _failed:
    print(f"通过 {_passed} 项，失败 {len(_failed)} 项：")
    for f in _failed:
        print(f"  - {f}")
    sys.exit(1)

print(f"全部通过（{_passed} 项）")
