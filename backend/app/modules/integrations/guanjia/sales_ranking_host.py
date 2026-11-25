"""Unwired trusted Host primitive; no grants, model schema or disclosure API.

Caller supplies protected fixed-store material and current dual-authority checks.
Checks must raise on denial; they are not replaced by a grant in this module.
Two sequential complete reads only establish projection agreement, never an
atomic snapshot, net-sales semantics, exact date endpoints or an accurate Top20.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from .live_host import _authentication
from .live_once_transport import _OneShotHttpsTransport
from .sales_ranking_contract import assemble_candidate, parse_page, RankingCandidate
from .sales_ranking_journal import RankingJournal, encode
from .sales_ranking_transport import RankingTransport


class RankingReadDenied(Exception):
    def __init__(self):
        super().__init__('sales_ranking_read_denied')


@dataclass(frozen=True, repr=False)
class HostMaterial:
    store_id: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    tenancy_name: str = field(repr=False)
    tenant_id: int
    journal_root: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class ReportCandidateReceipt:
    candidate: RankingCandidate
    projection_sha256: str
    verification_sha256: str
    independent_reads_match: bool
    as_of_utc: str
    classification: str = 'source_report_candidate'
    atomic_snapshot_verified: bool = False
    business_semantics_verified: bool = False
    date_endpoint_semantics_verified: bool = False


def _projection(candidate):
    return sha256(encode({'store_id': candidate.store_id, 'total_count': candidate.total_count,
        'rows': [[r.product_id, r.standard_id, r.store_id, str(r.qty), r.assist_qty_display,
                  str(r.total), r.product_name, r.product_code, r.standard_name]
                 for r in candidate.rows]})).hexdigest()


def collect_report(material, *, operation_id, as_of, authority_check):
    """Protected in-process entry; no credentials are read from disk here."""
    return _collect_report(material, operation_id=operation_id, as_of=as_of,
                           authority_check=authority_check)


def _collect_report(material, *, operation_id, as_of, authority_check,
                    _transport_factory=None, _clock=None, _take=200):
    """Private anonymous-test seam. Never expose it as a tool/config parameter."""
    journal = None
    try:
        if type(material) is not HostMaterial or not callable(authority_check):
            raise RankingReadDenied()
        clock = _clock or (lambda: datetime.now(timezone.utc))
        # Validate all shapes before reserving or authenticating.
        auth_transport = _OneShotHttpsTransport('authenticate', username=material.username,
            password=material.password, tenancy_name=material.tenancy_name)
        # Validate tenant/header material with a dummy token without any send.
        validation = RankingTransport(token='validation-only', tenant_id=material.tenant_id,
            tenancy_name=material.tenancy_name, store_id=material.store_id, as_of=as_of,
            now=clock(), skip=0, take=_take)
        body = validation.descriptor()['body']
        capture_utc = as_of.astimezone(timezone.utc).isoformat()
        authority_check()
        # Consumption follows the actual date-only upstream query, not the
        # finer capture timestamp. Unknown AND successful same-day acquisitions
        # are conservatively consumed even when operation/capture seconds change.
        binding = {'store_id': material.store_id, 'tenant_id': material.tenant_id,
                   'tenancy_sha256': sha256(material.tenancy_name.encode()).hexdigest(),
                   'query': body}
        journal = RankingJournal(material.journal_root, operation_id, binding,
                                 capture_utc=capture_utc, enforce_family=True)

        def send(key, descriptor, transport, authentication=False):
            journal.intent(key, descriptor)
            def gate():
                authority_check()
                journal.entered(key)  # durable conservative entry before bytes
                authority_check()  # fsync is not an authority lease
            try:
                if _transport_factory is None:
                    receipt = transport._send_after_host_gate(gate)
                    raw = receipt.raw
                else:
                    gate()
                    raw = _transport_factory(key, descriptor)
                journal.returned(key, raw, authentication=authentication)
                authority_check()
                return raw
            except BaseException:
                journal.unknown(key)
                raise

        candidates = []
        # Authentication is independent for each read; neither pass reuses a
        # page/response or silently refreshes an expired/failed authentication.
        for pass_index in range(2):
            if pass_index:
                auth_transport = _OneShotHttpsTransport('authenticate', username=material.username,
                    password=material.password, tenancy_name=material.tenancy_name)
            auth_raw = send(f'{pass_index}:authenticate', {'action': 'authenticate'},
                            auth_transport, authentication=True)
            token = _authentication(auth_raw, material.tenant_id)
            pages = []
            seen = set()
            previous_qty = None
            skip = 0
            while True:
                transport = RankingTransport(token=token, tenant_id=material.tenant_id,
                    tenancy_name=material.tenancy_name, store_id=material.store_id,
                    as_of=as_of, now=clock(), skip=skip, take=_take)
                descriptor = transport.descriptor()
                # Only the offset can vary within this frozen acquisition. The
                # descriptor is derived from the bytes this transport will send.
                if descriptor['body'] != dict(body, skipCount=skip):
                    raise RankingReadDenied()
                key = f'{pass_index}:page:{skip}'
                raw = send(key, descriptor, transport)
                page = parse_page(raw, expected_store=material.store_id, skip=skip, take=_take)
                journal.page(key, page)
                if pages and page.total_count != pages[0].total_count:
                    raise RankingReadDenied()
                for row in page.rows:
                    identity = (row.product_id, row.standard_id)
                    if identity in seen or previous_qty is not None and row.qty > previous_qty:
                        raise RankingReadDenied()
                    seen.add(identity)
                    previous_qty = row.qty
                pages.append(page)
                # Validate each prefix through the parser, and the complete
                # sequence through F1a before any candidate can escape.
                if sum(len(p.rows) for p in pages) == page.total_count:
                    break
                if not page.rows or len(pages) > 100_000:
                    raise RankingReadDenied()
                skip += len(page.rows)
            candidates.append(assemble_candidate(tuple(pages)))
            token = None
        first, second = map(_projection, candidates)
        journal.comparison(first, second)
        authority_check()
        # Mismatch is recorded but no candidate rows are disclosed.
        if first != second:
            raise RankingReadDenied()
        return ReportCandidateReceipt(candidates[0], first, second, True,
                                      capture_utc)
    except BaseException:
        raise RankingReadDenied() from None
    finally:
        if journal is not None:
            journal.close()
