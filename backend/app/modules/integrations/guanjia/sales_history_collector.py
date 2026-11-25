"""Host-only daily posted-sales collector; no password loading or authentication.

Two independent traversals preserve every source row. An unknown dispatch is a
hold, never an excuse to issue a new acquisition. No automatic source retries.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha256

from .erp_sales_detail_contract import parse_page, assemble_candidate, ErpSalesDetailContractError
from .sales_history_archive import SalesHistoryArchive, _binding, _canonical, _encode, _json, _uuid
from .sales_history_transport import SalesHistoryTransport, history_body, _header_material


class SalesHistoryCollectionError(ValueError):
    """Finite diagnostic only; no materials, source values, or paths."""


def _deny(code='sales_history_collection_hold'):
    raise SalesHistoryCollectionError(code) from None


@dataclass(frozen=True, repr=False)
class ReadCredentials:
    token: str = field(repr=False)
    tenant_id: int
    tenancy_name: str = field(repr=False)
    shop_id: str = field(repr=False)
    expires_at: datetime

    def __repr__(self):
        return 'ReadCredentials(<protected>)'


_UNSET = object()


def collect_day(archive: SalesHistoryArchive, *, day: str, acquisition: str,
                credentials: ReadCredentials, authority_check, take=100, refresh=False, expected_latest=_UNSET,
                _clock=lambda: datetime.now(timezone.utc), _transport_factory=SalesHistoryTransport):
    """Collect or locally resume exactly one Host-assigned acquisition.

