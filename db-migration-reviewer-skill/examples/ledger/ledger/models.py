from sqlalchemy import Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    # legacy_code was excluded from the model in release 2.3; its drop ships now (0009).


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    category = Column(String(40))
