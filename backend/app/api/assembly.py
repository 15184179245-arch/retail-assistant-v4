import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from backend.app.api.v1.router import router as v1_router
from backend.app.modules.office_intake.router import router as office_intake_router
from backend.app.modules.office_extraction.ofc_003_service import router as office_extraction_router
from backend.app.modules.office_rules.ofc_004_service import router as office_rules_router
from backend.app.modules.erp_commands.erp_002_service import router as erp_commands_router
from backend.app.modules.erp_commands import erp_003_models as erp_sandbox_models  # noqa: F401
from backend.app.modules.office_workflows import ofc_006_models as office_workflow_models  # noqa: F401
from backend.app.modules.office_exceptions import ofc_007_models as office_exception_models  # noqa: F401
from backend.app.modules.office_scheduling import ofc_008_models as office_schedule_models  # noqa: F401
from backend.app.modules.erp_sales import erp_004_models as erp_sales_models  # noqa: F401
from backend.app.modules.erp_purchasing import erp_005_models as erp_receipt_models  # noqa: F401
from backend.app.modules.erp_catalog import erp_007_models as erp_catalog_models  # noqa: F401
from backend.app.modules.erp_finance import erp_006_models as erp_settlement_models  # noqa: F401
from backend.app.modules.erp_finance.erp_006_api import router as erp_settlement_router
from backend.app.modules.office_review import ofc_009_models as office_review_models  # noqa: F401
from backend.app.modules.office_review.ofc_009_api import router as office_review_router
from backend.app.modules.erp_sales.erp_004_source import ERPSalesError
from backend.app.modules.erp_sales.erp_004_api import router as erp_sales_router
from backend.app.modules.erp_purchasing.erp_005_api import router as erp_receipt_router
from backend.app.modules.erp_purchasing.erp_005_source import ERPReceiptError
from backend.app.modules.erp_catalog.erp_007_api import router as erp_catalog_router
from backend.app.modules.erp_catalog.erp_007_source import ERPCatalogError
from backend.app.modules.office_workflows.ofc_006_service import router as office_workflow_router, WorkflowError, error_status as workflow_error_status
from backend.app.modules.office_scheduling.ofc_008_service import router as office_schedule_router, ScheduleError, error_status as schedule_error_status
from backend.app.modules.office_exceptions.ofc_007_service import router as office_exception_router, OfficeExceptionError, error_status as exception_error_status
from backend.app.modules.erp_commands.erp_003_service import router as erp_sandbox_router
from backend.app.modules.office_documents.ofc_005_service import router as office_documents_router
from backend.app.modules.office_finance.fin_001_service import router as office_finance_router
from backend.app.core.errors import ApiEnvelope
from backend.app.db.session import get_session
from backend.app.modules.agents.service import AgentRuntimeError
from backend.app.workers.outbox import OutboxEvent

from backend.app.modules.sales.service import SalesError


def _domain_error_response(status: int, error: str, message: str, hint: str | None) -> JSONResponse:
    body = ApiEnvelope(success=False, data=None, code=error.upper(), error=error,
                       user_message=message, hint=hint)
    return JSONResponse(status_code=status, content=body.model_dump(), headers={"Cache-Control": "no-store"})


async def agent_error_handler(_request: Request, exc: AgentRuntimeError) -> JSONResponse:
    message, hint = {
        "pi_provider_capacity_exceeded": (
            "输入及上下文超出本次处理上限。", "请拆分要求后重试；内容未被自动截断。"),
    }.get(exc.code, ("Agent 请求无法完成。", None))
    return _domain_error_response(exc.status_code, exc.code, message, hint)


async def workflow_error_handler(_request: Request, exc: WorkflowError) -> JSONResponse:
    status = workflow_error_status(exc.code)
    messages = {403: "当前身份不能执行此流程动作。", 404: "当前门店没有可读取的流程或任务。",
                422: "流程请求参数不符合要求。", 503: "此材料所需的解析能力暂不可用。"}
    return _domain_error_response(status, exc.code,
        messages.get(status, "流程内容、状态或来源校验已变化，请刷新并核对。"), "请核对原始记录；结果未知时不要重复执行。")


