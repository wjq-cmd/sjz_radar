"""数据库引擎与会话。"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # SQLite：默认禁止跨线程复用连接（FastAPI 线程池需要关掉），
    # 并放宽写锁等待，否则并发核销会直接抛 "database is locked"
    connect_args=(
        {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}
    ),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
