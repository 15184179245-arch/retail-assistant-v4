"""CAT-002: preserve external truth; local extensions are independent."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from backend.app.main import app  # noqa: F401  (registers all model metadata)
from backend.app.db.base import Base
from backend.app.db.session import make_engine
from backend.app.modules.catalog import service
from backend.app.modules.catalog.models import CatalogConflict, CatalogExtension, CatalogSnapshot
from backend.app.modules.identity.models import Organization, Store

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _payload(code="SKU-1", barcode="1.23457E+12", name="上游商品",
             price=0.0, cost=None):
    standard = {"standardName": "个"}
    if price is not None:
        standard["retailPrice"] = price
    if cost is not None:
        standard["buyingPrice"] = cost
    return {"code": code, "barCode": barcode, "name": name, "spec": "规格A",
            "productCategoryIdName": "合成分类", "brandName": "合成品牌",
            "baseProductStandardList": [standard]}


def test_mapping_preserves_dirty_values_without_derivation():
    mapped = service.map_source_payload(_payload())
    assert mapped["barCode"] == "1.23457E+12"
    assert "DIRTY_SCIENTIFIC" in mapped["barcode_flags"]
    assert mapped["external_code"] == "SKU-1"
    assert mapped["sell_price"] == 0.0  # zero is a real source value
    assert mapped["cost_price"] is None


def test_missing_barcode_stays_missing_and_code_is_never_derived():
    mapped = service.map_source_payload(_payload(barcode=None))
    assert mapped["barCode"] is None
    assert mapped["external_code"] == "SKU-1"
    assert "NO_BARCODE" in mapped["barcode_flags"]


def test_quoted_and_whitespace_barcodes_are_verbatim():
    quoted = service.map_source_payload(_payload(barcode='"12345"'))
    assert quoted["barCode"] == '"12345"'
    spaced = service.map_source_payload(_payload(barcode=" 6901234567890 "))
    assert spaced["barCode"] == " 6901234567890 "


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Organization(id="org-1", name="合成组织"))
        session.add_all([
            Store(id="store-1", organization_id="org-1", name="合成门店"),
            Store(id="store-2", organization_id="org-1", name="合成二店"),
        ])
    yield factory
    engine.dispose()


def test_snapshot_is_immutable_and_extension_is_not_overwritten(db):
    with db.begin() as session:
        product, first, conflicts = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v1",
            source_updated_at=NOW, payload=_payload(name="上游旧名"), now=NOW)
        assert conflicts == []
        service.add_extension(session, organization_id="org-1", store_id="store-1",
                              product_id=product.id,
                              payload={"overrides": {"name": "本地自定义名"}},
                              updated_by="staff-1", now=NOW)

    with db.begin() as session:
        product, second, conflicts = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v2",
            source_updated_at=NOW, payload=_payload(name="上游新名"), now=NOW)
        assert second.id != first.id
        assert len(conflicts) == 1 and conflicts[0].field_name == "name"
        assert conflicts[0].status == "pending"
        extension = session.query(CatalogExtension).one()
        assert extension.payload["overrides"]["name"] == "本地自定义名"
        assert product.current_snapshot_id == second.id

    with db.begin() as session:
        product, third, conflicts = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v2",
            source_updated_at=NOW, payload=_payload(name="上游新名"), now=NOW)
        assert third.id == second.id and conflicts == []
        assert session.query(CatalogSnapshot).count() == 2


def test_cross_store_access_is_rejected(db):
    with db.begin() as session:
        product, _, _ = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v1",
            source_updated_at=NOW, payload=_payload(), now=NOW)
    with db.begin() as session:
        with pytest.raises(HTTPException) as error:
            service.get_product(session, organization_id="org-1", store_id="store-2",
                                product_id=product.id)
        assert error.value.status_code == 404
        assert service.list_products(session, organization_id="org-1",
                                     store_id="store-2") == []


def test_source_can_return_to_old_payload_and_update_projection(db):
    from datetime import timedelta
    with db.begin() as session:
        product, a1, _ = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v1",
            source_updated_at=NOW, payload=_payload(name="A"), now=NOW)
        service.add_extension(session, organization_id="org-1", store_id="store-1",
                              product_id=product.id,
                              payload={"overrides": {"name": "本地名"}}, updated_by="s1", now=NOW)
        _, b, _ = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v2",
            source_updated_at=NOW + timedelta(minutes=1),
            payload=_payload(name="B"), now=NOW + timedelta(minutes=1))
        assert product.current_snapshot_id == b.id
    with db.begin() as session:
        product, returned, conflicts = service.record_snapshot(
            session, organization_id="org-1", store_id="store-1",
            source_system="synthetic-source", source_version="v3",
            source_updated_at=NOW + timedelta(minutes=2),
            payload=_payload(name="A"), now=NOW + timedelta(minutes=2))
        assert returned.id != a1.id  # new observation, not the stale v1 row
        assert product.current_snapshot_id == returned.id
        assert product.source_version == "v3"
        assert returned.payload["name"] == "A"
        assert conflicts and conflicts[0].field_name == "name"
    with db.begin() as session:
        product, current = service.get_product(
            session, organization_id="org-1", store_id="store-1",
            product_id=product.id)
        assert current.payload["name"] == "A"
        assert product.source_version == "v3"


def test_source_code_with_spaces_is_not_silently_rewritten():
    raw = _payload(code=" SKU-1 ")
    mapped = service.map_source_payload(raw)
    assert mapped["external_code"] == " SKU-1 "
    assert mapped["raw"] == raw
