from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_S,
    pool_recycle=settings.DB_POOL_RECYCLE_S,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        from app.core.db.registry import import_all_models
        import_all_models()   # every module's tables (app/core/db/registry.py)
        await conn.run_sync(Base.metadata.create_all)
    from app.core.db import upgrade
    await upgrade.run(engine)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