async def erp_sales_error_handler(_request: Request, exc: ERPSalesError) -> JSONResponse:
    status = 404 if exc.code == "erp_sales_not_found" else 403 if exc.code in (
        "erp_sales_human_required", "erp_sales_participant_unavailable") else 422 if exc.code in ("erp_sales_input_invalid", "erp_sales_cursor_invalid") else 409
    return _domain_error_response(status, exc.code,
        "销售命令、来源或授权状态不符合要求，请刷新核对。", "已绑定或结果未知的销售不得换通路重发。")


async def erp_receipt_error_handler(_request: Request, exc: ERPReceiptError) -> JSONResponse:
    status = 404 if exc.code == "erp_receipt_not_found" else 403 if exc.code in (
        "erp_receipt_human_required", "erp_receipt_participant_unavailable") else 422 if exc.code in (
        "erp_receipt_input_invalid", "erp_receipt_cursor_invalid") else 409
    return _domain_error_response(status, exc.code,
        "入库命令、来源或授权状态已变化，请刷新核对。", "已绑定或结果未知的批次不得换通路重发。")


async def erp_catalog_error_handler(_request: Request, exc: ERPCatalogError) -> JSONResponse:
    status = 404 if exc.code == "erp_catalog_not_found" else 403 if exc.code in (
        "erp_catalog_human_required", "erp_catalog_participant_unavailable") else 422 if exc.code in (
        "erp_catalog_input_invalid", "erp_catalog_cursor_invalid") else 409
    return _domain_error_response(status, exc.code,
        "商品维护命令、来源或授权状态已变化，请刷新核对。", "结果未知时请回查原命令，不得重复派发。")


async def office_schedule_error_handler(_request: Request, exc: ScheduleError) -> JSONResponse:
    status = schedule_error_status(exc.code)
    return _domain_error_response(status, exc.code,
        "排程、授权或所见版本已变化，请刷新后核对。", "排程只生成待人审建议，未知结果请先回查原操作。")


async def office_exception_error_handler(_request: Request, exc: OfficeExceptionError) -> JSONResponse:
    status = exception_error_status(exc.code)
    messages = {403: "当前身份或来源读取权限不能执行此异常操作。", 404: "当前门店没有可读取的异常或来源。",
                422: "异常请求参数不符合要求。", 400: "操作标识不符合要求。"}
    return _domain_error_response(status, exc.code,
        messages.get(status, "异常状态、所见来源或核验依据已变化，请刷新后重新核对。"), "异常关闭仅记录核对结果；原业务结果未知时不要重复执行。")


