"""SRCH-001 HTTP contract: authentication, scope, six-field envelope."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.base import Base
from backend.app.db.session import get_session, make_engine
from backend.app.main import app
from backend.app.modules.catalog import service as catalog
from backend.app.modules.identity.models import Organization, Store, Subject, PermissionGrant
from backend.tests.contract.test_platform_api import ENVELOPE_KEYS, request

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _payload():
    return {"code": "SKU-HTTP-1", "barCode": "2000000002002", "name": "合成甲",
            "spec": "规格", "productCategoryIdName": "分类", "brandName": "品牌",
            "baseProductStandardList": [{"standardName": "个", "retailPrice": 3.0,
                                         "buyingPrice": 1.0}]}


def _setup(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'search-http.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Organization(id="org-1", name="合成组织"))
        session.add_all([Store(id="store-1", organization_id="org-1", name="一店"),
                         Store(id="store-2", organization_id="org-1", name="二店")])
        session.add_all([Subject(id="subject-1", organization_id="org-1", display_name="合成一"),
                         Subject(id="subject-2", organization_id="org-1", display_name="合成二")])
        session.flush()
        for number in (1, 2):
            session.add(PermissionGrant(id=f"search-grant-{number}", organization_id="org-1",
                                        store_id=f"store-{number}", subject_id=f"subject-{number}",
                                        permission_key="catalog.read", granted=True, active=True))
        catalog.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v1",
            source_updated_at=NOW, payload=_payload(), now=NOW)
    return engine, factory


def test_search_requires_authentication(tmp_path):
    engine, factory = _setup(tmp_path)
    try:
        status, body = asyncio.run(request(app, "/api/v1/search?q=SKU-HTTP-1"))
        assert status == 401
        assert set(body) == ENVELOPE_KEYS
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_search_returns_scoped_exact_result_and_envelope(tmp_path):
    engine, factory = _setup(tmp_path)

    async def subject():
        return AuthenticatedSubject("subject-1", "org-1", "store-1",
                                    datetime.now(timezone.utc) + timedelta(minutes=5))

    def session_dependency():
        with factory() as session:
            yield session

    app.dependency_overrides[require_authenticated_subject] = subject
    app.dependency_overrides[get_session] = session_dependency
    try:
        status, body = asyncio.run(
            request(app, "/api/v1/search?q=SKU-HTTP-1"))
        assert status == 200
        assert set(body) == ENVELOPE_KEYS
        assert body["success"] is True and body["code"] == "OK"
        result = body["data"]
        assert result["query"] == "SKU-HTTP-1"
        assert result["exact"][0]["external_code"] == "SKU-HTTP-1"
        assert result["exact"][0]["match_type"] == "exact_code"
        assert result["candidates"] == [] and result["ambiguous"] is False

        async def other_store():
            return AuthenticatedSubject("subject-2", "org-1", "store-2",
                                        datetime.now(timezone.utc) + timedelta(minutes=5))

        app.dependency_overrides[require_authenticated_subject] = other_store
        other_status, other_body = asyncio.run(
            request(app, "/api/v1/search?q=SKU-HTTP-1"))
        assert other_status == 200
        assert other_body["data"]["exact"] == []
        assert other_body["data"]["candidates"] == []
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_search_without_catalog_read_is_forbidden(tmp_path):
    engine, factory = _setup(tmp_path)
    with factory.begin() as session:
        grant = session.get(PermissionGrant, "search-grant-1")
        grant.active = False

    async def subject():
        return AuthenticatedSubject("subject-1", "org-1", "store-1",
                                    datetime.now(timezone.utc) + timedelta(minutes=5))

    def session_dependency():
        with factory() as session:
            yield session

    app.dependency_overrides[require_authenticated_subject] = subject
    app.dependency_overrides[get_session] = session_dependency
    try:
        status, body = asyncio.run(request(app, "/api/v1/search?q=SKU-HTTP-1"))
        assert status == 403
        assert set(body) == ENVELOPE_KEYS
        assert body["success"] is False
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_cross_field_source_clue_contract_and_scope(tmp_path,monkeypatch):
    from backend.app.modules.search.source_clues import build_clue_index
    engine,factory=_setup(tmp_path)
    with factory.begin() as session:
        catalog.record_snapshot(session,organization_id='org-1',store_id='store-1',
            source_system='synthetic-source',source_version='v2',source_updated_at=NOW,
            payload={**_payload(),'model':'OLD-MODEL-001'},now=NOW)
    path=tmp_path/'clues.db'
    with factory() as session:
        build_clue_index(session,organization_id='org-1',store_id='store-1',target=path)
    monkeypatch.setenv('SEARCH_SOURCE_CLUES_PATH',str(path))
    async def subject():
        return AuthenticatedSubject('subject-1','org-1','store-1',
            datetime.now(timezone.utc)+timedelta(minutes=5))
    def session_dependency():
        with factory() as session:yield session
    app.dependency_overrides[require_authenticated_subject]=subject
    app.dependency_overrides[get_session]=session_dependency
    try:
        status,body=asyncio.run(request(app,'/api/v1/search?q=OLD-MODEL-001'))
        assert status==200 and set(body)==ENVELOPE_KEYS
        assert not body['data']['exact']
        candidate=body['data']['candidates'][0]
        assert candidate['match_field']=='derived'
        assert candidate['search_evidence'][0]['value']=='OLD-MODEL-001'
        assert candidate['search_evidence'][0]['source_field']=='model'
        assert candidate['search_evidence'][0]['certainty']=='observed_field'
    finally:
        app.dependency_overrides.clear();engine.dispose()
