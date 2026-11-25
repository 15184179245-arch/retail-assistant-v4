"""Employee-only HTTP entrypoints for bounded Agent runs."""

from datetime import datetime
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.model_context import MAX_MESSAGE_CHARS, ModelContextError
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session, get_auth_session
from backend.app.modules.agents.cost_ledger import CostLedgerError, query_costs
from backend.app.modules.agents.schemas import (
    AgentCostEnvelope, AgentCostPage, AgentReceiptEnvelope, AgentRunReceipt,
    ConfirmationDecision,
    PiRuntimeReceiptEnvelope,
)
from backend.app.modules.agents.service import (
    AgentRuntimeError, cancel_run, create_and_execute, decide_confirmation, read_run_receipt, submit_worker_run,
    read_pi_runtime_receipt,
)

router = APIRouter(prefix="/agent-runs", tags=["agents"])
cost_router = APIRouter(prefix="/agent-costs", tags=["agents"])


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    run_id: str | None = Field(default=None, min_length=1, max_length=36)


def _envelope(data: dict, message: str) -> AgentReceiptEnvelope:
    return AgentReceiptEnvelope(success=True, data=AgentRunReceipt.model_validate(data), code="OK", error=None,
                       user_message=message, hint=None)


@router.post("", response_model=AgentReceiptEnvelope)
async def create_route(payload: AgentRequest,
                       actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                       session: Annotated[Session, Depends(get_session)],
                       key: Annotated[str, Depends(require_idempotency_key)]) -> AgentReceiptEnvelope:
    try:
        if os.environ.get("AGENT_WORKER_SUBMIT_ENABLED") == "1":
            return _envelope(submit_worker_run(session, actor, payload.text, key,
                                               requested_run_id=payload.run_id),
                             "Agent 请求已持久入队，请使用 GET 查询结果。")
        return _envelope(await create_and_execute(session, actor, payload.text, key,
                                                 requested_run_id=payload.run_id),
                         "Agent 运行结果已记录。")
    except ModelContextError as exc:
        raise AgentRuntimeError(exc.code, 503) from None


# A separate opt-in route. No fallback or change to the existing POST.
from backend.app.modules.agents.runtime_http import SdkRequest, submit as submit_sdk

@router.post("/sdk-readonly", response_model=AgentReceiptEnvelope)
def sdk_create_route(payload: SdkRequest, request: Request,
                     actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                     auth_session: Annotated[Session, Depends(get_auth_session)]) -> AgentReceiptEnvelope:
    return _envelope(submit_sdk(request,payload,actor,auth_session), "Agent 请求已记录，请按原 Run 查询持久结果。")


from backend.app.modules.agents.runtime_http import PiRequest, submit_pi


@router.post("/pi-readonly", response_model=AgentReceiptEnvelope)
def pi_create_route(payload: PiRequest, request: Request,
                    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                    auth_session: Annotated[Session, Depends(get_auth_session)]) -> AgentReceiptEnvelope:
    return _envelope(submit_pi(request, payload, actor, auth_session),
                     "Pi 请求已记录，请按原 Run 查询持久结果。")


@router.post("/pi-memory", response_model=AgentReceiptEnvelope)
def pi_memory_create_route(payload: PiRequest, request: Request,
                    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                    auth_session: Annotated[Session, Depends(get_auth_session)]) -> AgentReceiptEnvelope:
    return _envelope(submit_pi(request, payload, actor, auth_session, memory=True),
                     "Pi 请求已记录，请按原 Run 查询持久结果。")


from backend.app.modules.agents.trial_info import PiTrialInfoEnvelope, read_trial_info


@router.get("/pi-trial-info", response_model=PiTrialInfoEnvelope)
def pi_trial_info_route(request: Request,
                        actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                        auth_session: Annotated[Session, Depends(get_auth_session)]) -> PiTrialInfoEnvelope:
    return PiTrialInfoEnvelope(success=True, data=read_trial_info(request, actor, auth_session),
        code="OK", error=None, user_message="已读取 Host 试用配置；不表示模型已执行。", hint=None)


@router.get("/{run_id}", response_model=AgentReceiptEnvelope)
def get_route(run_id: str,
              actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
              session: Annotated[Session, Depends(get_session)]) -> AgentReceiptEnvelope:
    return _envelope(read_run_receipt(session, actor, run_id), "Agent 运行状态已读取。")


