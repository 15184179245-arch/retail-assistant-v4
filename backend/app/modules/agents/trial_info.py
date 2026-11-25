"""Authenticated, read-only description of the exact configured Pi campaign.

This is admission information, never execution evidence. Reading it performs no
release preparation, credential resolution, Node launch or model request.
"""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.app.modules.agents import service
from backend.app.modules.agents.pi_online_campaign import PiOnlineCampaign
from backend.app.modules.agents.runtime_http import _pi_config
from backend.app.modules.auth.service import validate_actor_session


class PiTrialInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str
    run_id: str
    sample_text: str
    sample_sha256: str
    expires_at: str
    start_available: bool


class PiTrialInfoEnvelope(BaseModel):
    success: bool
    data: PiTrialInfo
    code: str
    error: str | None
    user_message: str
    hint: str | None


def read_trial_info(request: Request, actor, session: Session) -> PiTrialInfo:
    try:
        validate_actor_session(session, actor)
        service._scope(session, actor)
        config = _pi_config(request, actor)
        campaign = config.campaign
        if (type(campaign) is not PiOnlineCampaign
                or set(vars(campaign)) != {field.name for field in fields(PiOnlineCampaign)}):
            raise service.AgentRuntimeError("pi_trial_configuration_unavailable", 503)
        # Reject another requester's config without exposing any of its values.
        if ((campaign.organization_id, campaign.store_id, campaign.requester_id) !=
                (actor.organization_id, actor.store_id, actor.subject_id)):
            raise service.AgentRuntimeError("pi_trial_scope_unavailable", 403)
        from backend.app.modules.agents.runtime_bootstrap import SAMPLE, SAMPLE_SHA256
        try:
            now = datetime.now(timezone.utc)
            expiry = campaign.expires_at
            if (type(expiry) is not datetime or expiry.tzinfo is None
                    or expiry.utcoffset() != timedelta(0)):
                raise ValueError("expiry")
            expired = expiry <= now
            # Preserve readback after expiry. Validate every other campaign
            # field with a temporary future deadline, without mutating Host state.
            checked = replace(campaign, expires_at=now + timedelta(seconds=1)) if expired else campaign
            checked.validate()
            checked.check_transport(config.offline_transport)
            if (str(UUID(campaign.run_id)) != campaign.run_id
                    or campaign.sample_sha256 != SAMPLE_SHA256
                    or sha256(SAMPLE.encode()).hexdigest() != SAMPLE_SHA256
                    or campaign.manifest_sha256 != config.launch.expected_manifest_sha256):
                raise ValueError("binding")
        except Exception:
            raise service.AgentRuntimeError("pi_trial_configuration_unavailable", 503) from None
        existing = service.get_run(session, actor.organization_id, actor.store_id, campaign.run_id)
        if existing is not None and existing.requester_id != actor.subject_id:
            raise service.AgentRuntimeError("pi_trial_scope_unavailable", 403)
        return PiTrialInfo(mode=campaign.mode, run_id=campaign.run_id,
            sample_text=SAMPLE, sample_sha256=SAMPLE_SHA256,
            expires_at=expiry.isoformat(),
            start_available=not expired and existing is None and campaign.dispatch_window_open())
    finally:
        session.rollback()
        session.close()
