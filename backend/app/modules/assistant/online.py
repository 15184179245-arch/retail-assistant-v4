"""Host-owned admission policy for new work, independent of a fixed demo prompt.

This binds an existing authenticated work item to the existing bounded Go
protocol. It grants no identity, creates no budget and never sends a request.
The worker's durable launch intent still owns exactly-once dispatch.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path

from backend.app.modules.agents.pi_online_campaign import PiOnlineCampaign
from backend.app.modules.agents.pi_probe_artifacts import _directory, validate_directory
from backend.app.modules.agents.runtime_http import PiHttpConfig, PiRequest, validate_pi_config


@dataclass(frozen=True)
class AssistantOnlinePolicy:
    organization_id: str
    store_id: str
    requester_id: str
    machine_id: str
    manifest_sha256: str
    price_snapshot_sha256: str
    expires_at: datetime
    artifact_root: Path
    discovery_enabled: bool = False
    max_attempts: int = 3
    run_seconds: int = 300
    max_output_tokens: int = 2048
    source_enabled: bool = False

    def for_work(self, config: PiHttpConfig, actor, payload: PiRequest) -> PiHttpConfig:
        """Return a fresh run binding; all authorization is rechecked by Host."""
        if type(self) is not AssistantOnlinePolicy or type(payload) is not PiRequest:
            raise ValueError('assistant_online_policy_invalid')
        validate_pi_config(config, actor)
        # Dynamic admission is explicit and cannot replace a diagnostic permit.
        if (config.campaign is not None
                or (actor.organization_id, actor.store_id, actor.subject_id) !=
                   (self.organization_id, self.store_id, self.requester_id)
                or config.launch.expected_manifest_sha256 != self.manifest_sha256
                or type(self.expires_at) is not datetime
                or self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() != timedelta(0)
                or type(self.artifact_root) is not type(Path())
                or not self.artifact_root.is_absolute()):
            raise ValueError('assistant_online_policy_invalid')
        if (type(self.source_enabled) is not bool or self.source_enabled and not self.discovery_enabled
                or type(self.discovery_enabled) is not bool
                or type(self.run_seconds) is not int
                or not 1 <= self.run_seconds <= (900 if self.discovery_enabled else 300)
                or not self.discovery_enabled and (self.max_attempts, self.max_output_tokens) != (3, 2048)):
            raise ValueError('assistant_online_policy_invalid')
        now = datetime.now(timezone.utc)
        if now >= self.expires_at:
            raise ValueError('assistant_online_policy_expired')
        # Revalidate even a Pydantic object created via model_construct(). The
        # sole directory component is a canonical UUID, never request content.
        PiRequest.model_validate(payload.model_dump())
        if '\x00' in payload.text:
            raise ValueError('assistant_online_request_invalid')
        directory = self.artifact_root / payload.run_id
        campaign = PiOnlineCampaign(
            self.organization_id, self.store_id, self.requester_id, self.machine_id,
            payload.run_id, sha256(payload.text.encode()).hexdigest(), self.manifest_sha256,
            self.price_snapshot_sha256, min(self.expires_at, now + timedelta(seconds=self.run_seconds)),
            enabled=True, max_attempts=self.max_attempts, max_output_tokens=self.max_output_tokens,
            discovery_enabled=self.discovery_enabled, source_enabled=self.source_enabled,
            max_estimated_usd='unlimited',
            max_input_reservation=24576, max_request_body_bytes=131072, mode='real-assistant-trial',
            artifact_directory=str(directory))
        # Includes strict limits, fixed HTTPS/Go credential source and unbound
        # transport checks. It does not read the key or make a network call.
        campaign.check_transport(config.offline_transport)
        parent = _directory(str(self.artifact_root))
        try:
            try:
                os.mkdir(payload.run_id, mode=0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                # Multiple preparers are harmless; only one durable launcher
                # may win. Existing response artifacts are never discarded.
                pass
            validate_directory(str(directory))
            if any(directory.iterdir()):
                raise ValueError('assistant_online_artifacts_exist')
        finally:
            os.close(parent)
        return replace(config, campaign=campaign)
