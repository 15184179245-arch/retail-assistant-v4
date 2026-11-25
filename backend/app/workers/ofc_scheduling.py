"""Explicitly enabled bounded schedule ticks; reuses the existing Outbox lease.

No daemon/startup registration. Tokens remain only in trusted-host arguments.
"""
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from backend.app.modules.office_scheduling import ofc_008_authority as a
from backend.app.modules.office_scheduling.ofc_008_calendar import candidate_slots, slot_for_date
from backend.app.modules.office_scheduling.ofc_008_models import OfficeSchedule, OfficeScheduleGrant, OfficeScheduleOccurrence
from backend.app.modules.office_scheduling.ofc_008_schemas import CalendarRuleInput
from backend.app.workers.outbox import enqueue


def plan_schedules(db, *, machine_token, enabled=False, limit=20):
    if type(enabled) is not bool or type(limit) is not int or not 1 <= limit <= 100:
        a.fail("input_invalid")
    if not enabled:
        return dict(status="disabled", checked=0, queued=0, skipped=0)
    principal, now = a.machine_guard(db, machine_token)
    query = select(OfficeSchedule).join(OfficeScheduleGrant,
        (OfficeScheduleGrant.schedule_id == OfficeSchedule.id)
        & (OfficeScheduleGrant.organization_id == OfficeSchedule.organization_id)
        & (OfficeScheduleGrant.store_id == OfficeSchedule.store_id)
        & (OfficeScheduleGrant.schedule_version == OfficeSchedule.version)).where(
            *a.scope(OfficeSchedule, principal), OfficeSchedule.state == "active",
            OfficeScheduleGrant.machine_id == principal.machine_id).order_by(
                OfficeSchedule.last_checked_at.asc().nullsfirst(), OfficeSchedule.id).limit(limit).with_for_update()
    rows = list(db.scalars(query))
    counts = dict(status="planned", checked=0, queued=0, skipped=0)
    for row in rows:
        counts["checked"] += 1
        grant = a.current_grant(db, principal, row)
        now = a.clock(db)
        try:
            snapshot = a.validate_grant(db, principal, row, grant, now)
        except a.ScheduleError as exc:
            if exc.code == "schedule_not_yet_valid":
                row.last_checked_at = now
                continue
            code = exc.code.removeprefix("schedule_").upper()
            row.state, row.version, row.updated_at = "needs_reapproval", row.version + 1, now
            a.record(db, principal, row, operation="invalidate", request_key="ofc8invalid:" + grant.id,
                request_hash=a.digest(dict(grant_id=grant.id, operation="invalidate")), now=now,
                references=dict(template_digest=grant.template_digest, decision_code=code,
                                result_schedule_version=row.version))
            # Pending old slots retain their old version binding until consumed.
            row.last_checked_at = now
            continue
        rule = CalendarRuleInput.model_validate(snapshot["rule"]).to_calendar_rule()
        zone = ZoneInfo(rule.timezone_name)
        slots = candidate_slots(rule, now=now)
        oldest = min(s.local_date for s in slots)
        anchor = max(row.last_checked_at or grant.valid_from, grant.valid_from)
        anchor_local = anchor.astimezone(zone)
        previous = anchor_local.date()
        prior_slot = slot_for_date(rule, previous, now=anchor)
        # An observed calendar date is not necessarily an observed trigger: a
        # first approval/tick before that day's trigger must include that day
        # in a later bounded downtime explanation. Never backfill its Task.
        unchecked_anchor_day = (prior_slot.decision == "future"
            or (prior_slot.decision == "gap_skipped" and anchor_local.strftime("%H:%M") < rule.local_time)
            or ((row.last_checked_at is None or row.last_checked_at < grant.valid_from)
                and prior_slot.scheduled_at is not None
                and prior_slot.scheduled_at >= grant.valid_from))
        first_unchecked = previous if unchecked_anchor_day else previous + timedelta(days=1)
        skipped_from, skipped_to = max(first_unchecked, row.start_date), oldest - timedelta(days=1)
        if skipped_from <= skipped_to:
            key = f"ofc8check:{grant.id}:{skipped_to.isoformat()}"
            request_hash = a.digest(dict(grant_id=grant.id, skipped_from=skipped_from.isoformat(), skipped_to=skipped_to.isoformat()))
            if a.replay(db, principal, key, request_hash) is None:
                a.record(db, principal, row, operation="checkpoint", request_key=key, request_hash=request_hash, now=now,
                    references=dict(template_digest=grant.template_digest, decision_code="NO_HISTORICAL_CATCHUP",
                        skipped_from=skipped_from, skipped_to=skipped_to, skipped_days=(skipped_to-skipped_from).days+1))
        for slot in slots:
            if slot.local_date < row.start_date or slot.decision in ("not_scheduled", "future"):
                continue
            if slot.scheduled_at is not None and slot.scheduled_at < grant.valid_from:
                continue
            if slot.decision == "gap_skipped":
                local_now = now.astimezone(zone)
                if (slot.local_date < grant.valid_from.astimezone(zone).date()
                        or slot.local_date > local_now.date()
                        or (slot.local_date == local_now.date() and local_now.strftime("%H:%M") < rule.local_time)):
                    continue
            existing = db.scalar(select(OfficeScheduleOccurrence.id).where(*a.scope(OfficeScheduleOccurrence, principal),
                OfficeScheduleOccurrence.schedule_id == row.id, OfficeScheduleOccurrence.local_date == slot.local_date))
            if existing:
                continue
            used, reserved = a.usage(db, principal, grant)
            code = "GAP_SKIPPED" if slot.decision == "gap_skipped" else "WINDOW_EXPIRED" if slot.decision == "expired" else "QUEUED"
            if code == "QUEUED" and used + reserved >= grant.max_occurrences:
                code = "QUOTA_EXHAUSTED"
            occurrence = OfficeScheduleOccurrence(id=str(uuid4()), organization_id=principal.organization_id,
                store_id=principal.store_id, schedule_id=row.id, schedule_version=row.version, grant_id=grant.id,
                local_date=slot.local_date, state="queued" if code == "QUEUED" else "skipped", decision_code=code,
                version=1, scheduled_at=slot.scheduled_at, window_end=slot.window_end,
                task_id=None, source_digest=None, created_at=now, updated_at=now)
            db.add(occurrence)
            db.flush()
            if code == "QUEUED":
                enqueue(db, organization_id=principal.organization_id, store_id=principal.store_id,
                    event_type=a.EVENT_PREFIX+principal.machine_id, idempotency_key="office-schedule:"+occurrence.id,
                    payload=dict(occurrence_id=occurrence.id, schedule_version=occurrence.schedule_version,
                                 template_digest=grant.template_digest), now=now)
                counts["queued"] += 1
            else:
                a.record(db, principal, row, operation="skip", request_key="ofc8slot:"+occurrence.id,
                    request_hash=a.digest(dict(occurrence_id=occurrence.id, template_digest=grant.template_digest)),
                    now=now, grant=grant, occurrence=occurrence,
                    references=dict(template_digest=grant.template_digest, decision_code=code, local_date=slot.local_date,
                                    result_schedule_version=row.version))
                counts["skipped"] += 1
        row.last_checked_at = now
        db.flush()
        # Planning also has a final current authorization boundary; rollback all
        # new reservations if a credential or finite grant expired mid-tick.
        from backend.app.modules.identity.machine_service import authenticate_machine_token
        fresh = authenticate_machine_token(db, machine_token, now=a.clock(db))
        a.validate_grant(db, fresh, row, grant, a.clock(db))
    return counts


