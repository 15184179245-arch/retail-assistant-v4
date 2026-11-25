"""Host-pinned, read-only evidence corpus. No network, tool registration or publication.

The Host must review/sanitize the corpus and inject CorpusConfig. Neither a
document, model argument, hash nor knowledge.read creates that configuration.
Callers' transactions are untouched; authority is read in fresh transactions.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import base64
import os
from pathlib import Path
import stat

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool, SingletonThreadPool

from . import research_contracts as c
from . import tool_registry as registry
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.models import ScopeSecurityFence

ResearchError = c.ResearchError


@dataclass(frozen=True, repr=False)
class CorpusConfig:
    directory: str
    manifest_sha256: str
    organization_id: str
    store_id: str
    owner_id: str


def _now():
    return datetime.now(timezone.utc)


def _identity(actor, agent, fence):
    values = [actor.organization_id, actor.store_id, actor.subject_id,
              actor.account_id, actor.session_id, actor.account_version, agent.subject_id,
              actor.auth_expires_at.isoformat(), agent.auth_expires_at.isoformat(),
              getattr(actor, "work_id", None), getattr(actor, "run_id", None),
              getattr(actor, "binding_sha256", None),
              None if fence is None else fence.version,
              None if fence is None else fence.work_version]
    return c.digest(values)


def _authorize(caller, requester, agent, config):
    if (type(config) is not CorpusConfig or not isinstance(caller, Session)
            or caller.new or caller.dirty or caller.deleted):
        c.deny()
    engine = caller.get_bind()
    if (not isinstance(engine, Engine)
            or isinstance(engine.pool, (StaticPool, SingletonThreadPool))
            or (config.organization_id, config.store_id, config.owner_id) !=
               (requester.organization_id, requester.store_id, requester.subject_id)):
        c.deny()
    with Session(engine, autoflush=False, expire_on_commit=False) as fresh, fresh.no_autoflush:
        registry._person_scope(fresh, requester)
        registry._agent_scope(fresh, requester, agent, "agents.knowledge.search")
        authorize_scope(fresh, requester, organization_id=config.organization_id,
                        store_id=config.store_id, permission="knowledge.read")
        fence = fresh.get(ScopeSecurityFence, (config.organization_id, config.store_id))
        return _identity(requester, agent, fence)


def _open_root(directory):
    if (type(directory) is not str or not os.path.isabs(directory)
            or directory != os.path.normpath(directory) or "\x00" in directory):
        c.deny()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(directory).parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity_stat(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink)


def _read_exact(root, name, expected, maximum):
    c.fingerprint(expected)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022
                or not 0 < before.st_size <= maximum):
            c.deny()
        chunks, count = [], 0
        while block := os.read(fd, min(65536, maximum + 1)):
            count += len(block)
            if count > maximum:
                c.deny()
            chunks.append(block)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=root, follow_symlinks=False)
        if (_identity_stat(before) != _identity_stat(after)
                or _identity_stat(after) != _identity_stat(named)
                or sha256(raw).hexdigest() != expected):
            c.deny()
        return raw
    finally:
        os.close(fd)


def _load(config):
    c.fingerprint(config.manifest_sha256)
    root = _open_root(config.directory)
    try:
        raw = _read_exact(root, "manifest.json", config.manifest_sha256, c.MAX_MANIFEST)
        data = c.manifest(c.decode(raw))
        docs, size = {}, 0
        now = _now()
        for source in data["sources"]:
            if not c.instant(source["observed_at"]) <= now < c.instant(source["expires_at"]):
                c.deny()
            blob = _read_exact(root, source["content_sha256"] + ".txt",
                               source["content_sha256"], c.MAX_SOURCE)
            size += len(blob)
            if size > c.MAX_CORPUS:
                c.deny()
            content = blob.decode("utf-8")
            c.text(content, c.MAX_SOURCE)
            docs[source["artifact_id"]] = (source, content)
        for candidate in data["candidates"]:
            for ref in candidate["source_refs"]:
                if ref["end"] > len(docs[ref["artifact_id"]][1]):
                    c.deny()
        other = _open_root(config.directory)
        try:
            if _identity_stat(os.fstat(root)) != _identity_stat(os.fstat(other)):
                c.deny()
        finally:
            os.close(other)
        return data, docs
    finally:
        os.close(root)


@contextmanager
def access(caller, requester, agent, config):
    """Every entry, including negative lookups, checks before and after access."""
    try:
        with caller.no_autoflush:
            identity = _authorize(caller, requester, agent, config)
            data, docs = _load(config)
            yield identity, data, docs
            # A second complete pin read detects changed/replaced artifacts;
            # final fresh authorization catches logout/expiry/revocation in I/O.
            current, current_docs = _load(config)
            if current != data or current_docs != docs:
                c.deny()
            if _authorize(caller, requester, agent, config) != identity:
                c.deny()
    except Exception:
        c.deny()


def _cursor(binding, offset):
    return base64.urlsafe_b64encode(c.canonical({"binding": binding, "offset": offset})).decode("ascii")


def _offset(cursor, binding):
    if cursor is None:
        return 0
    c.text(cursor, 1024)
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        value = c.decode(raw, 512)
        c.exact(value, ("binding", "offset"))
        if value["binding"] != binding or _cursor(binding, value["offset"]) != cursor:
            c.deny()
        return c.integer(value["offset"], 0, 128)
    except (ValueError, UnicodeError):
        c.deny()


def search(caller, requester, agent, config, *, query="", limit=10, cursor=None):
    with access(caller, requester, agent, config) as (identity, data, docs):
        c.text(query, 300, empty=True)
        c.integer(limit, 1, 20)
        binding = c.digest([config.manifest_sha256, data["corpus_id"], data["revision"],
                            identity, query, limit])
        offset = _offset(cursor, binding)
        terms = query.casefold().split()
        matched = []
        for key in sorted(docs):
            source, content = docs[key]
            haystack = " ".join((source["title"], source["domain"], content)).casefold()
            if all(term in haystack for term in terms):
                matched.append(key)
        if offset > len(matched):
            c.deny()
        end = min(offset + limit, len(matched))
        items = []
        for key in matched[offset:end]:
            source, content = docs[key]
            items.append({"artifact_id": key, "title": source["title"], "domain": source["domain"],
                "excerpt": content[:512], "evidence": c.evidence(source, config.manifest_sha256, 0, min(512, len(content)))})
        result = {"version": 1, "authority": "reference_only",
            "corpus_digest": config.manifest_sha256, "corpus_id": data["corpus_id"],
            "revision": data["revision"], "items": items, "total_matches": len(matched),
            "offset": offset, "next_cursor": _cursor(binding, end) if end < len(matched) else None,
            "truncated": offset != 0 or end < len(matched),
            "coverage": "matched_page_in_this_corpus_only"}
    return result


def read(caller, requester, agent, config, *, artifact_id, corpus_digest, start=0, limit=1024):
    with access(caller, requester, agent, config) as (_, data, docs):
        c.identifier(artifact_id)
        c.fingerprint(corpus_digest)
        c.integer(start, 0, c.MAX_SOURCE)
        c.integer(limit, 1, 4096)
        if corpus_digest != config.manifest_sha256 or artifact_id not in docs:
            c.deny()
        source, content = docs[artifact_id]
        if start >= len(content):
            c.deny()
        end = min(start + limit, len(content))
        result = {"version": 1, "authority": "reference_only", "text": content[start:end],
            "evidence": c.evidence(source, config.manifest_sha256, start, end),
            "truncated": start != 0 or end < len(content), "next_start": end if end < len(content) else None,
            "candidate_ids": sorted(x["id"] for x in data["candidates"]
                if any(r["artifact_id"] == artifact_id for r in x["source_refs"]))}
    return result


def describe_candidate(caller, requester, agent, config, *, candidate_id, corpus_digest):
    """Read one exact inert candidate contract before proposing unknown work.

    This is a research description, not capability_catalog admission. In
    particular neither the read label nor required_permissions is a grant.
    """
    with access(caller, requester, agent, config) as (_, data, docs):
        c.identifier(candidate_id)
        c.fingerprint(corpus_digest)
        if corpus_digest != config.manifest_sha256:
            c.deny()
        candidate = next((x for x in data["candidates"] if x["id"] == candidate_id), None)
        if candidate is None:
            c.deny()
        result = {"version": 1, "authority": "reference_only",
            "corpus_digest": corpus_digest, "candidate_digest": c.digest(candidate),
            "candidate": candidate,
            "request_schema_digest": c.digest(candidate["request_schema"]),
            "result_schema_digest": c.digest(candidate["result_schema"]),
            "permissions_authority": "document_claim_only",
            "evidence": [c.evidence(docs[r["artifact_id"]][0], corpus_digest, r["start"], r["end"])
                         for r in candidate["source_refs"]]}
        # Detach mutable data and cap the entire response, not only each schema.
        result = c.decode(c.canonical(result), 65536)
    return result
