"""Shared SQLAlchemy metadata for tables owned by the application database.

Domain packages define their own rows, while this module supplies only the
declarative registry and a timezone-aware default. Keeping the registry here
prevents one domain (for example, Audit) from depending on another domain's
table module merely to participate in the same database schema.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Return an aware UTC timestamp suitable for SQLAlchemy defaults."""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base shared by FinanceClaw application-owned tables."""
