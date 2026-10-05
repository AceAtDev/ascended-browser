"""The login vault table, empty here: saving logins needs the Ascended app.

The browser asks it on every page load whether a saved login matches the
site; a real (SQLite) table makes that answer "none" without special cases.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


Base = declarative_base()


class BrowserLoginCredential(Base):
    __tablename__ = "browser_login_credentials"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, default="")
    username = Column(Text, nullable=True)
    password = Column(Text, nullable=True)
    totp = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    uris_json = Column(Text, nullable=False, default="[]")
    expires_at = Column(DateTime, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(_engine)
SessionLocal = sessionmaker(bind=_engine)
