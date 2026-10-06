"""The login vault table: ``logins.db`` in the data directory, readable only by you.

``ascended-browser login add`` writes it; ``browser_login`` reads it to fill a
sign-in form, and the server scrubs every value in it from what the agent is
shown (see ``ascended_browser.logins``). The browser also asks it on every page
load whether a saved login matches the site.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .paths import data_dir


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


def vault_path():
    return data_dir() / "logins.db"


def _create_private(path) -> None:
    """Create the file owner-only before SQLite opens it (its journal copies the mode)."""
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    try:
        path.chmod(0o600)
    except OSError:
        pass


_path = vault_path()
_create_private(_path)
_engine = create_engine(f"sqlite:///{_path}", connect_args={"check_same_thread": False, "timeout": 15})
Base.metadata.create_all(_engine)
SessionLocal = sessionmaker(bind=_engine)
