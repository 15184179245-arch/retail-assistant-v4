"""Host-only repeatable local sales reads; no upstream access or aggregation.

Paths, binding and authority are Host configuration, never model arguments.
Snapshot hashes bind a read set; they do not grant permission to that read set.
"""
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from .erp_sales_detail_contract import ErpSalesDetailRow, parse_page, assemble_candidate
from .sales_history_archive import SalesHistoryArchive, _binding, _encode, _hash, _time, _uuid

VERSION = 'erp-local-sales-range/1'
MAX_DAYS = 366
MAX_LIMIT = 1000
_BEIJING = ZoneInfo('Asia/Shanghai')


class SalesHistoryQueryError(ValueError):
    """Does not expose private paths, source contents, or caller materials."""


def _deny():
    raise SalesHistoryQueryError('sales_history_query_rejected') from None


def _day(value):
    if type(value) is not str:
        _deny()
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        _deny()
    return result


def _range(start, end, as_of):
    a, b = _day(start), _day(end)
    if (type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None or
            not a <= b <= as_of.astimezone(_BEIJING).date() or (b-a).days >= MAX_DAYS):
        _deny()
    return tuple((a+timedelta(days=i)).isoformat() for i in range((b-a).days+1))


@dataclass(frozen=True)
class DaySnapshot:
    day: str
    acquisition: str | None
    manifest_sha256: str | None
    row_count: int | None
    captured_at: str | None


@dataclass(frozen=True)
class RangeSnapshot:
    version: str
    binding_sha256: str
    start: str
    end: str
    as_of: str
    days: tuple[DaySnapshot, ...]
    snapshot_sha256: str

    @property
    def missing_days(self):
        return tuple(d.day for d in self.days if d.acquisition is None)

    @property
    def structural_coverage_complete(self):
        return not self.missing_days

    @property
    def contains_open_day(self):
        return _day(self.end) == datetime.fromisoformat(self.as_of).astimezone(_BEIJING).date()

    @property
    def total_available_rows(self):
        return sum(d.row_count for d in self.days if d.row_count is not None)


@dataclass(frozen=True)
class LocalSalesRow:
    day: str
    acquisition: str
    row: ErpSalesDetailRow


@dataclass(frozen=True)
class LocalSalesPage:
    snapshot_sha256: str
    rows: tuple[LocalSalesRow, ...]
    total_available_rows: int
    next_offset: int | None
    missing_days: tuple[str, ...]
    structural_coverage_complete: bool
    contains_open_day: bool
    business_semantics_verified: bool = False


def _payload(snapshot):
    return dict(version=snapshot.version, binding_sha256=snapshot.binding_sha256,
                start=snapshot.start, end=snapshot.end, as_of=snapshot.as_of,
                days=[asdict(day) for day in snapshot.days])


def _captured_at(manifest):
    receipts = manifest['page_receipts']
    if type(receipts) is not list or not receipts:
        _deny()
    values = [_time(receipt['observed_at']) for receipt in receipts]
    return max(values, key=datetime.fromisoformat)


