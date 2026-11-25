"""Trusted server sales snapshot. Reading/DTO validity never grants a write."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_UP

from pydantic import ValidationError
from sqlalchemy import select, text

from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.sales import service as sales
from backend.app.modules.sales.models import SalesDraftLine, SalesHandoff, PosDispatch
from backend.app.modules.erp_sales.erp_004_schemas import SalesSourceSnapshot, compute_line_amounts
from backend.app.modules.erp_commands.erp_003_wire import canonical_digest


class ERPSalesError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def fail(code):
    raise ERPSalesError('erp_sales_'+code)


def clock(db):
    sql="SELECT clock_timestamp()" if db.get_bind().dialect.name=='postgresql' else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    value=db.execute(text(sql)).scalar_one()
    value=datetime.fromisoformat(value) if type(value) is str else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def scope(model,actor):
    return (model.organization_id==actor.organization_id,model.store_id==actor.store_id)


def require_person(db,actor,permission,now):
    if (not getattr(actor,'account_id',None) or not getattr(actor,'session_id',None)
            or type(getattr(actor,'account_version',None)) is not int or actor.account_version<=0):
        fail('human_required')
    authorize_scope(db,actor,organization_id=actor.organization_id,store_id=actor.store_id,
                    permission=permission,now=now)
    if not has_usable_account(db,organization_id=actor.organization_id,store_id=actor.store_id,
                             subject_id=actor.subject_id,now=now):
        fail('human_required')


def guard(db,actor,permission):
    if not getattr(actor,'account_id',None) or not getattr(actor,'session_id',None):
        fail('human_required')
    lock_employee_authority(db,actor)
    now=clock(db)
    require_person(db,actor,'sales.read',now)
    require_person(db,actor,permission,now)
    return now


def _snapshot(db,actor,draft_id,*,require_handoff=False):
    """Read under the same employee fence and draft CAS lock as sales writes.

    Snapshot normalization is derived; no source price, quantity or state is
    rewritten. Existing per-quote source/approval validation remains authority.
    """
    now=guard(db,actor,'sales.read')
    draft=sales._draft(db,actor,draft_id,lock=True)
    if (draft.stage!='ready' or draft.status!='pending' or draft.voided_at is not None
            or (draft.expires_at is not None and draft.expires_at<=now)):
        fail('draft_not_ready')
    # Refuse oversized/unconfirmed input before the legacy validator iterates
    # every line; its monetary/source validation still runs on every valid row.
    initial=list(db.scalars(select(SalesDraftLine).where(*scope(SalesDraftLine,actor),
        SalesDraftLine.draft_id==draft.id).limit(51).execution_options(populate_existing=True)))
    if not 1<=len(initial)<=50 or any(line.confirmed_by is None or line.confirmed_at is None for line in initial):
        fail('lines_unconfirmed')
    sales.validate_draft_quotes(db,actor,draft,now)
    # Reload source rows after quote services revalidate their own ORM snapshots.
    draft=sales._draft(db,actor,draft_id,lock=True)
    lines=list(db.scalars(select(SalesDraftLine).where(*scope(SalesDraftLine,actor),
        SalesDraftLine.draft_id==draft.id).order_by(SalesDraftLine.created_at,SalesDraftLine.id)
        .execution_options(populate_existing=True).with_for_update()))
    if not 1<=len(lines)<=50 or any(line.confirmed_by is None or line.confirmed_at is None for line in lines):
        fail('lines_unconfirmed')
    handoff=db.scalar(select(SalesHandoff).where(*scope(SalesHandoff,actor),
        SalesHandoff.draft_id==draft.id).execution_options(populate_existing=True).with_for_update())
    if require_handoff:
        if (handoff is None or handoff.handoff_state!='requested' or handoff.external_ref is not None
                or handoff.delivery_ref is not None or db.scalar(select(PosDispatch.id).where(
                    *scope(PosDispatch,actor),PosDispatch.draft_id==draft.id)) is not None):
            fail('route_already_bound')
    values=[]
    try:
        for line in lines:
            discounted,_=compute_line_amounts(line.unit_price,line.quantity,line.discount_bps)
            values.append(dict(id=line.id,line_key=line.line_key,product_id=line.product_id,
                product_name=line.product_name,quote_id=line.quote_id,quantity=line.quantity,
                unit_price=line.unit_price,discount_bps=line.discount_bps,discounted_unit_price=format(discounted,'f'),
                line_amount=line.line_amount,confirmed_by=line.confirmed_by,confirmed_at=line.confirmed_at))
        result=SalesSourceSnapshot.model_validate(dict(organization_id=actor.organization_id,store_id=actor.store_id,
            draft_id=draft.id,draft_version=draft.version,handoff_id=handoff.id if handoff else None,
            stage=draft.stage,status=draft.status,currency=draft.currency,total_amount=draft.total_amount,
            expires_at=draft.expires_at,lines=values))
    except (ValidationError,ArithmeticError,ValueError,TypeError):
        fail('source_invalid')
    require_person(db,actor,'sales.read',clock(db))
    return result


def snapshot(db,actor,draft_id,*,require_handoff=False):
    """Stable sales-command domain boundary; retain auth HTTP401/403 intact."""
    try:
        return _snapshot(db,actor,draft_id,require_handoff=require_handoff)
    except sales.SalesError as exc:
        fail('not_found' if exc.code=='sales_draft_not_found' else 'source_invalid')
    except (ArithmeticError,ValueError,TypeError) as exc:
        if isinstance(exc,ERPSalesError):
            raise
        fail('source_invalid')


def source_digest(value):
    return canonical_digest(value.model_dump(mode='json'))


def reject_bound_draft(db,actor,draft_id):
    """Legacy entry guard, not an authority or migration fallback."""
    from backend.app.modules.erp_sales.erp_004_models import ERPSalesCommand
    if db.scalar(select(ERPSalesCommand.id).where(*scope(ERPSalesCommand,actor),
            ERPSalesCommand.draft_id==draft_id)) is not None:
        fail('managed_sales_route_required')
