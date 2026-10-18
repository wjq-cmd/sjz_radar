"""卡密生成、加解密、鉴权。"""

import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

# 去掉了 0/O、1/I/L 这些肉眼容易混淆的字符，方便用户手抄
CARD_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CARD_ALPHABET_SET = frozenset(CARD_ALPHABET)

_SEPARATORS = frozenset(" \t\r\n-_")


def _fernet() -> Fernet:
    if not settings.card_fernet_key:
        raise RuntimeError("CARD_FERNET_KEY 未配置，请先运行：python manage.py init")
    return Fernet(settings.card_fernet_key.encode())


def gen_card_no(length: int | None = None, group: int | None = None) -> str:
    """用 CSPRNG 生成卡号。

    绝不要用自增 ID / 时间戳 / 可推导序列来拼卡号 —— 那是被刷库的直接原因。
    secrets.choice 内部走 randbelow，取值是无偏的。
    """
    length = length or settings.card_no_length
    group = settings.card_no_group if group is None else group
    raw = "".join(secrets.choice(CARD_ALPHABET) for _ in range(length))
    if group > 0:
        return "-".join(raw[i : i + group] for i in range(0, length, group))
    return raw


def normalize_card_no(value: str) -> str:
    """统一成存储格式：转大写、去掉分隔符，再按配置重新分组。

    只剔除分隔符（空格、横线、下划线），其余字符原样保留 —— 用户输错了应该
    报「卡密不存在」，而不是被静默纠正成一个别的卡号。
    """
    raw = "".join(ch for ch in value.upper() if ch not in _SEPARATORS)
    group = settings.card_no_group
    if group > 0:
        return "-".join(raw[i : i + group] for i in range(0, len(raw), group))
    return raw


def mask_card_no(card_no: str) -> str:
    """后台列表展示用：保留首尾分组，中间打码。"""
    parts = card_no.split("-")
    if len(parts) <= 2:
        if len(card_no) <= 8:
            return card_no
        return f"{card_no[:4]}{'*' * (len(card_no) - 8)}{card_no[-4:]}"
    return "-".join([parts[0], *("*" * len(p) for p in parts[1:-1]), parts[-1]])


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def verify_admin(key: str | None) -> bool:
    """定长比较，避免时序侧信道。"""
    if not settings.admin_api_key or not key:
        return False
    return hmac.compare_digest(key, settings.admin_api_key)
