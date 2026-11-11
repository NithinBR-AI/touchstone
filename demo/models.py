"""
demo/models.py — Declared (intended) schema for Touchstone demo.

Intentional drift cases vs the Neon DB:
  · users.email  VARCHAR(50) in DB → VARCHAR(255) here   = TYPE_CHANGED, widening, auto-fix
  · users.age    BIGINT in DB      → INTEGER here        = TYPE_CHANGED, narrowing, review-only
  · orders missing ix_orders_user_id                     = INDEX_MISSING, auto-fix
"""

from sqlalchemy import Column, ForeignKey, Index, Integer, Numeric, String, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True)   # intended wider than DB's 50
    age = Column(Integer, nullable=True)                        # intended INTEGER; DB has BIGINT
    created_at = Column(TIMESTAMP, server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    total = Column(Numeric(10, 2), nullable=True)
    status = Column(String(20), nullable=True)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    price = Column(Numeric(10, 2), nullable=True)
    stock = Column(Integer, nullable=True)


# Declared index — intentionally missing from Neon DB
ix_orders_user_id = Index("ix_orders_user_id", Order.user_id)
