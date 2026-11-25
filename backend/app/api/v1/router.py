from fastapi import APIRouter

from backend.app.modules.audit.router import router as audit_router
from backend.app.modules.catalog.router import router as catalog_router
from backend.app.modules.media.router import router as media_router
from backend.app.modules.locations.router import router as locations_router
from backend.app.modules.memory.router import router as memory_router
from backend.app.modules.agents.router import cost_router as agents_cost_router
from backend.app.modules.agents.router import router as agents_router
from backend.app.modules.approvals.router import router as approvals_router
from backend.app.modules.auth.router import router as auth_router
from backend.app.modules.identity.router import router as identity_router
from backend.app.modules.knowledge.router import router as knowledge_router
from backend.app.modules.sales.router import router as sales_router
from backend.app.modules.purchasing.router import router as purchasing_router
from backend.app.modules.stocktake.router import router as stocktake_router
from backend.app.modules.analytics.router import router as analytics_router
from backend.app.modules.search.router import router as search_router
from backend.app.modules.tasks.router import router as tasks_router
from backend.app.modules.notifications.router import router as notifications_router
from backend.app.modules.assistant.router import router as assistant_router
from backend.app.modules.workspace.router import router as workspace_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(identity_router)
router.include_router(approvals_router)
router.include_router(tasks_router)
router.include_router(notifications_router)
router.include_router(audit_router)
router.include_router(agents_router)
router.include_router(agents_cost_router)
router.include_router(knowledge_router)
router.include_router(sales_router)
router.include_router(purchasing_router)
router.include_router(stocktake_router)
router.include_router(analytics_router)
router.include_router(search_router)
router.include_router(catalog_router)
router.include_router(media_router)
router.include_router(locations_router)
router.include_router(memory_router)
router.include_router(assistant_router)
router.include_router(workspace_router)
