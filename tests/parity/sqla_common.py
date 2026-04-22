"""Shared SQLAlchemy model + schema definitions for the three driver apps.

Each driver (psycopg3, asyncpg, psycopg2) uses a DIFFERENT table suffix so
apps can co-exist in the same DB without collision.

Environment variable SQLA_SUFFIX controls the table suffix:
  - "pg3"   - psycopg3 sync
  - "async" - asyncpg async
  - "pg2"   - psycopg2 sync
  - "lite"  - fallback sqlite (used when postgres unreachable)
"""
from __future__ import annotations

import enum
import os
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


SUFFIX = os.environ.get("SQLA_SUFFIX", "pg3")
IS_SQLITE = os.environ.get("SQLA_SQLITE", "0") == "1"


# ── JSON / ARRAY types: switch to JSON on SQLite, JSONB on Postgres ─────────
if IS_SQLITE:
    from sqlalchemy import JSON as JSONType
    ARRAY_TYPE = None  # no arrays in sqlite
else:
    from sqlalchemy.dialects.postgresql import JSONB as JSONType  # type: ignore
    from sqlalchemy.dialects.postgresql import ARRAY as ARRAY_TYPE  # type: ignore


class Base(DeclarativeBase):
    pass


class StatusEnum(str, enum.Enum):
    draft = "draft"
    active = "active"
    archived = "archived"


def _tbl(name: str) -> str:
    return f"{name}_{SUFFIX}"


class User(Base):
    __tablename__ = _tbl("users")
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # naive UTC so asyncpg (TIMESTAMP WITHOUT TIME ZONE) + psycopg accept it.
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        onupdate=datetime.utcnow,
        nullable=True,
    )
    items: Mapped[List["Item"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    __table_args__ = (
        CheckConstraint("age IS NULL OR age >= 0", name=f"ck_{_tbl('users')}_age"),
        Index(f"ix_{_tbl('users')}_name", "name"),
    )


class Category(Base):
    __tablename__ = _tbl("categories")
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    items: Mapped[List["Item"]] = relationship(back_populates="category")


class Item(Base):
    __tablename__ = _tbl("items")
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[StatusEnum] = mapped_column(
        SAEnum(StatusEnum, name=f"status_enum_{SUFFIX}"),
        default=StatusEnum.draft,
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey(f"{_tbl('users')}.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey(f"{_tbl('categories')}.id"),
        nullable=True,
    )
    owner: Mapped[User] = relationship(back_populates="items")
    category: Mapped[Optional[Category]] = relationship(back_populates="items")
    tags_json: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    __table_args__ = (
        UniqueConstraint("owner_id", "title", name=f"uq_{_tbl('items')}_owner_title"),
        CheckConstraint("price >= 0", name=f"ck_{_tbl('items')}_price"),
        Index(f"ix_{_tbl('items')}_status", "status"),
    )


# Composite primary key model
class OrderLine(Base):
    __tablename__ = _tbl("order_lines")
    order_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TagArr(Base):
    # ARRAY column (postgres only). On sqlite, fallback to JSON.
    __tablename__ = _tbl("tagarr")
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    if IS_SQLITE:
        tags: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    else:
        tags = Column(ARRAY_TYPE(String), nullable=True)  # type: ignore


# Pydantic schemas ----------------------------------------------------
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserIn(BaseModel):
    email: str
    name: str
    age: Optional[int] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str
    age: Optional[int] = None
    is_active: bool


class ItemIn(BaseModel):
    title: str
    price: float = 0.0
    quantity: int = 0
    status: StatusEnum = StatusEnum.draft
    description: Optional[str] = None
    owner_id: int
    category_id: Optional[int] = None


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    price: float
    quantity: int
    status: StatusEnum
    description: Optional[str] = None
    owner_id: int
    category_id: Optional[int] = None


class ItemWithOwnerOut(ItemOut):
    owner: UserOut


class CategoryIn(BaseModel):
    name: str


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
