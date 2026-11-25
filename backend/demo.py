"""Standalone loopback practice session over the genuine application core."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
from datetime import datetime, timezone

from fastapi import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

DEV_PERMISSIONS = (
    "catalog.read", "sales.read", "sales.draft.create", "purchasing.read",
    "receiving.read", "inventory.stocktake", "tasks.read",
)


def start(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise ValueError("DEMO_PORT must be 1024–65535")
    web = Path(__file__).resolve().parents[1] / "apps" / "web" / "dist"
    if not (web / "index.html").is_file():
        raise SystemExit("Run bash scripts/demo.sh first to build the Web application.")
    with TemporaryDirectory(prefix="retail-v4-practice-") as directory:
        url = "sqlite:///" + str(Path(directory) / "practice.sqlite3")
        # Enforce local-only options even if invoked directly. Shell entry
        # also starts the application with an empty environment.
        for key in list(os.environ):
            if key not in {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE"}:
                del os.environ[key]
        os.environ["DATABASE_URL"] = url
        os.environ["RETAIL_PI_HOST_MODE"] = "disabled"

        import uvicorn
        from sqlalchemy.orm import Session

        from backend.app.db.base import Base
        from backend.app.db.session import make_engine
        from backend.app.main import app
        from backend.app.modules.auth.service import create_account
        from backend.app.modules.catalog.service import record_snapshot
        from backend.app.modules.identity.models import Organization, PermissionGrant, Store, Subject

        organization, store, subject = "practice-org", "practice-store", "practice-user"
        password = secrets.token_urlsafe(18)
        engine = make_engine(url)
        now = datetime.now(timezone.utc)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session, session.begin():
                session.add(Organization(id=organization, name="虚构零售门店"))
                session.flush()
                session.add_all([
                    Store(id=store, organization_id=organization, name="本地练习店"),
                    Subject(id=subject, organization_id=organization, display_name="练习员工"),
                ])
                session.flush()
                create_account(session, username="demo", password=password, organization_id=organization,
                               store_id=store, subject_id=subject)
                for permission in DEV_PERMISSIONS:
                    session.add(PermissionGrant(id=secrets.token_hex(16), organization_id=organization,
                                                store_id=store, subject_id=subject, permission_key=permission))
                for code, name, spec, amount in (
                    ("DEMO-A01", "演示六角扳手", "5 毫米", 8.0),
                    ("DEMO-A02", "演示十字螺丝刀", "PH2", 16.0),
                    ("DEMO-A03", "演示尼龙扎带", "150 毫米", 12.5),
                ):
                    record_snapshot(
                        session, organization_id=organization, store_id=store, source_system="practice",
                        source_version="v1", source_updated_at=now, now=now,
                        payload={"code": code, "name": name, "spec": spec,
                                 "productCategoryIdName": "演示工具", "brandName": "虚构品牌",
                                 "baseProductStandardList": [{"standardName": "件", "retailPrice": amount}]},
                    )
        finally:
            engine.dispose()

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="practice-assets")

        @app.get("/{path:path}", include_in_schema=False)
        def practice_ui(path: str):
            if path.split("/", 1)[0] in {"api", "health", "assets"}:
                raise HTTPException(404)
            return FileResponse(web / "index.html", headers={"Cache-Control": "no-store"})

        print(f"本地练习: http://127.0.0.1:{port}/catalog | 账号: demo | 本次密码: {password}", flush=True)
        print("试着检索 DEMO-A01。按 Ctrl+C 停止，虚构练习库随进程删除。", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False)


if __name__ == "__main__":
    start(int(sys.argv[1]) if len(sys.argv) > 1 else 8900)
