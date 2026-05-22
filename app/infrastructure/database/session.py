import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.infrastructure.database.sql_connect import connect_args_for_async_url

DATABASE_URL = settings.get_async_database_url()
DATABASE_SSLMODE = os.getenv("DATABASE_SSLMODE", "").strip() or None

engine = create_async_engine(
    DATABASE_URL,
    connect_args=connect_args_for_async_url(DATABASE_URL, DATABASE_SSLMODE),
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_timeout=settings.DATABASE_POOL_TIMEOUT,
    echo=settings.DATABASE_ECHO,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
