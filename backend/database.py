"""
Database layer — async SQLAlchemy + Postgres/SQLite.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./alpha_hunter.db")

# Render's Postgres connection string comes as postgres:// or postgresql://,
# which SQLAlchemy's async engine needs rewritten to use the asyncpg driver.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class ScanResult(Base):
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(String, index=True)
    symbol = Column(String, index=True)
    score = Column(Float)
    monthly_rsi = Column(Float)
    passed_gate = Column(Integer)
    entry = Column(Float)
    stop_loss = Column(Float)
    tp1 = Column(Float)
    tp2 = Column(Float)
    tp3 = Column(Float)
    volume_24h = Column(Float)
    breakdown = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TokenOutcome(Base):
    """
    Outcome tracking — records what actually happened to a hunted token
    after it was discovered, checked at fixed time checkpoints (15m, 1h,
    4h, 24h, 3d, 7d). This is the foundation for real (not backtested)
    win-rate stats: 'did our signals actually work?'

    v1 scope: Binance-sourced tokens only (DEX price re-checking would need
    a different price source per chain — can be added later).
    """
    __tablename__ = "token_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    scan_id = Column(String, index=True)
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    score = Column(Float)
    tier = Column(String)
    entry = Column(Float)
    stop_loss = Column(Float)
    tp1 = Column(Float)
    tp2 = Column(Float)
    tp3 = Column(Float)
    checkpoints = Column(Text, default="{}")  # JSON: {"15m": {"price":..,"hit_tp1":bool,...}, ...}
    status = Column(String, default="pending")  # pending | tp1 | tp2 | tp3 | stopped | none
    completed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