class SalesHistoryQuery:
    """Read-only private archive facade, configured solely by a trusted Host.

The authority callback must revalidate the current user/Agent/work/store grant;
it is called repeatedly, and must return exactly True, including after IO.
"""

    def __init__(self, *, archive_path, binding, authority_check):
        try:
            if type(archive_path) is not str or not callable(authority_check):
                _deny()
            self._path = archive_path
            self._binding = _binding(binding)
            self._binding_bytes = _encode(self._binding)
            self._binding_sha = sha256(self._binding_bytes).hexdigest()
            self._authority = authority_check
        except Exception:
            _deny()

    def __repr__(self):
        return 'SalesHistoryQuery(<host-configured>)'

    def _check(self):
        if (_encode(_binding(self._binding)) != self._binding_bytes or
                self._authority() is not True):
            _deny()

    def _validate(self, snapshot, expected_snapshot_sha256):
        _hash(expected_snapshot_sha256)
        if (type(snapshot) is not RangeSnapshot or snapshot.version != VERSION or
                snapshot.binding_sha256 != self._binding_sha or type(snapshot.days) is not tuple or
                any(type(d) is not DaySnapshot for d in snapshot.days)):
            _deny()
        observed = datetime.fromisoformat(_time(snapshot.as_of))
        expected_days = _range(snapshot.start, snapshot.end, observed)
        if tuple(d.day for d in snapshot.days) != expected_days:
            _deny()
        for day in snapshot.days:
            if day.acquisition is None:
                if any(v is not None for v in (day.manifest_sha256, day.row_count, day.captured_at)):
                    _deny()
            else:
                _uuid(day.acquisition)
                if type(day.row_count) is not int or not 0 <= day.row_count <= 100000:
                    _deny()
                _hash(day.manifest_sha256); _time(day.captured_at)
                if datetime.fromisoformat(day.captured_at) > observed:
                    _deny()
        digest = sha256(_encode(_payload(snapshot))).hexdigest()
        if digest != expected_snapshot_sha256 or snapshot.snapshot_sha256 != digest:
            _deny()

    def inspect_range(self, *, start, end, as_of):
        """Pin all available complete days; missing days remain explicit gaps."""
        try:
            dates = _range(start, end, as_of)
            self._check()
            days = []
            with SalesHistoryArchive(self._path, self._binding) as archive:
                for day in dates:
                    self._check()
                    # One read set for the whole range; do not reset it per day.
                    manifest = archive._read_latest(day)
                    self._check()
                    if manifest is None:
                        days.append(DaySnapshot(day, None, None, None, None))
                    else:
                        raw = archive._read(manifest['acquisition']+'.manifest.json')
                        days.append(DaySnapshot(day, manifest['acquisition'], sha256(raw).hexdigest(),
                                                manifest['total_count'], _captured_at(manifest)))
                archive._finish_reads()
                self._check()
            snapshot = RangeSnapshot(VERSION, self._binding_sha, start, end,
                                     as_of.isoformat(), tuple(days), '')
            snapshot = RangeSnapshot(**{**snapshot.__dict__,
                'snapshot_sha256': sha256(_encode(_payload(snapshot))).hexdigest()})
            self._validate(snapshot, snapshot.snapshot_sha256)
            self._check()
            return snapshot
        except Exception:
            _deny()

    def read_rows(self, *, snapshot, expected_snapshot_sha256, offset=0, limit=MAX_LIMIT):
        """Read a stable page of projected original rows, retaining duplicates."""
        try:
            self._check()
            self._validate(snapshot, expected_snapshot_sha256)
            total = snapshot.total_available_rows
            if (type(offset) is not int or not 0 <= offset <= total or
                    type(limit) is not int or not 1 <= limit <= MAX_LIMIT):
                _deny()
            result, position = [], 0
            with SalesHistoryArchive(self._path, self._binding) as archive:
                for day in snapshot.days:
                    self._check()
                    if day.acquisition is None:
                        continue
                    raw = archive._read(day.acquisition+'.manifest.json')
                    if sha256(raw).hexdigest() != day.manifest_sha256:
                        _deny()
                    manifest = archive._verify_snapshot(day.acquisition)
                    if (manifest['day'] != day.day or manifest['total_count'] != day.row_count or
                            _encode(manifest) != raw or _captured_at(manifest) != day.captured_at):
                        _deny()
                    if position < offset+limit and position+day.row_count > offset:
                        pages = []
                        for receipt in manifest['page_receipts']:
                            if not receipt['key'].startswith(day.acquisition+'.p1-'):
                                continue
                            self._check()
                            raw_page = archive._read(receipt['key']+'.raw.json')
                            if sha256(raw_page).hexdigest() != receipt['response_sha256']:
                                _deny()
                            skip = len(pages) * manifest['take']
                            pages.append(parse_page(raw_page, expected_shop_id=self._binding['shop_id'],
                                                    day=day.day, skip=skip, take=manifest['take']))
                            self._check()
                        candidate = assemble_candidate(tuple(pages))
                        if candidate.total_count != day.row_count:
                            _deny()
                        begin, finish = max(0, offset-position), min(day.row_count, offset+limit-position)
                        result.extend(LocalSalesRow(day.day, day.acquisition, row)
                                      for row in candidate.rows[begin:finish])
                    position += day.row_count
                    self._check()
                archive._finish_reads()
                self._check()
            if len(result) != min(limit, total-offset):
                _deny()
            self._check()
            return LocalSalesPage(snapshot.snapshot_sha256, tuple(result), total,
                offset+len(result) if offset+len(result) < total else None,
                snapshot.missing_days, snapshot.structural_coverage_complete, snapshot.contains_open_day)
        except Exception:
            _deny()