async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "http_error"
    code_map = {
        "office_extraction_sheet_limit": ("OFFICE_EXTRACTION_SHEET_LIMIT", "工作表超过 30 张，未生成解析结果。", "请拆分为每份不超过 30 张工作表后重试。"),
        "office_review_source_not_integrated": ("OFFICE_REVIEW_SOURCE_NOT_INTEGRATED", "审核来源尚未接入，此处仅保留历史查询。", "请使用对应业务的原有审批入口。"),
        "assistant_input_capacity_exceeded": ("ASSISTANT_INPUT_CAPACITY_EXCEEDED", "这条消息超出当前传输容量。", "请分段发送；原文未截断，也未提交执行。"),
        "knowledge_revision_closed": ("KNOWLEDGE_REVISION_CLOSED", "知识已撤回或失效，不能直接修订。", "如需重新提交，请创建新的待审条目。"),
        "knowledge_version_conflict": ("KNOWLEDGE_VERSION_CONFLICT", "知识版本已变化。", "请刷新并审阅最新版本后再操作。"),
        "knowledge_permission_denied": ("KNOWLEDGE_PERMISSION_DENIED", "当前身份没有此知识操作权限。", None),
        "knowledge_command_conflict": ("KNOWLEDGE_COMMAND_CONFLICT", "知识命令发生竞争。", "请用原幂等键查询或重试。"),
        "knowledge_command_incomplete": ("KNOWLEDGE_COMMAND_INCOMPLETE", "知识命令收据不完整。", "请联系管理员核对，勿重复提交。"),
        "authentication_required": ("AUTHENTICATION_REQUIRED", "请先登录。", "使用员工账号和密码登录。"),
        "invalid_credentials": ("INVALID_CREDENTIALS", "账号或凭据无效，或暂时无法登录。", "请检查输入，稍后重试或联系管理员。"),
        "authentication_unavailable": ("AUTHENTICATION_UNAVAILABLE", "服务端身份认证尚未配置。", "请联系管理员完成认证配置。"),
        "credential_expired": ("CREDENTIAL_EXPIRED", "登录凭据已过期。", "请重新登录后重试。"),
        "store_scope_denied": ("STORE_SCOPE_DENIED", "无权访问该门店。", "请切换到已授权门店。"),
        "organization_scope_denied": ("ORGANIZATION_SCOPE_DENIED", "无权访问该组织。", None),
        "identity_inactive": ("IDENTITY_INACTIVE", "当前身份或门店已停用。", "请联系管理员。"),
        "permission_denied": ("PERMISSION_DENIED", "当前身份没有执行此操作的权限。", "请联系有权限的负责人。"),
        "idempotency_key_required": ("IDEMPOTENCY_KEY_REQUIRED", "请求缺少幂等标识。", "为写入请求添加 Idempotency-Key。"),
        "idempotency_key_invalid": ("IDEMPOTENCY_KEY_INVALID", "请求幂等标识无效。", "请检查 Idempotency-Key 长度。"),
        "idempotency_conflict": ("IDEMPOTENCY_CONFLICT", "幂等标识已用于不同请求。", "请使用新的 Idempotency-Key。"),
        "approval_conflict": ("APPROVAL_CONFLICT", "审批请求已发生状态变化。", "请刷新状态后重试。"),
        "approval_not_found": ("APPROVAL_NOT_FOUND", "审批请求不存在或不属于当前门店。", None),
        "approval_cursor_invalid": ("APPROVAL_CURSOR_INVALID", "审批分页游标无效。", "请从当前门店审批列表重新开始分页。"),
        "catalog_cache_cursor_invalid": ("CATALOG_CACHE_CURSOR_INVALID", "商品缓存游标无效。", "请从当前门店商品缓存重新开始分页。"),
        "media_permission_denied": ("MEDIA_PERMISSION_DENIED", "当前身份没有媒体访问权限。", None),
        "media_category_invalid": ("MEDIA_CATEGORY_INVALID", "媒体分类无效。", "仅支持分类图片或盘点照片。"),
        "media_type_unsupported": ("MEDIA_TYPE_UNSUPPORTED", "媒体类型不受支持。", "仅支持 JPEG/PNG/WebP。"),
        "media_too_large": ("MEDIA_TOO_LARGE", "媒体文件超过大小限制。", "文件不得超过 5 MiB。"),
        "media_empty": ("MEDIA_EMPTY", "媒体文件为空。", None),
        "media_asset_not_found": ("MEDIA_ASSET_NOT_FOUND", "媒体不存在或不属于当前门店。", None),
        "media_content_missing": ("MEDIA_CONTENT_MISSING", "媒体文件缺失。", "请联系管理员核对存储。"),
        "media_dedupe_conflict": ("MEDIA_DEDUPE_CONFLICT", "相同文件已存在但分类不同。", "请使用匹配的分类或上传新文件。"),
        "media_source_invalid": ("MEDIA_SOURCE_INVALID", "媒体来源无效。", None),
        "stocktake_person_required": ("STOCKTAKE_PERSON_REQUIRED", "盘点作业仅供授权人员操作。", None),
        "stocktake_session_not_found": ("STOCKTAKE_SESSION_NOT_FOUND", "盘点作业不存在或不属于当前门店。", None),
        "stocktake_observation_not_found": ("STOCKTAKE_OBSERVATION_NOT_FOUND", "原观察不存在或不属于此盘点。", None),
        "stocktake_session_version_conflict": ("STOCKTAKE_SESSION_VERSION_CONFLICT", "盘点作业已变化，请刷新版本后继续。", None),
        "stocktake_idempotency_conflict": ("STOCKTAKE_IDEMPOTENCY_CONFLICT", "此事件幂等键对应不同请求。", None),
        "stocktake_observation_superseded": ("STOCKTAKE_OBSERVATION_SUPERSEDED", "原观察已有更正，请核对最新事件。", None),
        "stocktake_coverage_invalid": ("STOCKTAKE_COVERAGE_INVALID", "盘点范围缺少有效位置。", None),
        "stocktake_location_not_found": ("STOCKTAKE_LOCATION_NOT_FOUND", "库位不存在或不属于当前门店。", None),
        "stocktake_product_not_found": ("STOCKTAKE_PRODUCT_NOT_FOUND", "商品不存在或不属于当前门店。", None),
        "stocktake_media_not_found": ("STOCKTAKE_MEDIA_NOT_FOUND", "盘点照片不存在或不属于当前门店。", None),
        "stocktake_observation_time_invalid": ("STOCKTAKE_OBSERVATION_TIME_INVALID", "观察时间不在允许范围。", None),
        "stocktake_unknown_product_invalid": ("STOCKTAKE_UNKNOWN_PRODUCT_INVALID", "未知商品必须提供原始标签。", None),
        "stocktake_unknown_location_invalid": ("STOCKTAKE_UNKNOWN_LOCATION_INVALID", "未知库位必须提供原始线索。", None),
        "media_name_invalid": ("MEDIA_NAME_INVALID", "媒体文件名无效。", None),
        "media_time_invalid": ("MEDIA_TIME_INVALID", "媒体时间无效。", None),
        "memory_not_found": ("MEMORY_NOT_FOUND", "个人记忆不存在或不可见。", None),
        "memory_permission_denied": ("MEMORY_PERMISSION_DENIED", "当前身份没有个人记忆权限。", None),
        "memory_content_invalid": ("MEMORY_CONTENT_INVALID", "个人记忆内容无效。", None),
        "memory_policy_required": ("MEMORY_POLICY_REQUIRED", "长期保存需要服务端留存政策、用途和明确期限。", None),
        "memory_expiry_invalid": ("MEMORY_EXPIRY_INVALID", "个人记忆期限无效。", None),
        "memory_idempotency_invalid": ("MEMORY_IDEMPOTENCY_INVALID", "个人记忆幂等键无效。", None),
        "memory_purpose_invalid": ("MEMORY_PURPOSE_INVALID", "个人记忆用途无效。", None),
        "memory_idempotency_conflict": ("MEMORY_IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求。", None),
        "memory_command_conflict": ("MEMORY_COMMAND_CONFLICT", "记忆命令未完成或收据冲突，请刷新核对。", None),
        "sales_quote_required": ("SALES_QUOTE_REQUIRED", "请先为每行商品取得有效报价。", None),
        "sales_quote_invalid": ("SALES_QUOTE_INVALID", "商品、金额或报价绑定不一致，请重新报价。", None),
        "sales_version_conflict": ("SALES_VERSION_CONFLICT", "草稿已变化，请刷新重试。", None),
        "finance_workpaper_not_found": ("FINANCE_WORKPAPER_NOT_FOUND", "对账底稿不存在或不属于当前门店。", None),
        "finance_idempotency_conflict": ("FINANCE_IDEMPOTENCY_CONFLICT", "此操作编号已用于不同的底稿内容。", "请先回查已有底稿，勿重复提交。"),
        "finance_version_conflict": ("FINANCE_VERSION_CONFLICT", "对账底稿版本已变化。", "请回查最新版本并核对后再操作。"),
        "memory_version_conflict": ("MEMORY_VERSION_CONFLICT", "个人记忆版本已变化，请刷新后重试。", None),
        "approval_expired": ("APPROVAL_EXPIRED", "审批请求已过期，不能裁决。", "请重新提交审批请求。"),
        "approval_not_expired": ("APPROVAL_NOT_EXPIRED", "审批请求尚未到期。", None),
        "approval_self_decision": ("APPROVAL_SELF_DECISION", "申请人不能裁决自己的审批请求。", "请由其他获授权人员处理。"),
        "approval_cancel_forbidden": ("APPROVAL_CANCEL_FORBIDDEN", "只有申请人可以取消此审批请求。", None),
        "approval_invalid": ("APPROVAL_INVALID", "审批请求内容无效。", "请检查请求内容。"),
        "approval_expiry_invalid": ("APPROVAL_EXPIRY_INVALID", "审批请求有效期无效。", "有效期必须晚于当前时间。"),
        "approval_expiry_timezone_required": ("APPROVAL_EXPIRY_TIMEZONE_REQUIRED", "审批有效期必须包含时区。", None),
        "approval_time_timezone_required": ("APPROVAL_TIME_TIMEZONE_REQUIRED", "审批时间必须包含时区。", None),
        "task_conflict": ("TASK_CONFLICT", "任务状态已发生变化。", "请刷新任务后重试。"),
        "task_not_found": ("TASK_NOT_FOUND", "任务不存在或不属于当前门店。", None),
        "task_self_accept_forbidden": ("TASK_SELF_ACCEPT_FORBIDDEN", "建议创建人不能接受自己的建议。", "请由其他获权负责人审核。"),
        "task_reviewer_required": ("TASK_REVIEWER_REQUIRED", "只有指定负责人可以裁决此建议。", None),
        "task_reviewer_invalid": ("TASK_REVIEWER_INVALID", "指定负责人在当前门店不可用或没有裁决授权。", None),
        "task_agent_actor_required": ("TASK_AGENT_ACTOR_REQUIRED", "此入口仅接受 Agent 建议。", None),
        "task_decision_invalid": ("TASK_DECISION_INVALID", "任务裁决内容无效。", None),
        "task_supplement_required": ("TASK_SUPPLEMENT_REQUIRED", "请提供补充资料引用后再接受建议。", None),
        "task_assignment_incomplete": ("TASK_ASSIGNMENT_INCOMPLETE", "责任人与截止时间须同时提供。", None),
        "task_assignee_required": ("TASK_ASSIGNEE_REQUIRED", "只有当前责任人可以开始此任务。", None),
        "task_self_verify_forbidden": ("TASK_SELF_VERIFY_FORBIDDEN", "责任人不能自行核验任务。", "请由其他获权人员核验。"),
        "task_assignee_invalid": ("TASK_ASSIGNEE_INVALID", "指定责任人不存在或已停用。", "请指定本组织内的有效员工。"),
        "task_due_date_invalid": ("TASK_DUE_DATE_INVALID", "任务期限必须晚于当前时间。", None),
        "task_invalid": ("TASK_INVALID", "任务内容或原因无效。", "请检查必填字段。"),
        "task_time_timezone_required": ("TASK_TIME_TIMEZONE_REQUIRED", "任务时间必须包含有效时区。", None),
        "task_subject_type_unsupported": ("TASK_SUBJECT_TYPE_UNSUPPORTED", "当前身份类型不能提出任务建议。", "请使用受支持的人员或 Agent 身份。"),
        "task_human_actor_required": ("TASK_HUMAN_ACTOR_REQUIRED", "此任务流转必须由人员身份执行。", "Agent 只能提出待处理建议。"),
        "approval_person_required": ("APPROVAL_PERSON_REQUIRED", "此审批操作仅限人员身份执行。", "请使用具有相应权限的员工账号。"),
        "audit_person_required": ("AUDIT_PERSON_REQUIRED", "审计记录仅限人员身份读取。", None),
        "audit_cursor_invalid": ("AUDIT_CURSOR_INVALID", "审计分页游标无效。", "请从当前门店审计记录重新开始分页。"),
    }
    code_map.update({
        key: (key.upper(), message, hint) for key, message, hint in (
            ("erp_sandbox_disabled", "隔离测试发送尚未启用。", "已发送记录仍可由有权限的人回查。"),
            ("erp_sandbox_unconfigured", "隔离测试适配器尚未配置。", None),
            ("erp_sandbox_permission_denied", "当前身份没有此隔离测试操作权限。", None),
            ("erp_sandbox_human_required", "此操作需要有效员工会话。", None),
            ("erp_sandbox_not_found", "当前门店不存在此隔离测试记录。", None),
            ("erp_sandbox_command_not_found", "当前门店不存在此命令。", None),
            ("erp_sandbox_idempotency_conflict", "此幂等标识已绑定其他请求。", None),
            ("erp_sandbox_intent_bound", "此命令已绑定隔离执行记录。", "读取原记录，不要重新发送。"),
            ("erp_sandbox_binding_changed", "原审批内容或凭证已变化。", "重新核对原命令。"),
            ("erp_sandbox_approval_invalid", "原审批或执行人绑定无效。", None),
            ("erp_sandbox_approval_expired", "原审批已过期。", None),
            ("erp_sandbox_authorization_changed", "原执行授权已失效。", "已发送记录只做回查，不要重放。"),
            ("erp_sandbox_target_changed", "目标数据已变化。", "重新核对原命令。"),
            ("erp_sandbox_not_dispatched", "此记录尚未登记发送。", None),
            ("erp_sandbox_receipt_invalid", "回查凭证与原记录不匹配。", "保留未知状态并人工核对。"),
            ("erp_sandbox_receipt_conflict", "回查凭证与已保存事实冲突。", "保留原事实并人工核对。"),
        )
    })
    # Preserve bounded ERP domain codes. A business object missing from this
    # scope is not a missing HTTP route, and self-approval is not generic403.
    code_map.update({key: (key.upper(), message, hint) for key, message, hint in (
        ("erp_permission_denied", "当前员工没有此ERP命令操作权限。", None),
        ("erp_human_session_required", "此ERP操作需要有效员工会话。", None),
        ("erp_self_approval_denied", "不能审批自己提出的ERP命令。", "请交由其他有权限的员工核对。"),
        ("erp_executor_separation_denied", "执行人须独立于请求人和审批人。", "请由第三位有权限的员工执行。"),
        ("erp_request_not_found", "当前门店没有可读取的ERP命令。", None),
        ("erp_object_not_found", "当前门店没有此ERP命令的来源对象。", None),
        ("erp_approval_expired", "原ERP审批已过期。", "刷新核对；已发送或未知结果先回查。"),
        ("erp_approval_missing", "尚无匹配的有效ERP审批。", "请先取得独立人工审批。"),
        ("erp_approval_consumed", "原ERP审批已绑定既有操作。", "读取原命令，不要重复发送。"),
        ("erp_approval_window_invalid", "ERP命令的审批有效期不符合要求。", None),
        ("erp_request_digest_changed", "所见ERP命令内容已变化。", "刷新原命令后重新核对。"),
        ("erp_object_changed", "ERP命令的来源内容已变化。", "重新核对来源并审批。"),
        ("erp_object_version_changed", "ERP命令的来源版本已变化。", "刷新来源后重新核对。"),
        ("erp_object_inactive", "ERP命令的来源对象已失效。", None),
        ("erp_idempotency_conflict", "此ERP操作标识已绑定不同内容。", "读取原记录，不要更换标识重发未知操作。"),
        ("erp_intent_missing", "尚无匹配的ERP执行意图。", None),
        ("erp_outcome_immutable", "已保存的ERP结果不能覆盖。", "保留原事实并人工核对。"),
        ("erp_external_receipt_verification_unavailable", "尚无正式ERP回执核验能力。", "不能将人工声明当作正式成功。"),
        ("erp_sales_entry_required", "普通销售命令须通过受控销售入口建立。", None),
        ("erp_receipt_entry_required", "入库命令须通过受控收货入口建立。", None),
        ("erp_catalog_entry_required", "商品维护须通过逐动作人审入口办理。", None),
        ("erp_action_unsupported", "此ERP动作尚未登记。", None),
        ("erp_parameters_invalid", "ERP命令参数不符合已登记契约。", None),
        ("erp_field_not_allowed", "此ERP字段不在允许范围。", None),
        ("erp_time_invalid", "ERP命令时间必须包含有效时区。", None),
    )})
    if exc.status_code == 404 and detail not in code_map:
        code, message, hint = "NOT_FOUND", "请求的接口不存在。", "请检查请求路径。"
        error = "route_not_found"
    else:
        code, message, hint = code_map.get(detail, (f"HTTP_{exc.status_code}", "请求无法处理。", None))
        error = detail if detail in code_map else "http_error"
    body = ApiEnvelope(success=False, data=None, code=code, error=error, user_message=message, hint=hint)
    headers = dict(exc.headers or {})
    if _request.url.path.startswith("/api/v1/auth"):
        headers["Cache-Control"] = "no-store"
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(), headers=headers)



