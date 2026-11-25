"""Host metadata for Pi's official deferred-tool discovery.

This module neither searches nor invokes tools. A frozen catalog is a metadata
pin, not authority to execute; callers still use the registered tool boundary.
No provider, filesystem catalog, dynamic handler or secret source is consulted.
"""
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool, SingletonThreadPool

from backend.app.modules.agents import tool_registry as registry
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.assistant.principal import WorkPrincipal


class CapabilityCatalogError(ValueError):
    pass


def _deny():
    raise CapabilityCatalogError('agent_capability_catalog_unavailable') from None


@dataclass(frozen=True, repr=False)
class CapabilityCatalog:
    payload_json: str
    digest: str


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def freeze_catalog(engine, requester, agent, *, run_id: str, now=None) -> CapabilityCatalog:
    """Create a new Run's catalog from current identities and current grants.

    Own a fresh transaction, so a caller's prior snapshot or identity map cannot
    retain a revoked grant. Shared-connection pools cannot provide this boundary.
    """
    try:
        if (not isinstance(engine, Engine)
                or isinstance(engine.pool, (StaticPool, SingletonThreadPool))
                or type(run_id) is not str or str(UUID(run_id)) != run_id):
            _deny()
        if isinstance(requester, WorkPrincipal) and requester.run_id != run_id:
            _deny()
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            registry._person_scope(session, requester, now=now)
            registry._agent_scope(session, requester, agent, None, now=now)
            registry._build_registry()
            descriptions = registry.model_tool_descriptions(session, requester, agent, now=now)
            entries = []
            for description in descriptions:
                definition = registry.resolve_tool(description['tool_key'], description['version'])
                try:
                    for permission in definition.extra_requester_permissions:
                        authorize_scope(session, requester, organization_id=requester.organization_id,
                            store_id=requester.store_id, permission=permission, now=now)
                except HTTPException as error:
                    if error.status_code == 403 and error.detail == 'permission_denied':
                        continue
                    raise
                entries.append({
                    'tool_key': definition.key, 'version': definition.version,
                    'description': definition.description, 'mode': definition.mode,
                    'confirmation': definition.confirmation, 'side_effect': definition.side_effect,
                    'input_schema': definition.input_model.model_json_schema(),
                    'output_schema': definition.output_model.model_json_schema(),
                    'contract': registry.toolset_manifest([description], store_id=requester.store_id)[0],
                })
            entries.sort(key=lambda entry: (entry['tool_key'], entry['version']))
            payload = _encode({'version': 1, 'run_id': run_id,
                'organization_id': requester.organization_id, 'store_id': requester.store_id,
                'requester_id': requester.subject_id, 'agent_id': agent.subject_id, 'tools': entries})
            payload.encode('utf-8')
            return CapabilityCatalog(payload, 'sha256:' + sha256(payload.encode()).hexdigest())
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, HTTPException, SQLAlchemyError):
        _deny()


def _verified(engine, requester, agent, snapshot, *, run_id, expected_digest, now):
    if (type(snapshot) is not CapabilityCatalog or type(snapshot.payload_json) is not str
            or len(snapshot.payload_json) > 1024 * 1024 or type(snapshot.digest) is not str
            or type(expected_digest) is not str or len(expected_digest) != 71):
        _deny()
    try:
        digest = 'sha256:' + sha256(snapshot.payload_json.encode('utf-8')).hexdigest()
        if not hmac.compare_digest(digest, expected_digest) or not hmac.compare_digest(digest, snapshot.digest):
            _deny()
        current = freeze_catalog(engine, requester, agent, run_id=run_id, now=now)
        if (not hmac.compare_digest(current.digest, expected_digest)
                or current.payload_json != snapshot.payload_json):
            _deny()
        return json.loads(current.payload_json)
    except (ValueError, TypeError, UnicodeError):
        _deny()


def catalog_page(engine, requester, agent, snapshot, *, run_id, expected_digest,
                 offset=0, limit=20, now=None) -> dict:
    """Paginate authorized summaries without sending every tool's long schema."""
    if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100):
        _deny()
    payload = _verified(engine, requester, agent, snapshot, run_id=run_id,
                        expected_digest=expected_digest, now=now)
    entries = payload['tools']
    if offset > len(entries):
        _deny()
    end = min(offset + limit, len(entries))
    return {'catalog_digest': expected_digest, 'total': len(entries),
        'next_offset': end if end < len(entries) else None,
        'items': [{key: entry[key] for key in (
            'tool_key', 'version', 'description', 'mode', 'confirmation', 'side_effect')}
            for entry in entries[offset:end]],
        'authority': 'metadata_only'}


def describe_capability(engine, requester, agent, snapshot, *, run_id, expected_digest,
                        tool_key, version, now=None) -> dict:
    """Return one exact pinned schema after revalidating the whole authorized pin."""
    if type(tool_key) is not str or type(version) is not str:
        _deny()
    payload = _verified(engine, requester, agent, snapshot, run_id=run_id,
                        expected_digest=expected_digest, now=now)
    for entry in payload['tools']:
        if (entry['tool_key'], entry['version']) == (tool_key, version):
            return {'catalog_digest': expected_digest, 'capability': entry,
                    'authority': 'metadata_only'}
    _deny()
