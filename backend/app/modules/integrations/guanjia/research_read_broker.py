"""Unwired G2-B bounded read kernel, called only by the fixed Host exec worker.

No grant issuer, tool registration, authentication or business write exists here.
Prepared results are PRIVATE until the Host commits/rechecks current native/Work
authority. Never retry an ambiguous operation, even after process restart.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import os
import stat
import threading
import time

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool, SingletonThreadPool

from backend.app.modules.agents import research_catalog as catalog, research_contracts as c
from backend.app.modules.agents import research_probe_plan as plan, tool_registry as registry
from backend.app.modules.identity.service import authorize_scope
from . import research_read_contract as contract, research_read_transport as transport
from .live_call_ledger import _root_fd
from .live_structure import _decode

ReadDenied = contract.ReadDenied


@dataclass(frozen=True, repr=False)
class PreparedReadOutcome:
    operation_id: str
    contract_sha256: str
    proposal_digest: str
    context_digest: str
    projection: dict = field(repr=False)
    projection_sha256: str
    disclosure_ready: bool = False


def _identity(info):
    return info.st_dev, info.st_ino


class _Journal:
    """Permanent exclusive operation reservation. No reset/reopen-for-send API."""
    def __init__(self, root, operation_id, binding):
        c.fingerprint(operation_id); c.fingerprint(binding)
        self.root, self.operation_id = root, operation_id
        self.parent = self.fd = -1
        self.sequence = 0
        self.frozen = False
        self.pid = os.getpid()
        try:
            self.parent = _root_fd(root)
            self.parent_identity = _identity(os.fstat(self.parent))
            # EEXIST denies all re-entry, even empty/unknown/incomplete directories.
            os.mkdir(operation_id, 0o700, dir_fd=self.parent)
            os.fsync(self.parent)
            self.fd = os.open(operation_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent)
            self.own_identity = _identity(os.fstat(self.fd))
            self.append("reserved", page=0, binding=binding)
        except Exception:
            self.close()
            contract.deny()

    def verify(self):
        if self.frozen or self.pid != os.getpid() or self.fd < 0:
            contract.deny()
        new = _root_fd(self.root)
        try:
            own = os.stat(self.operation_id, dir_fd=new, follow_symlinks=False)
            if (_identity(os.fstat(new)) != self.parent_identity or not stat.S_ISDIR(own.st_mode)
                    or _identity(own) != self.own_identity or own.st_uid != os.geteuid()
                    or own.st_mode & 0o077 or _identity(os.fstat(self.fd)) != self.own_identity):
                contract.deny()
        finally:
            os.close(new)

    def write(self, name, data):
        fd = None
        try:
            self.verify()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                contract.deny()
            remaining = data
            while remaining:
                n = os.write(fd, remaining)
                if n <= 0:
                    raise OSError()
                remaining = remaining[n:]
            os.fsync(fd)
            os.fsync(self.fd)
            named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (_identity(named) != _identity(info) or named.st_nlink != 1
                    or named.st_size != len(data) or named.st_mode & 0o077):
                contract.deny()
            self.verify()
            return sha256(data).hexdigest()
        except Exception:
            self.frozen = True
            contract.deny()
        finally:
            if fd is not None:
                os.close(fd)

    def append(self, state, *, page, **metadata):
        event = {"state": state, "page": page, "observed_at": contract.utcnow().isoformat(), **metadata}
        self.write(f"{self.sequence:04d}.event.json", c.canonical(event))
        self.sequence += 1

    def close(self):
        for name in ("fd", "parent"):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)


def _process_gate(caller):
    from backend.app.modules.agents import research_process
    if (research_process._IN_WORKER is not True
            or threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
            or not isinstance(caller, Session) or caller.in_transaction()
            or caller.new or caller.dirty or caller.deleted):
        contract.deny()
    engine = caller.get_bind()
    if not isinstance(engine, Engine) or isinstance(engine.pool, (StaticPool, SingletonThreadPool)):
        contract.deny()
    return engine


def _business_authorize(caller, requester, agent, approved):
    engine = _process_gate(caller)
    # This connection lives ONLY in the dedicated exec child, not threaded Host.
    with Session(engine, autoflush=False, expire_on_commit=False) as fresh, fresh.no_autoflush:
        registry._person_scope(fresh, requester)
        registry._agent_scope(fresh, requester, agent, contract.AGENT_PERMISSION)
        for permission in contract.permissions(approved["operation"]):
            authorize_scope(fresh, requester, organization_id=approved["organization_id"],
                            store_id=approved["store_id"], permission=permission)


def _gate(caller, requester, agent, config, proposal, approved_contract, frozen,
          credentials, host_gate, context_digest, phase, page):
    _process_gate(caller)
    c.fingerprint(context_digest)
    # Host callback MUST independently check machine/Work/Run/native/claim/lease/
    # cancellation/source in a fresh guard, and close its transaction before return.
    if not callable(host_gate) or host_gate(phase, page) != context_digest:
        contract.deny()
    current = approved_contract.load()
    if current != frozen:
        contract.deny()
    if credentials is not None:
        credentials.validate(current)
    fresh_proposal = plan.revalidate(caller, requester, agent, config, proposal)
    contract.validate_binding(current, fresh_proposal, config, requester, agent)
    _business_authorize(caller, requester, agent, current)
    if approved_contract.load() != frozen or host_gate(phase + "_checked", page) != context_digest:
        contract.deny()
    _process_gate(caller)


def _reject_reflected_secret(raw, token):
    """Do not persist even a private raw response that reflects authentication."""
    value = _decode(raw)
    pending = [value]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            pending.extend(node.keys()); pending.extend(node.values())
        elif type(node) is list:
            pending.extend(node)
        elif type(node) is str and token in node:
            contract.deny()


def execute(caller, requester, agent, corpus_config, proposal, *,
            approved_contract, credentials, ledger_root, operation_id,
            host_gate, context_digest):
    """Prepare bounded lexical evidence, never a disclosure-ready answer.

    All kwargs are trusted Host configuration except the already revalidated G1
    proposal. operation_id is a Host-derived native operation identity, NEVER a
    model retry nonce. Unknown outcome means reconcile this ID; do not execute
    with a replacement ID. No transport dependency injection in this API.
    """
    journal = None
    page = 0
    try:
        _process_gate(caller)
        if type(approved_contract) is not contract.ApprovedReadContract or type(credentials) is not transport.Credentials:
            contract.deny()
        c.fingerprint(operation_id); c.fingerprint(context_digest)
        frozen = approved_contract.load()
        def gate(phase):
            _gate(caller, requester, agent, corpus_config, proposal, approved_contract,
                  frozen, credentials, host_gate, context_digest, phase, page)
        gate("admission")
        parameters = proposal["normalized_parameters"]
        start = parameters["skipCount"]
        requested = min(parameters["maxResultCount"], frozen["page_size"], frozen["max_rows"])
        contract.request_body(frozen["operation"], parameters, frozen["scope_ids"], start, requested)
        binding = c.digest({"context": context_digest, "contract": approved_contract.manifest_sha256,
                            "proposal": proposal["proposal_digest"], "operation": operation_id})
        journal = _Journal(ledger_root, operation_id, binding)
        deadline = time.monotonic() + frozen["timeout_seconds"]
        offset, first_total, seen_fingerprints = start, None, set()
        all_items, pages, reasons = [], [], {"snapshot_consistency_unverified", "row_identity_unverified"}
        terminal = False
        while page < frozen["max_pages"] and len(all_items) < frozen["max_rows"]:
            page += 1
            size = min(requested, frozen["max_rows"] - len(all_items))
            body = contract.request_body(frozen["operation"], parameters, frozen["scope_ids"], offset, size)
            gate("before_intent")
            journal.append("intent", page=page, offset=offset, requested=size, request_sha256=c.digest(body))
            # Permanently consume before child release. Even failure after here
            # but before connect is not a new permission to resend.
            journal.append("entered", page=page)
            gate("before_send")
            journal.verify()
            if time.monotonic() >= deadline:
                raise transport.TransportUnknown()
            receipt = transport.send(frozen, credentials, body, deadline=deadline)
            gate("after_response")
            if type(receipt) is not transport.RawReceipt or sha256(receipt.raw).hexdigest() != receipt.sha256:
                contract.deny()
            _reject_reflected_secret(receipt.raw, credentials.token)
            item = contract.project(frozen, receipt.raw, offset, size)
            gate("after_projection")
            raw_sha = journal.write(f"page-{page:03d}.raw", receipt.raw)
            page_projection = {"offset": offset, "requested": size, **item}
            projection_sha = journal.write(f"page-{page:03d}.projection.json", c.canonical(page_projection))
            journal.append("returned", page=page, raw_sha256=raw_sha, projection_sha256=projection_sha)
            pages.append({"page": page, "offset": offset, "requested": size,
                          "raw_item_count": item["raw_item_count"], "total_count": item["total_count"],
                          "raw_sha256": raw_sha, "projection_sha256": projection_sha})
            all_items.extend(item["items"])
            count, total = item["raw_item_count"], item["total_count"]
            offset += count  # raw positions already included, including partial/drift pages.
            if first_total is None:
                first_total = total
            elif total != first_total:
                reasons.add("total_changed")
                break
            if count and item["raw_rows_sha256"] in seen_fingerprints:
                reasons.add("repeated_page_or_legitimate_duplicate")
                break
            seen_fingerprints.add(item["raw_rows_sha256"])
            if count < size:
                reasons.add("server_short_page")
            if not count:
                terminal = True
                if offset < total:
                    reasons.add("early_empty")
                break
            if offset >= total:
                terminal = True
                if offset > total:
                    reasons.add("observed_count_exceeds_total")
                break
        if not terminal:
            reasons.add("bounded_before_verified_terminal")
        gate("final_projection")
        result = {"version": 1, "coverage": "bounded_partial", "complete": False,
                  "operation": frozen["operation"], "contract_id": frozen["contract_id"],
                  "start_offset": start, "next_offset": offset, "raw_item_count": len(all_items),
                  "observed_total": first_total, "terminal_observed": terminal,
                  "same_upstream_snapshot": False, "unknowns": sorted(reasons),
                  "pages": pages, "items": all_items}
        encoded = c.canonical(result)
        if len(encoded) > 262144:
            contract.deny()
        projection_sha = journal.write("prepared.json", encoded)
        journal.append("prepared", page=page, projection_sha256=projection_sha)
        gate("prepared_return")
        return PreparedReadOutcome(operation_id, approved_contract.manifest_sha256,
            proposal["proposal_digest"], context_digest, result, projection_sha)
    except BaseException:
        if journal is not None and not journal.frozen:
            try:
                journal.append("unknown_no_replay", page=page)
            except Exception:
                pass
        # No raw exception, body, token, path or arbitrary upstream diagnostic.
        raise ReadDenied() from None
    finally:
        if journal is not None:
            journal.close()


def _read_private(directory, name, maximum):
    """One stable owned regular file, never symlinks, hard links or partial reads."""
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                or before.st_mode & 0o077 or not 0 < before.st_size <= maximum):
            contract.deny()
        chunks, count = [], 0
        while block := os.read(fd, min(65536, maximum + 1)):
            count += len(block)
            if count > maximum:
                contract.deny()
            chunks.append(block)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (catalog._identity_stat(before) != catalog._identity_stat(after)
                or catalog._identity_stat(after) != catalog._identity_stat(named)):
            contract.deny()
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)


def recover_prepared(caller, requester, agent, corpus_config, proposal, *,
                     approved_contract, ledger_root, operation_id,
                     host_gate, context_digest):
    """Read ONLY a fully prepared original operation, with fresh disclosure gates.

    No credentials, socket, write, renewed nonce or send permission. An incomplete
    page, UNKNOWN terminal state, changed pin or any raw/projection SHA mismatch
    is denied, not retried. Host must also reconcile its original native commit.
    """
    parent = directory = -1
    page = 0
    try:
        _process_gate(caller)
        if type(approved_contract) is not contract.ApprovedReadContract:
            contract.deny()
        c.fingerprint(operation_id); c.fingerprint(context_digest)
        frozen = approved_contract.load()
        def gate(phase):
            _gate(caller, requester, agent, corpus_config, proposal, approved_contract,
                  frozen, None, host_gate, context_digest, phase, page)
        gate("recovery_admission")
        parent = _root_fd(ledger_root)
        parent_identity = _identity(os.fstat(parent))
        directory = os.open(operation_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        before = os.fstat(directory)
        if before.st_uid != os.geteuid() or before.st_mode & 0o077:
            contract.deny()
        reads = {}
        def read(name, maximum):
            data = _read_private(directory, name, maximum)
            reads[name] = (maximum, sha256(data).hexdigest())
            return data
        names = sorted(os.listdir(directory))
        event_names = [name for name in names if name.endswith(".event.json")]
        if not 5 <= len(event_names) <= 32 or event_names != [f"{i:04d}.event.json" for i in range(len(event_names))]:
            contract.deny()
        events = [c.decode(read(name, 4096), 4096) for name in event_names]
        binding = c.digest({"context": context_digest, "contract": approved_contract.manifest_sha256,
                            "proposal": proposal["proposal_digest"], "operation": operation_id})
        if events[0].get("state") != "reserved" or events[0].get("binding") != binding or events[-1].get("state") != "prepared":
            contract.deny()
        middle = events[1:-1]
        if len(middle) % 3:
            contract.deny()
        page_count = len(middle) // 3
        if not 1 <= page_count <= frozen["max_pages"]:
            contract.deny()
        expected_files = set(event_names) | {"prepared.json"}
        pages, all_items = [], []
        offset = proposal["normalized_parameters"]["skipCount"]
        for n in range(page_count):
            page = n + 1
            gate("recovery_page")
            intent, entered, returned = middle[n*3:n*3+3]
            if ([item.get("state") for item in (intent, entered, returned)] != ["intent","entered","returned"]
                    or any(type(item.get("page")) is not int or item["page"] != page for item in (intent, entered, returned))
                    or intent.get("offset") != offset):
                contract.deny()
            requested = contract.integer(intent["requested"], 1, min(frozen["page_size"], 100))
            body = contract.request_body(frozen["operation"], proposal["normalized_parameters"],
                                          frozen["scope_ids"], offset, requested)
            if intent.get("request_sha256") != c.digest(body):
                contract.deny()
            raw_name = f"page-{page:03d}.raw"
            projected_name = f"page-{page:03d}.projection.json"
            expected_files.update((raw_name, projected_name))
            raw = read(raw_name, frozen["max_response_bytes"])
            projected = read(projected_name, 262144)
            if sha256(raw).hexdigest() != returned.get("raw_sha256") or sha256(projected).hexdigest() != returned.get("projection_sha256"):
                contract.deny()
            item = contract.project(frozen, raw, offset, requested)
            if c.canonical({"offset": offset, "requested": requested, **item}) != projected:
                contract.deny()
            pages.append({"page": page, "offset": offset, "requested": requested,
                          "raw_item_count": item["raw_item_count"], "total_count": item["total_count"],
                          "raw_sha256": returned["raw_sha256"], "projection_sha256": returned["projection_sha256"]})
            all_items.extend(item["items"])
            offset += item["raw_item_count"]
        if set(names) != expected_files or len(all_items) > frozen["max_rows"]:
            contract.deny()
        encoded = read("prepared.json", 262144)
        projection_sha = sha256(encoded).hexdigest()
        if projection_sha != events[-1].get("projection_sha256"):
            contract.deny()
        result = c.decode(encoded, 262144)
        c.exact(result, ("version","coverage","complete","operation","contract_id","start_offset",
            "next_offset","raw_item_count","observed_total","terminal_observed","same_upstream_snapshot",
            "unknowns","pages","items"))
        if (type(result["version"]) is not int or result["version"] != 1 or result["coverage"] != "bounded_partial"
                or result["complete"] is not False or result["same_upstream_snapshot"] is not False
                or result["operation"] != frozen["operation"] or result["contract_id"] != frozen["contract_id"]
                or result["start_offset"] != proposal["normalized_parameters"]["skipCount"]
                or result["next_offset"] != offset or result["raw_item_count"] != len(all_items)
                or result["pages"] != pages or result["items"] != all_items
                or result["observed_total"] != pages[0]["total_count"]
                or type(result["terminal_observed"]) is not bool):
            contract.deny()
        known = {"snapshot_consistency_unverified", "row_identity_unverified", "total_changed",
                 "repeated_page_or_legitimate_duplicate", "server_short_page", "early_empty",
                 "observed_count_exceeds_total", "bounded_before_verified_terminal"}
        if (type(result["unknowns"]) is not list or not set(result["unknowns"]) <= known
                or not {"snapshot_consistency_unverified","row_identity_unverified"} <= set(result["unknowns"])):
            contract.deny()
        gate("recovery_return")
        for name, (maximum, expected_sha) in reads.items():
            if sha256(_read_private(directory, name, maximum)).hexdigest() != expected_sha:
                contract.deny()
        current_parent = _root_fd(ledger_root)
        try:
            named = os.stat(operation_id, dir_fd=current_parent, follow_symlinks=False)
            if (_identity(os.fstat(current_parent)) != parent_identity
                    or catalog._identity_stat(before) != catalog._identity_stat(os.fstat(directory))
                    or catalog._identity_stat(before) != catalog._identity_stat(named)
                    or sorted(os.listdir(directory)) != names):
                contract.deny()
        finally:
            os.close(current_parent)
        return PreparedReadOutcome(operation_id, approved_contract.manifest_sha256,
            proposal["proposal_digest"], context_digest, result, projection_sha)
    except Exception:
        raise ReadDenied() from None
    finally:
        for fd in (directory, parent):
            if fd >= 0:
                os.close(fd)
