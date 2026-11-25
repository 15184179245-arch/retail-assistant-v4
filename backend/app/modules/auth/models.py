from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class EmployeeAccount(Base):
    __tablename__ = "employee_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())

    __table_args__ = (
        UniqueConstraint("username", name="uq_employee_accounts_username"),
        UniqueConstraint("organization_id", "store_id", "subject_id", name="uq_employee_accounts_store_subject"),
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_employee_accounts_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_employee_accounts_subject_scope", ondelete="RESTRICT"),
        CheckConstraint("version >= 1", name="ck_employee_accounts_version_positive"),
        CheckConstraint("failed_attempts >= 0", name="ck_employee_accounts_failed_attempts_nonnegative"),
    )


class LoginSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("employee_accounts.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        CheckConstraint("account_version >= 1", name="ck_auth_sessions_account_version_positive"),
        Index("ix_auth_sessions_account", "account_id"),
    )
