"""请求/响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


class CreateBatchIn(BaseModel):
    product: str = Field(min_length=1, max_length=64, description="产品/套餐标识")
    count: int = Field(ge=1, le=100_000, description="生成数量")
    note: str = Field(default="", max_length=255)
    with_pwd: bool = Field(default=False, description="是否额外生成卡密密码（第二因子）")
    pwd_length: int = Field(default=8, ge=0, le=32)
    duration_days: int = Field(default=0, ge=0, le=3650, description="时长卡天数，0 = 永久")


class RedeemIn(BaseModel):
    card_no: str = Field(min_length=4, max_length=64)
    card_pwd: str = Field(default="", max_length=64)
    device_id: str = Field(
        default="", max_length=128, description="调用方的设备/账号标识，用于绑定和排查"
    )


class VerifyIn(BaseModel):
    """启动时的授权校验。不绑定设备，device_id 只用于日志。"""

    card_no: str = Field(min_length=4, max_length=64)
    device_id: str = Field(default="", max_length=128)


def ok(data: Any = None, msg: str = "ok") -> dict:
    return {"code": 0, "msg": msg, "data": data}


def fail(code: int, msg: str, data: Any = None) -> dict:
    return {"code": code, "msg": msg, "data": data}