@router.get("/{run_id}/runtime-receipt", response_model=PiRuntimeReceiptEnvelope)
def runtime_receipt_route(run_id: str,
                          actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                          session: Annotated[Session, Depends(get_session)]) -> PiRuntimeReceiptEnvelope:
    data = read_pi_runtime_receipt(session, actor, run_id)
    return PiRuntimeReceiptEnvelope(success=True, data=data,
        code="OK", error=None, user_message="已读取同 Run 的 Host 记录；不表示真实模型运行已验证。", hint=None)


def _cancel_message(data: dict) -> str:
    """Describe the durable cancel outcome; never claim a success it lacked."""
    state = data.get("state")
    execution = data.get("execution")
    if execution is None:
        return ("Agent 运行已取消。" if state == "cancelled"
                else "取消意图已记录；运行中的外部操作未必停止。")
    resolution = execution["cancel_resolution"]
    if resolution == "cancelled":
        return "Agent 运行已取消；本次取消未产生新的业务写入。"
    if resolution == "too_late":
        return "取消请求提交较晚：原任务已提交，无法取消，请核对原任务结果。"
    if resolution == "unknown" or execution["effect_status"] == "external_unknown":
        return "取消结果尚未核实，请重新查询原 Run 并核对原任务结果，勿重复提交。"
    if resolution == "requested":
        return "取消请求已记录，正在核对执行结果。"
    return "Agent 运行已取消。" if state == "cancelled" else "Agent 运行已结束。"


@router.post("/{run_id}/cancel", response_model=AgentReceiptEnvelope)
def cancel_route(run_id: str,
                 actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                 session: Annotated[Session, Depends(get_session)]) -> AgentReceiptEnvelope:
    data = cancel_run(session, actor, run_id)
    return _envelope(data, _cancel_message(data))


@router.post("/{run_id}/confirmation", response_model=AgentReceiptEnvelope)
def confirmation_route(run_id: str, payload: ConfirmationDecision,
                       actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                       session: Annotated[Session, Depends(get_session)]) -> AgentReceiptEnvelope:
    result = decide_confirmation(session, actor, run_id, decision=payload.decision,
                                 input_digest=payload.input_digest, reason=payload.reason,
                                 run_version=payload.run_version, step_version=payload.step_version)
    if payload.decision == "approve" and result.get("execution") is not None:
        # The grant is queued; no Task exists until the single worker transaction.
        if result.get("state") == "completed":
            message = "原任务建议已提交，请查看原结果。"
        elif result.get("state") in ("failed", "cancelled"):
            message = "批准已记录，请核对当前运行结果。"
        else:
            message = "批准已记录，等待后台执行。"
    else:
        message = "人工确认结论已记录。"
    return _envelope(result, message)


@cost_router.get("", response_model=AgentCostEnvelope)
def cost_route(
        actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
        session: Annotated[Session, Depends(get_session)],
        run_id: Annotated[str | None, Query(max_length=36)] = None,
        requester_id: Annotated[str | None, Query(max_length=36)] = None,
        from_time: Annotated[datetime | None, Query()] = None,
        to_time: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query()] = 50,
        cursor: Annotated[str | None, Query(max_length=36)] = None,
) -> AgentCostEnvelope:
    try:
        items, next_cursor = query_costs(
            session, actor, run_id=run_id, requester_id=requester_id,
            from_time=from_time, to_time=to_time, limit=limit, cursor=cursor)
    except CostLedgerError as exc:
        raise AgentRuntimeError(exc.code, exc.status_code) from None
    return AgentCostEnvelope(success=True, data=AgentCostPage(
        items=items, next_cursor=next_cursor), code="OK", error=None,
        user_message="费用记录已读取。", hint=None)


from backend.app.modules.agents.schemas import PiToolResultReadEnvelope
from backend.app.modules.agents.service import read_pi_tool_result


@router.get('/{run_id}/tool-results/{tool_step_id}', response_model=PiToolResultReadEnvelope)
def tool_result_route(run_id: str, tool_step_id: str,
                      actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
                      session: Annotated[Session, Depends(get_session)]) -> PiToolResultReadEnvelope:
    return PiToolResultReadEnvelope(success=True,data=read_pi_tool_result(session,actor,run_id,tool_step_id),
        code='OK',error=None,user_message='已读取已提交工具结果的当前可见内容。',hint=None)