The Host persists batch authentication separately and supplies an expiry. A
returned page is retained before post-request authority checks; revoked callers
cannot publish or receive it. Test hooks are trusted Host inputs, not Agent args.
"""
    rejection = 'sales_history_collection_hold'

    def reject(code='sales_history_collection_hold'):
        # Preserve only our fixed diagnostic codes across the archive's generic
        # transaction wrapper; never propagate callback/transport error text.
        nonlocal rejection
        rejection = code
        _deny(code)

    try:
        if (type(archive) is not SalesHistoryArchive or type(credentials) is not ReadCredentials or
                type(take) is not int or not 1 <= take <= 100 or type(refresh) is not bool or
                type(day) is not str or date.fromisoformat(day).isoformat() != day or
                not callable(authority_check) or not callable(_clock)):
            reject()
        binding = _binding(archive.binding)
        binding_bytes = _encode(binding)

        def check():
            now = _clock()
            if (type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None or
                    type(credentials.expires_at) is not datetime or credentials.expires_at.tzinfo is None or
                    credentials.expires_at.utcoffset() is None or now >= credentials.expires_at or
                    type(credentials.tenant_id) is not int or
                    credentials.tenant_id != binding['tenant_id'] or credentials.shop_id != binding['shop_id'] or
                    not _header_material(credentials.token, 4096) or
                    not _header_material(credentials.tenancy_name, 4096) or
                    _encode(_binding(archive.binding)) != binding_bytes or authority_check() is not True):
                reject('sales_history_authority_rejected')
            archive._read_binding()
            archive._finish_reads()
            return now

        check()
        with archive.writer():
            check()
            latest = archive.read_latest(day)
            check()
            if latest is not None and latest['acquisition'] == acquisition:
                return {'status': 'already_complete', 'manifest': latest}
            observed_latest = None if latest is None else latest['acquisition']
            if expected_latest is not _UNSET:
                if expected_latest is not None:
                    _uuid(expected_latest)
                if observed_latest != expected_latest:
                    reject('sales_history_latest_changed')
            if latest is not None and not refresh:
                return {'status': 'already_complete', 'manifest': latest}
            expected_latest = observed_latest
            archive.reserve(day, acquisition)
            # A sealed-but-unpublished old observation must not replace a newer
            # published acquisition after a crash between manifest and pointer.
            archive._unsealed(acquisition)
            traversals = []
            for pass_number in (1, 2):
                pages, items, offset, total = [], [], 0, None
                while True:
                    now = check()
                    state = archive.operation_state(acquisition, pass_number=pass_number, skip=offset)
                    check()
                    if state['state'] in ('unknown', 'raw_unreceipted'):
                        reject('sales_history_dispatch_unknown')
                    expected = {'method': 'POST', 'action': 'erp_detail', 'body': history_body(
                        operation='erp_detail', store_id=binding['shop_id'], start=date.fromisoformat(day),
                        end=date.fromisoformat(day), now=now, skip=offset, take=take)}
                    if state['state'] == 'absent':
                        archive.intent(acquisition, pass_number=pass_number, skip=offset, take=take)
                        state = archive.operation_state(acquisition, pass_number=pass_number, skip=offset)
                    if _encode(state['intent']['descriptor']) != _encode(expected):
                        reject()
                    if state['state'] == 'intended':
                        check()
                        transport = _transport_factory(operation='erp_detail', token=credentials.token,
                            tenant_id=credentials.tenant_id, tenancy_name=credentials.tenancy_name,
                            store_id=binding['shop_id'], start=date.fromisoformat(day),
                            end=date.fromisoformat(day), now=now, skip=offset, take=take)
                        if _encode(transport.descriptor()) != _encode(expected):
                            reject()
                        def gate():
                            check()
                            archive.entered(acquisition, pass_number=pass_number, skip=offset,
                                            observed_at=_clock().isoformat())
                        response = transport._send_after_host_gate(gate)
                        # Source validation is pure and precedes raw IO. Do not
                        # misclassify a filesystem failure as a source failure.
                        valid = response.http_status == 200 and response.json_body is True
                        if valid:
                            try:
                                _json(response.raw, business=True)
                                parse_page(response.raw, expected_shop_id=binding['shop_id'],
                                           day=day, skip=offset, take=take)
                            except (ValueError, TypeError, RecursionError, OverflowError):
                                valid = False
                        if valid:
                            archive.returned(acquisition, pass_number=pass_number, skip=offset,
                                raw=response.raw, http_status=response.http_status,
                                observed_at=_clock().isoformat())
                        else:
                            archive.returned_failure(acquisition, pass_number=pass_number, skip=offset,
                                response_sha256=sha256(response.raw).hexdigest(), response_bytes=len(response.raw),
                                http_status=response.http_status, observed_at=_clock().isoformat())
                        state = archive.operation_state(acquisition, pass_number=pass_number, skip=offset)
                    # A known returned page remains resumable after regrant or
                    # credential renewal. A temporary authority loss is not a
                    # source failure and does not burn this acquisition.
                    check()
                    if state['state'] == 'returned_failure':
                        archive.abort_known(acquisition, reason='source_returned_failure')
                        reject('sales_history_source_rejected')
                    if state['receipt']['http_status'] != 200:
                        archive.abort_known(acquisition, reason='source_returned_failure')
                        reject('sales_history_source_rejected')
                    decoded = _json(state['raw'], business=True)
                    if decoded.get('success') is not True:
                        archive.abort_known(acquisition, reason='source_returned_failure')
                        reject('sales_history_source_rejected')
                    try:
                        page = parse_page(state['raw'], expected_shop_id=binding['shop_id'],
                                          day=day, skip=offset, take=take)
                    except ErpSalesDetailContractError:
                        archive.abort_known(acquisition, reason='schema_rejected')
                        reject('sales_history_source_rejected')
                    if total is None:
                        total = page.total_count
                    if page.total_count != total:
                        archive.abort_known(acquisition, reason='source_changed')
                        reject('sales_history_source_changed')
                    pages.append(page)
                    items.extend(decoded['result']['items'])
                    offset += len(page.rows)
                    check()
                    if offset == total:
                        break
                assemble_candidate(tuple(pages))
                traversals.append(Counter(_encode(_canonical(item)) for item in items))
            if traversals[0] != traversals[1]:
                archive.abort_known(acquisition, reason='source_changed')
                reject('sales_history_source_changed')
            check()
            manifest = archive.seal(acquisition, expected_latest=expected_latest)
            check()
            return {'status': 'collected', 'manifest': manifest}
    except Exception:
        _deny(rejection)
