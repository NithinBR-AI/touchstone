"""
examples/scan-your-own/models.py: a "your own repo" declared schema for trying
the Touchstone CLI.

It is the demo target's models.py with three extra changes that the demo does
not cover, to show that Touchstone scans whatever models you point it at:

  1. users.phone              declared, not in the DB   -> COLUMN_MISSING
  2. products.name            String(200) vs DB's 100   -> TYPE_CHANGED (widening)
  3. reviews table            declared, not in the DB   -> TABLE_MISSING

Run (read-only; compare never writes to the database):
  $env:MODELS_PATH = "<path to this file>"
  touchstone compare
"""

from sqlalchemy import TIMESTAMP, Column, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True)
    age = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    phone = Column(String(20), nullable=True)  # new: not in the live DB


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    total = Column(Numeric(10, 2), nullable=True)
    status = Column(String(20), nullable=True)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)  # changed: DB has VARCHAR(100)
    price = Column(Numeric(10, 2), nullable=True)
    stock = Column(Integer, nullable=True)


class Review(Base):  # new: table not in the live DB
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    rating = Column(Integer, nullable=False)


ix_orders_user_id = Index("ix_orders_user_id", Order.user_id)
