"""Host local dataset selection and safe quantity views; no ERP credentials.

Source pins bind identity/integrity only. Every operation requires a callback
that performs the current SQL user/Agent/machine/Work authorization checks.
Neither filesystem presence nor a snapshot digest grants disclosure authority.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from backend.app.modules.agents import local_sales_runtime as runtime
from backend.app.modules.agents.source_environment import SourceEnvironment
from .sales_history_archive import SalesHistoryArchive, _binding, _encode
from .sales_history_query import SalesHistoryQuery, RangeSnapshot, DaySnapshot, _payload
from .local_sales_analysis import aggregate_products, RULE
from .sales_ranking_receipts import _read, _root_fd
from . import erp_sales_detail_contract, retail_sales_detail_contract, sales_ranking_contract

_DATASET_ROOT = Path.home()/'.local/share/retail-v4/sales-history'
_CHINA = ZoneInfo('Asia/Shanghai')
_DESCRIPTOR = 'local-sales.json'


class LocalSalesSourceDenied(ValueError):
    def __init__(self):
        super().__init__('local_sales_source_rejected')


def _deny():
    raise LocalSalesSourceDenied() from None


def analysis_contract_fingerprint():
    files = [Path(__file__), Path(runtime.__file__), Path(erp_sales_detail_contract.__file__),
             Path(retail_sales_detail_contract.__file__), Path(sales_ranking_contract.__file__),
             Path(__file__).with_name('local_sales_analysis.py'),
             Path(__file__).with_name('sales_history_query.py')]
    entries = sorted((p.name, sha256(p.read_bytes()).hexdigest()) for p in files)
    return sha256(_encode(entries)).hexdigest()


@dataclass(frozen=True, repr=False)
class PreparedLocalResult:
    result: dict
    snapshot: dict


def snapshot_from_json(value):
    fields = {'version','binding_sha256','start','end','as_of','days','snapshot_sha256'}
    if type(value) is not dict or set(value) != fields or type(value['days']) is not list:
        _deny()
    return RangeSnapshot(**{**value,'days':tuple(DaySnapshot(**d) for d in value['days'])})


def snapshot_to_json(snapshot):
    return {**_payload(snapshot), 'snapshot_sha256':snapshot.snapshot_sha256}


class LocalSourceSelection:
    """Fixed host alias, private descriptor, no material.json/password reads."""

    def __init__(self, environment, descriptor_sha256):
        self._rootfd = -1
        try:
            if type(environment) is not SourceEnvironment or environment.trial_name is not None:
                _deny()
            if type(descriptor_sha256) is not str or re.fullmatch('[a-f0-9]{64}',descriptor_sha256) is None:
                _deny()
            environment.revalidate()
            self._environment = environment
            self._descriptor_sha = descriptor_sha256
            self._rootfd = _root_fd(environment.root)
            info = os.fstat(self._rootfd)
            self._root_identity = info.st_dev, info.st_ino
            self._raw = _read(self._rootfd,_DESCRIPTOR,16384)
            if sha256(self._raw).hexdigest() != descriptor_sha256:
                _deny()
            from .live_loader import _decode
            descriptor = _decode(self._raw)
            if (type(descriptor) is not dict or set(descriptor) != {'version','purpose','binding',
                    'earliest_day','analysis_contract_sha256'} or type(descriptor['version']) is not int or
                    descriptor['version'] != 1 or descriptor['purpose'] != 'authorized-local-sales-dataset'):
                _deny()
            self._binding = _binding(descriptor['binding'])
            self._earliest = runtime._day(descriptor['earliest_day'])
            self._analysis_sha = descriptor['analysis_contract_sha256']
            if self._analysis_sha != analysis_contract_fingerprint():
                _deny()
            values = environment.values
            if any(self._binding[k] != values[k] for k in ('organization_id','store_id')):
                _deny()
            self._pin = runtime.SourcePin(version=3,environment_sha256=environment.sha256,
                descriptor_sha256=descriptor_sha256,dataset_binding_sha256=sha256(_encode(self._binding)).hexdigest(),
                analysis_contract_sha256=self._analysis_sha,**{k:values[k] for k in
                    ('organization_id','store_id','requester_id','agent_id','machine_id')},
                shop_id=self._binding['shop_id'],source_namespace=self._binding['source_namespace'],
                earliest_day=self._earliest.isoformat()).model_dump(mode='json')
            self._pid = os.getpid()
            self.revalidate()
        except BaseException:
            self.close()
            _deny()

    def __repr__(self):
        return '<LocalSalesSourceSelection>'

    def close(self):
        if self._rootfd >= 0:
            os.close(self._rootfd)
            self._rootfd = -1

    def pin_dict(self):
        self.revalidate()
        return runtime.validate_source(self._pin)

    def revalidate(self):
        try:
            if self._rootfd < 0 or os.getpid() != self._pid:
                _deny()
            self._environment.revalidate()
            for info in (os.fstat(self._rootfd),os.stat(self._environment.root,follow_symlinks=False)):
                if ((info.st_dev,info.st_ino) != self._root_identity or info.st_uid != os.getuid() or
                        info.st_mode & 0o777 != 0o700):
                    _deny()
            raw = _read(self._rootfd,_DESCRIPTOR,16384)
            if raw != self._raw or sha256(raw).hexdigest() != self._descriptor_sha:
                _deny()
            if analysis_contract_fingerprint() != self._analysis_sha:
                _deny()
            with SalesHistoryArchive(str(_DATASET_ROOT),self._binding) as archive:
                if archive.binding_sha != self._pin['dataset_binding_sha256']:
                    _deny()
        except BaseException:
            _deny()

    def _query(self, authority_check):
        if not callable(authority_check):
            _deny()
        def authorized():
            if authority_check() is not True:
                return False
            self.revalidate()
            return authority_check() is True
        if not authorized():
            _deny()
        return SalesHistoryQuery(archive_path=str(_DATASET_ROOT),binding=self._binding,
                                 authority_check=authorized), authorized

    def _latest_day(self, today):
        candidates = []
        with SalesHistoryArchive(str(_DATASET_ROOT),self._binding) as archive:
            for name in os.listdir(archive._root):
                if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\.latest\.json',name):
                    day = runtime._day(name[:10])
                    if self._earliest <= day <= today:
                        candidates.append(day)
        return max(candidates) if candidates else None

    @staticmethod
    def _collection(snapshot):
        captured = [d.captured_at for d in snapshot.days if d.captured_at is not None]
        return dict(start_date=snapshot.start,end_date=snapshot.end,queried_at=snapshot.as_of,
            captured_through=max(captured,key=runtime._instant) if captured else None,
            snapshot_sha256=snapshot.snapshot_sha256,dataset_binding_sha256=snapshot.binding_sha256,
            source_namespace='erp_posted_sales_detail_v1',calendar_basis='source_bill_date',
            source_row_count=snapshot.total_available_rows,
            structural_coverage_complete=snapshot.structural_coverage_complete,
            contains_open_day=snapshot.contains_open_day,missing_days=list(snapshot.missing_days),
            days=[asdict(d) for d in snapshot.days])

    def _render(self, query, snapshot, params, authorized):
        output = dict(version=3,classification='local_posted_sales_analysis',operation=params.operation,
            collection=self._collection(snapshot),quantity_rule=RULE,monetary_totals_included=False,
            atomic_snapshot_verified=False)
        if params.operation == 'coverage':
            # Reconsumption verifies the original read set even when only
            # metadata was requested; it never substitutes newer pointers.
            query.read_rows(snapshot=snapshot,expected_snapshot_sha256=snapshot.snapshot_sha256,
                            offset=snapshot.total_available_rows,limit=1)
            if not authorized():
                _deny()
            return runtime.Output(**output).model_dump(mode='json')
        if params.operation == 'aggregate' and not snapshot.structural_coverage_complete:
            _deny()
        rows, offset = [], 0
        while True:
            page = query.read_rows(snapshot=snapshot,expected_snapshot_sha256=snapshot.snapshot_sha256,
                                   offset=offset,limit=1000)
            rows.extend(page.rows)
            if page.next_offset is None:
                break
            offset = page.next_offset
        if len(rows) != snapshot.total_available_rows:
            _deny()
        if params.operation == 'aggregate':
            analysis = aggregate_products(tuple(rows),expected_shop_id=self._binding['shop_id'],
                expected_row_count=snapshot.total_available_rows,metric=params.metric,order=params.order,
                offset=params.offset,limit=params.limit)
            output.update(metric=analysis.metric,order=analysis.order,offset=analysis.offset,
                next_offset=analysis.next_offset,total_product_count=analysis.total_product_count,
                explicit_gift_rows=analysis.explicit_gift_rows,products=[dict(product_id=r.product_id,
                    product_code=r.product_code,product_name=r.product_name,standard_id=r.standard_id,
                    standard_name=r.standard_name,quantity=str(r.quantity),source_row_count=r.source_row_count,
                    source_refs_sha256=r.source_refs_sha256) for r in analysis.rows])
        else:
            matched = [r for r in rows if params.product_id is None or r.row.product_id == params.product_id]
            if params.offset > len(matched):
                _deny()
            selected = matched[params.offset:params.offset+params.limit]
            end = params.offset+len(selected)
            output.update(offset=params.offset,next_offset=end if end<len(matched) else None,
                total_matching_rows=len(matched),rows=[dict(day=r.day,acquisition=r.acquisition,
                    source_ordinal=r.row.source_ordinal,product_id=r.row.product_id,product_code=r.row.product_code,
                    product_name=r.row.product_name,standard_id=r.row.standard_id,standard_name=r.row.standard_name,
                    source_quantity=str(r.row.qty),is_giveaway=r.row.is_giveaway,bill_type=r.row.bill_type,
                    business_type=r.row.business_type,red_word=r.row.red_word) for r in selected])
        if not authorized():
            _deny()
        result = runtime.Output(**output).model_dump(mode='json')
        if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>100000:
            _deny()
        return result

    def evaluate(self, arguments, *, authority_check, now=None):
        try:
            params = runtime.Input.model_validate(arguments)
            query, authorized = self._query(authority_check)
            now = datetime.now(timezone.utc) if now is None else now
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                _deny()
            today = now.astimezone(_CHINA).date()
            latest = self._latest_day(today)
            start = runtime._day(params.start_date) if params.start_date is not None else today.replace(day=1)
            end = runtime._day(params.end_date) if params.end_date is not None else latest or today
            if params.end_date is None and end < start:
                end = today  # Entire current month remains an explicit gap.
            if start < self._earliest or end > today or not authorized():
                _deny()
            snapshot = query.inspect_range(start=start.isoformat(),end=end.isoformat(),as_of=now)
            result = self._render(query,snapshot,params,authorized)
            if not authorized():
                _deny()
            return PreparedLocalResult(result,snapshot_to_json(snapshot))
        except BaseException:
            _deny()

    def recompute(self, arguments, snapshot_value, *, authority_check):
        """Read the original immutable snapshot; never resolve new latest days."""
        try:
            params = runtime.Input.model_validate(arguments)
            query, authorized = self._query(authority_check)
            snapshot = snapshot_from_json(snapshot_value)
            query._validate(snapshot,snapshot.snapshot_sha256)
            if (snapshot.binding_sha256 != self._pin['dataset_binding_sha256'] or
                    runtime._day(snapshot.start)<self._earliest or
                    params.start_date is not None and params.start_date!=snapshot.start or
                    params.end_date is not None and params.end_date!=snapshot.end):
                _deny()
            result = self._render(query,snapshot,params,authorized)
            if not authorized():
                _deny()
            return result
        except BaseException:
            _deny()
