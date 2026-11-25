from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (UniqueConstraint("organization_id", "id", name="uq_stores_org_id"),)


class Subject(Base):
    __tablename__ = "subjects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False, default="person", server_default="person")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (UniqueConstraint("organization_id", "id", name="uq_subjects_org_id"),)


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role_key: Mapped[str] = mapped_column(String(80), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_role_assignments_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_role_assignments_subject_scope", ondelete="RESTRICT"),
        Index("ix_role_assignments_scope", "organization_id", "store_id", "subject_id", "active"),
    )


class PermissionGrant(Base):
    __tablename__ = "permission_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    permission_key: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_permission_grants_store_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["organization_id", "subject_id"], ["subjects.organization_id", "subjects.id"], name="fk_permission_grants_subject_scope", ondelete="RESTRICT"),
        CheckConstraint("(subject_id IS NOT NULL AND role_key IS NULL) OR (subject_id IS NULL AND role_key IS NOT NULL)", name="ck_permission_grants_exactly_one_selector"),
        Index("ix_permission_grants_scope", "organization_id", "store_id", "permission_key", "active"),
    )


class ScopeSecurityFence(Base):
    """Session and work epochs share one physical organization/store lock.

    Every revocation-capable change (logout, account invalidation, lockout,
    personal permission change, machine credential change) takes the exclusive
    row lock on this scope and increments ``version``; read-only command guards
    take the shared row lock, so they serialize against those changes. The epoch
    work_version also advances except on ordinary single-session logout. Each
    is monotonic and only ever moves forward: it is an opaque fence value that
    later execution records compare against, never a permission source itself.
    """

    __tablename__ = "scope_security_fences"
    organization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    work_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.current_timestamp())
    __table_args__ = (
        ForeignKeyConstraint(["organization_id", "store_id"], ["stores.organization_id", "stores.id"], name="fk_scope_security_fences_store_scope", ondelete="RESTRICT"),
        CheckConstraint("version > 0", name="ck_scope_security_fence_version"),
        CheckConstraint("work_version > 0", name="ck_scope_security_fence_work_version"),
    )