async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    body = ApiEnvelope(success=False, data=None, code="VALIDATION_ERROR", error="invalid_request", user_message="请求参数不符合要求。", hint="请检查输入内容后重试。")
    headers = {"Cache-Control": "no-store"} if _request.url.path.startswith(("/api/v1/auth", "/api/v1/office/exceptions", "/api/v1/office/schedules", "/api/v1/erp/sales", "/api/v1/erp/receiving", "/api/v1/erp/catalog-maintenance", "/api/v1/erp/commands")) else None
    return JSONResponse(status_code=422, content=body.model_dump(), headers=headers)



async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    body = ApiEnvelope(success=False, data=None, code="INTERNAL_ERROR", error="internal_error", user_message="服务暂时无法处理请求。", hint="请稍后重试；如问题持续，请联系管理员。")
    headers = {"Cache-Control": "no-store"} if _request.url.path.startswith(("/api/v1/auth", "/api/v1/office/exceptions", "/api/v1/office/schedules", "/api/v1/erp/sales", "/api/v1/erp/receiving", "/api/v1/erp/catalog-maintenance", "/api/v1/erp/commands")) else None
    return JSONResponse(status_code=500, content=body.model_dump(), headers=headers)



async def sales_error_handler(request: Request, exc: SalesError) -> JSONResponse:
    status = 403 if exc.code == "sales_permission_denied" else 409 if exc.code == "sales_version_conflict" else 400
    return await http_error_handler(request, HTTPException(status, exc.code))


