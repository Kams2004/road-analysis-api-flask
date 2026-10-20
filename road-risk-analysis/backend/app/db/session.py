from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        from app.models import job, detection, validation_label, dataset, signalement, cluster_config, vehicle, vehicle_road_state, historical_event, user, auth_token, api_client, audit_log, system_setting  # noqa
        from app.models import rejection_reason, hazard, cluster_area, cluster_zone, detection_review, job_track_point  # noqa
        from app.models import cloud_connection, stream_source, job_event, signalement_vote, hazard_lifecycle  # noqa
        from app.models import cloud_import, training_sample  # noqa
        await conn.run_sync(Base.metadata.create_all)
    from app.db import upgrade
    await upgrade.run(engine)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
