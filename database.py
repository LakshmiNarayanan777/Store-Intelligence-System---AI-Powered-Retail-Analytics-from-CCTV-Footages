"""
database.py — SQLite setup via SQLAlchemy.

TEACHING:
  We use SQLite for simplicity — it's file-based, no server needed.
  SQLAlchemy gives us an ORM so we write Python objects, not raw SQL.

  Tables:
    events       — every ingested event (deduplicated by event_id)
    sessions     — one row per visitor session (computed on ingest)
    pos_transactions — loaded from CSV at startup
"""

import os
from sqlalchemy import (
    create_engine, Column, String, Integer, Float,
    Boolean, DateTime, Text, Index
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "./store_intelligence.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # needed for SQLite + FastAPI
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ─── ORM Models ──────────────────────────────────────────────────────────────

class EventRecord(Base):
    __tablename__ = "events"

    event_id    = Column(String, primary_key=True)
    store_id    = Column(String, nullable=False, index=True)
    camera_id   = Column(String)
    visitor_id  = Column(String, nullable=False, index=True)
    event_type  = Column(String, nullable=False)
    timestamp   = Column(DateTime, nullable=False, index=True)
    zone_id     = Column(String, nullable=True)
    dwell_ms    = Column(Integer, default=0)
    is_staff    = Column(Boolean, default=False)
    confidence  = Column(Float)
    queue_depth = Column(Integer, nullable=True)
    sku_zone    = Column(String, nullable=True)
    session_seq = Column(Integer, default=0)
    ingested_at = Column(DateTime, default=datetime.utcnow)


class POSTransaction(Base):
    __tablename__ = "pos_transactions"

    transaction_id  = Column(String, primary_key=True)
    store_id        = Column(String, index=True)
    timestamp       = Column(DateTime, index=True)
    basket_value    = Column(Float)


class AnomalyRecord(Base):
    __tablename__ = "anomalies"

    anomaly_id      = Column(String, primary_key=True)
    store_id        = Column(String, index=True)
    anomaly_type    = Column(String)
    severity        = Column(String)
    description     = Column(Text)
    suggested_action = Column(Text)
    detected_at     = Column(DateTime, default=datetime.utcnow)
    zone_id         = Column(String, nullable=True)
    value           = Column(Float, nullable=True)
    threshold       = Column(Float, nullable=True)
    resolved        = Column(Boolean, default=False)


def create_tables():
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
