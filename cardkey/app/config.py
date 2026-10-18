"""全局配置。所有可调项都可以通过 .env 覆盖。"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据库。默认 SQLite 开箱即用；上生产换 MySQL：
    #   DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/cardkey?charset=utf8mb4
    database_url: str = f"sqlite:///{(BASE_DIR / 'cardkey.db').as_posix()}"

    # 管理端密钥。所有 /admin/* 接口都要在请求头带 X-Admin-Key
    admin_api_key: str = ""

    # 卡密第二因子（card_pwd）的加密密钥，Fernet 格式，由 manage.py init 自动生成
    card_fernet_key: str = ""

    # 卡号长度与分组。字符集 31 个，20 位 ≈ 99 bit 熵，撞库不现实
    card_no_length: int = 20
    card_no_group: int = 4

    # 核销接口限流：window 秒内同一 IP 最多 attempts 次
    redeem_rate_window: int = 60
    redeem_rate_attempts: int = 10

    # 启动校验 /verify 限流（客户端每次启动都会调用，阈值放宽）
    verify_rate_window: int = 60
    verify_rate_attempts: int = 30

    # 生产环境建议关闭 /docs、/redoc、/openapi.json
    enable_docs: bool = True

    # 是否信任 X-Forwarded-For 头来取真实 IP。
    # 只有在 Nginx/反代后面、且反代会覆写该头时才打开，否则客户端可以伪造它绕过限流
    trust_proxy: bool = False


settings = Settings()
