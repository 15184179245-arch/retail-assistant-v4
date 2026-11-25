"""AGT031 P1-A: strict manifest and approved public-source validation only.

No SDK loader, eval admission, resource body output, Provider, or runtime enablement.
Publication/revocation linearizability for future consumption is NOT implemented.
"""
from __future__ import annotations

import json
import re
from hashlib import sha256

from sqlalchemy import select
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge import access_policy
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision

KIND = "pi-skill-resource/1"
LEGACY = "legacy-digest"
PREFIX = "pi-resource."
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_SDK_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"contract", "organization_id", "store_id", "skill_key", "release_tag",
           "sdk_name", "description", "resources"}
_RESOURCE_FIELDS = {"logical_path", "knowledge_entry_id", "revision_id", "raw_utf8_bytes", "raw_sha256"}


class ResourceManifestError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code="skill_resource_manifest_invalid"):
    raise ResourceManifestError(code)


def _text(value, maximum, *, pattern=None):
    if type(value) is not str or not value or "\x00" in value:
        _fail()
    try:
        data = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail()
    if len(data) > maximum or not value.strip() or (pattern and not pattern.fullmatch(value)):
        _fail()
    return value


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            _fail("skill_resource_duplicate_key")
        out[key] = value
    return out


def canonical_manifest(value):
    """Accept an exact object or strict JSON text, never paths/YAML/coerced numbers."""
    if type(value) is str:
        _text(value, 16384)
        try:
            value = json.loads(value, object_pairs_hook=_pairs,
                               parse_constant=lambda _: _fail())
        except (ValueError, TypeError, RecursionError):
            _fail()
    if type(value) is not dict or set(value) != _FIELDS or value.get("contract") != KIND:
        _fail()
    for field in ("organization_id", "store_id"):
        _text(value[field], 36)
    key = _text(value["skill_key"], 120, pattern=_KEY)
    if not key.startswith(PREFIX) or not _KEY.fullmatch(key[len(PREFIX):]):
        _fail("skill_resource_namespace_invalid")
    _text(value["release_tag"], 120, pattern=_KEY)
    _text(value["sdk_name"], 64, pattern=_SDK_NAME)
    _text(value["description"], 512)
    if type(value["resources"]) is not list or len(value["resources"]) != 1:
        _fail()
    resource = value["resources"][0]
    if type(resource) is not dict or set(resource) != _RESOURCE_FIELDS or resource["logical_path"] != "SKILL.md":
        _fail()
    for field in ("knowledge_entry_id", "revision_id"):
        _text(resource[field], 36)
    if type(resource["raw_utf8_bytes"]) is not int or not 1 <= resource["raw_utf8_bytes"] <= 32768:
        _fail()
    _text(resource["raw_sha256"], 64, pattern=_SHA)
    try:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        data = canonical.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail()
    if len(data) > 16384:
        _fail()
    # Detach caller-owned dicts: later mutation cannot change approved identity.
    return json.loads(canonical), canonical, sha256(data).hexdigest()


def validate_public_source(session, actor, manifest, *, now):
    """Check a current approved source for DRAFT creation, not a send-intent fence.

    Full existing resolver semantics are reused. All ancestor policies must be
    established/public; privileged restricted access is deliberately not used.
    Caller must supply a clean Session before any command or source check.
    """
    if (manifest["organization_id"], manifest["store_id"]) != (actor.organization_id, actor.store_id):
        _fail("skill_resource_scope_denied")
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="knowledge.read", now=now)
    ref = manifest["resources"][0]
    entry = session.scalar(select(KnowledgeEntry).where(
        KnowledgeEntry.id == ref["knowledge_entry_id"],
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id).execution_options(populate_existing=True))
    revision = session.scalar(select(KnowledgeRevision).where(
        KnowledgeRevision.id == ref["revision_id"], KnowledgeRevision.entry_id == ref["knowledge_entry_id"],
        KnowledgeRevision.organization_id == actor.organization_id,
        KnowledgeRevision.store_id == actor.store_id).execution_options(populate_existing=True))
    if entry is None or revision is None:
        _fail("skill_resource_source_unavailable")
    observed = (entry.version, entry.current_revision_id)
    revisions, policies, facts, conflict = access_policy.inspect_entry(session, entry)
    policy = {p.revision_id: p for p in policies}
    ancestors = [r for r in revisions if r.version <= revision.version]
    if (conflict or not ancestors or any(r.id not in policy or
            policy[r.id].classification != "public" or facts.get(r.id) is None or
            facts[r.id]["classification"] != "public" for r in ancestors)
            or access_policy._content_class(entry.scope_ref) != "public"):
        _fail("skill_resource_source_unavailable")
    # can_read refreshes entry. Revalidate the full current pair AFTER that.
    if not access_policy.can_read(session, actor, entry, revision, now=now):
        _fail("skill_resource_source_unavailable")
    if ((entry.version, entry.current_revision_id) != observed or entry.current_revision_id != revision.id
            or entry.status != "approved" or entry.approved_by is None
            or (entry.expires_at is not None and entry.expires_at <= now)
            or (entry.organization_id, entry.store_id) != (actor.organization_id, actor.store_id)
            or access_policy._content_class(entry.scope_ref) != "public"):
        _fail("skill_resource_source_unavailable")
    # Re-evaluate after the refreshed pair; this is still only DRAFT validation,
    # not the unimplemented concurrent final consumption lock protocol.
    _, policies, facts, conflict = access_policy.inspect_entry(session, entry)
    if conflict or any(facts.get(r.id) is None or facts[r.id]["classification"] != "public"
                       for r in ancestors) or any(p.classification != "public" for p in policies):
        _fail("skill_resource_source_unavailable")
    try:
        raw = revision.body.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("skill_resource_source_bytes_invalid")
    if (not raw or len(raw) > 32768 or b"\x00" in raw or len(raw) != ref["raw_utf8_bytes"]
            or sha256(raw).hexdigest() != ref["raw_sha256"]):
        _fail("skill_resource_source_bytes_invalid")
    return entry, revision
