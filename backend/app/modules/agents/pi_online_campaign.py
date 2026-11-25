"""Host-only limits for synthetic, probe, readonly and shared-memory trials.

The final HTTP body's byte limit is a size preflight, NOT a token bound. No
tokenizer/Go equivalence evidence is present in this contract.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import http.client
import json
from pathlib import Path
import re


class CampaignError(RuntimeError):
    # Campaign checks happen before transport entry; an intent alone is not a send.
    send_started = False


def reject():
    raise CampaignError('pi_campaign_held')


def utc_now():
    return datetime.now(timezone.utc)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def amount(value):
    try:
        if type(value) is not str or not re.fullmatch(r'(?:0|[1-9][0-9]{0,20})(?:\.[0-9]{1,18})?', value):
            reject()
        result = Decimal(value)
        if not result.is_finite() or result < 0:
            reject()
        return result
    except (InvalidOperation, TypeError):
        reject()


@dataclass(frozen=True)
class PiOnlineCampaign:
    organization_id: str
    store_id: str
    requester_id: str
    machine_id: str
    run_id: str
    sample_sha256: str
    manifest_sha256: str
    price_snapshot_sha256: str
    expires_at: datetime
    enabled: bool = False
    max_attempts: int = 2
    max_input_reservation: int = 4096
    max_output_tokens: int = 256
    max_request_body_bytes: int = 16384
    max_estimated_usd: str = '0.05'
    mode: str = 'synthetic-only-v1'
    artifact_directory: str | None = None
    discovery_enabled: bool = False
    source_enabled: bool = False

    def validate(self):
        if type(self) is not PiOnlineCampaign or self.enabled is not True:
            reject()
        if type(self.mode) is not str or self.mode not in ('synthetic-only-v1', 'real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial'):
            reject()
        if (type(self.discovery_enabled) is not bool
                or self.discovery_enabled and self.mode != 'real-assistant-trial'
                or type(self.source_enabled) is not bool or self.source_enabled and not self.discovery_enabled):
            reject()
        if self.mode == 'synthetic-only-v1' and self.artifact_directory is not None:
            reject()
        for key in ('organization_id', 'store_id', 'requester_id', 'machine_id', 'run_id'):
            value = getattr(self, key)
            if type(value) is not str or not 1 <= len(value) <= 200 or not all('!' <= c <= '~' for c in value):
                reject()
        for key in ('sample_sha256', 'manifest_sha256', 'price_snapshot_sha256'):
            if type(getattr(self, key)) is not str or not re.fullmatch('[0-9a-f]{64}', getattr(self, key)):
                reject()
        if (type(self.expires_at) is not datetime or self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() != timezone.utc.utcoffset(None)
                or utc_now() >= self.expires_at):
            reject()
        for key, ceiling in (('max_attempts', 32 if self.discovery_enabled else 3 if self.mode in ('real-memory-trial', 'real-assistant-trial') else 2), ('max_input_reservation', 24576 if self.mode == 'real-assistant-trial' else 4096),
                             ('max_output_tokens', 8192 if self.discovery_enabled else 2048 if self.mode in ('real-readonly-trial', 'real-memory-trial', 'real-assistant-trial') else 256),
                             ('max_request_body_bytes', 131072)):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= ceiling:
                reject()
        if self.mode == 'real-assistant-trial' and self.max_estimated_usd == 'unlimited':
            pass  # Monetary policy is enforced by the scoped cost ledger.
        elif (amount(self.max_estimated_usd) <= 0
                or self.mode != 'real-assistant-trial' and amount(self.max_estimated_usd) > Decimal('0.05')):
            reject()
        if self.mode in ('real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial'):
            if ((self.mode == 'real-probe' and self.max_attempts != 1)
                    or self.max_request_body_bytes > (131072 if self.mode == 'real-assistant-trial' else 8192 if self.mode == 'real-probe' else 32768 if self.mode == 'real-memory-trial' else 16384)
                    or self.expires_at - utc_now() > timedelta(minutes=15 if self.discovery_enabled else 5)
                    or type(self.artifact_directory) is not str
                    or not Path(self.artifact_directory).is_absolute()
                    or '..' in Path(self.artifact_directory).parts):
                reject()

    def snapshot(self):
        self.validate()
        result = asdict(self)
        result['expires_at'] = self.expires_at.isoformat()
        if not self.source_enabled:
            result.pop('source_enabled')
        if not self.discovery_enabled:
            result.pop('discovery_enabled')  # Preserve historical campaign hashes.
        if self.mode == 'synthetic-only-v1':
            # Preserve the previously frozen H4a/H4e snapshot shape.
            result.pop('artifact_directory')
        result.update(input_bound_verified=False)
        return result

    def check_transport(self, transport, *, bound=False):
        from backend.app.modules.agents.pi_online_transport import PiOnlineTransport, HostFileCredentialSource
        self.validate()
        if (type(transport) is not PiOnlineTransport
                or (transport._run_scope is None if bound else transport._run_scope is not None)):
            reject()
        if self.mode in ('real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial'):
            if (transport._connection_factory is not http.client.HTTPSConnection
                    or type(transport._credential_resolver) is not HostFileCredentialSource):
                reject()
        elif transport._connection_factory is http.client.HTTPSConnection:
            reject()

    def check_submission(self, actor, text, run_id, record, transport):
        self.validate()
        self.check_transport(transport)
        self.check_profile(record)
        if ((actor.organization_id, actor.store_id, actor.subject_id, run_id)
                != (self.organization_id, self.store_id, self.requester_id, self.run_id)
                or type(text) is not str or sha256(text.encode()).hexdigest() != self.sample_sha256
                or record['contract_version'] != 2
                or record['source_manifest_sha256'] != self.manifest_sha256):
            reject()

    def check_context(self, ctx, record):
        self.validate()
        self.check_profile(record)
        run = ctx.run
        if ((run.organization_id, run.store_id, run.requester_id, run.id, ctx.principal.machine_id)
                != (self.organization_id, self.store_id, self.requester_id, self.run_id, self.machine_id)
                or run.provider_key != 'opencode-go' or run.model != 'deepseek-v4.1-flash'
                or record['source_manifest_sha256'] != self.manifest_sha256
                or record['contract_version'] != 2):
            reject()

    def check_profile(self, record):
        """A real readonly approval cannot silently authorize a memory profile."""
        if self.mode == 'synthetic-only-v1':
            return
        family = 'source' if self.source_enabled else 'discovery' if self.discovery_enabled else 'memory' if self.mode in ('real-memory-trial', 'real-assistant-trial') else 'readonly'
        if (type(record) is not dict or record.get('contract_version') != 2
                or record.get('wire_protocol') != 'v4.pi-' + family + '/2'
                or record.get('launch_profile') not in (
                    'host-pi-' + family + '-node24-darwin-online-v1',
                    'host-pi-' + family + '-node24-linux-online-v1')):
            reject()

    def check_facts(self, facts, *, admission=False):
        """Count ALL persisted attempts, including failed/unknown and old epochs."""
        self.validate()
        if len(facts) > self.max_attempts or (admission and len(facts) >= self.max_attempts):
            reject()
        total = Decimal(0)
        for row, ledger in facts:
            if (digest(ledger.price_snapshot) != self.price_snapshot_sha256
                    or ledger.currency != 'USD' or ledger.provider_key != 'opencode-go'
                    or ledger.model != 'deepseek-v4.1-flash'
                    or ledger.requester_id != self.requester_id):
                reject()
            if ((row.state == 'permitted' and ledger.status != 'reserved')
                    or row.state not in ('permitted', 'responded')
                    or (row.state == 'responded' and ledger.status != 'settled')):
                reject()
            reserved = amount(ledger.reserved_amount)
            estimated = amount(ledger.estimated_amount) if ledger.estimated_amount is not None else reserved
            total += max(reserved, estimated)
            if admission and (row.state != 'responded' or ledger.status != 'settled'):
                reject()
        if self.max_estimated_usd != 'unlimited' and total > amount(self.max_estimated_usd):
            reject()

    def check_body(self, body):
        self.validate()
        raw = body.encode('utf-8')
        if not raw or len(raw) > self.max_request_body_bytes:
            reject()
        # Host calls this only after strict duplicate-key/non-finite decoding.
        parsed = json.loads(body)
        output = parsed.get('max_tokens', parsed.get('max_completion_tokens'))
        if type(output) is not int or not 1 <= output <= self.max_output_tokens:
            reject()