def install_application(app: FastAPI) -> None:
    app.include_router(v1_router)
    app.include_router(office_intake_router, prefix="/api/v1")
    app.include_router(office_extraction_router, prefix="/api/v1")
    app.include_router(office_rules_router, prefix="/api/v1")
    app.include_router(erp_commands_router, prefix="/api/v1")
    app.include_router(erp_sandbox_router, prefix="/api/v1")
    app.include_router(office_workflow_router, prefix="/api/v1")
    app.include_router(office_exception_router, prefix="/api/v1")
    app.include_router(office_schedule_router, prefix="/api/v1")
    app.include_router(erp_sales_router, prefix="/api/v1")
    app.state.erp_sales_adapters = {}
    app.include_router(erp_receipt_router, prefix="/api/v1")
    app.state.erp_receipt_adapters = {}
    app.include_router(erp_catalog_router, prefix="/api/v1")
    app.state.erp_catalog_adapters = {}
    app.include_router(erp_settlement_router, prefix="/api/v1")
    app.include_router(office_documents_router, prefix="/api/v1")
    app.include_router(office_finance_router, prefix="/api/v1")
    app.include_router(office_review_router, prefix="/api/v1")
    app.add_exception_handler(AgentRuntimeError, agent_error_handler)
    app.add_exception_handler(WorkflowError, workflow_error_handler)
    app.add_exception_handler(ERPSalesError, erp_sales_error_handler)
    app.add_exception_handler(ERPReceiptError, erp_receipt_error_handler)
    app.add_exception_handler(ERPCatalogError, erp_catalog_error_handler)
    app.add_exception_handler(ScheduleError, office_schedule_error_handler)
    app.add_exception_handler(OfficeExceptionError, office_exception_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
    app.add_exception_handler(SalesError, sales_error_handler)