def run_schedule_once(*, session_factory, machine_token, enabled=False, worker_id="office-schedule", lease_seconds=30):
    if type(enabled) is not bool:
        a.fail("input_invalid")
    if not enabled:
        return "disabled"
    from backend.app.workers.runner import run_once
    from backend.app.modules.tasks.service import create_scheduled_suggestion
    with session_factory() as db:
        principal, _ = a.machine_guard(db, machine_token)
        event_type = a.EVENT_PREFIX + principal.machine_id
        db.commit()
    def handler(message):
        with session_factory() as db:
            current, _ = a.machine_guard(db, machine_token)
            if (message.organization_id, message.store_id, message.event_type) != (
                    current.organization_id, current.store_id, a.EVENT_PREFIX+current.machine_id):
                a.fail("outbox_binding_invalid")
            payload = message.payload
            if type(payload) is not dict or set(payload) != {"occurrence_id", "schedule_version", "template_digest"}:
                a.fail("outbox_binding_invalid")
            if type(payload["occurrence_id"]) is not str or type(payload["schedule_version"]) is not int:
                a.fail("outbox_binding_invalid")
            row, grant, occurrence = a.occurrence_context(db, current, payload["occurrence_id"])
            event = a.bound_outbox(db, current, occurrence, grant)
            if (message.id != event.id or message.idempotency_key != event.idempotency_key
                    or payload != event.payload):
                a.fail("outbox_binding_invalid")
            result = create_scheduled_suggestion(db, machine_token=machine_token, occurrence_id=occurrence.id)
            # This machine-only route has its own finite approval/slot policy;
            # ordinary Task command checks cannot replace it.
            db.flush()
            if not result.get("replayed") and result.get("task_id") is not None:
                from backend.app.modules.identity.machine_service import authenticate_machine_token, authorize_machine_action
                instant = a.clock(db)
                fresh = authenticate_machine_token(db, machine_token, now=instant)
                authorize_machine_action(db, fresh, organization_id=current.organization_id,
                    store_id=current.store_id, action=a.ACTION, now=instant)
                a.validate_slot(db, fresh, row, grant, occurrence, instant, consumed=True)
            db.commit()
            return result["receipt_id"]
    return run_once(session_factory=session_factory, handlers={event_type: handler}, worker_id=worker_id,
        lease_seconds=lease_seconds, event_types=(event_type,), organization_id=principal.organization_id,
        store_id=principal.store_id)
