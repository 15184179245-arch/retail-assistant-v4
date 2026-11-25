package com.retail.smartops.features.agents

enum class ExecutionPhase(val wire: String) {
    QUEUED("queued"), CLAIMED("claimed"), MODEL_INFLIGHT("model_inflight"),
    WAITING_FOR_HUMAN("waiting_for_human"), TOOL_INFLIGHT("tool_inflight"),
    RECONCILING("reconciling"), TERMINAL("terminal")
}
enum class CancelResolution(val wire: String) {
    NOT_REQUESTED("not_requested"), REQUESTED("requested"), CANCELLED("cancelled"),
    TOO_LATE("too_late"), UNKNOWN("unknown")
}
enum class EffectStatus(val wire: String) {
    NONE("none"), LOCAL_TASK_COMMITTED("local_task_committed"), EXTERNAL_UNKNOWN("external_unknown")
}
data class AgentExecution(
    val phase: ExecutionPhase,
    val cancelRequested: Boolean,
    val cancelResolution: CancelResolution,
    val effectStatus: EffectStatus,
) {
    val needsReconciliation: Boolean get() = effectStatus == EffectStatus.EXTERNAL_UNKNOWN ||
        cancelResolution == CancelResolution.UNKNOWN || phase == ExecutionPhase.RECONCILING
    val allowsHumanDecision: Boolean get() = phase == ExecutionPhase.WAITING_FOR_HUMAN &&
        !cancelRequested && !needsReconciliation
}

/** Missing metadata is legacy. Present metadata must be the whole v1 contract. */
internal fun parseExecution(value: Any?, state: RunState): AgentExecution? {
    if (value == null) return null
    require(value is Map<*, *>)
    require(value.keys == setOf("protocol_version", "phase", "cancel_requested", "cancel_resolution", "effect_status"))
    val version = value["protocol_version"]
    require((version is Int || version is Long) && (version as Number).toLong() == 1L)
    val phase = ExecutionPhase.entries.firstOrNull { it.wire == value["phase"] }
        ?: throw IllegalArgumentException("execution phase")
    val requested = value["cancel_requested"]
    require(requested is Boolean)
    val resolution = CancelResolution.entries.firstOrNull { it.wire == value["cancel_resolution"] }
        ?: throw IllegalArgumentException("cancel resolution")
    val effect = EffectStatus.entries.firstOrNull { it.wire == value["effect_status"] }
        ?: throw IllegalArgumentException("effect status")
    require(requested == (resolution != CancelResolution.NOT_REQUESTED))
    require(resolution != CancelResolution.CANCELLED ||
        (effect == EffectStatus.NONE && state == RunState.CANCELLED))
    require((phase == ExecutionPhase.TERMINAL) == state.terminal)
    require(phase != ExecutionPhase.WAITING_FOR_HUMAN || state == RunState.WAITING_FOR_HUMAN)
    require(phase !in setOf(ExecutionPhase.MODEL_INFLIGHT, ExecutionPhase.TOOL_INFLIGHT) ||
        state == RunState.RUNNING)
    require(resolution != CancelResolution.TOO_LATE || state == RunState.COMPLETED)
    return AgentExecution(phase, requested, resolution, effect)
}

/** A successful HTTP response reports a durable state, not necessarily completed work. */
fun agentExecutionNotice(receipt: AgentRunReceipt): String? {
    val execution = receipt.execution ?: return null
    return when {
        execution.needsReconciliation -> "执行结果尚未核实；请重新读取原运行并核对，勿重复提交或确认。"
        execution.cancelResolution == CancelResolution.TOO_LATE ->
            if (execution.effectStatus == EffectStatus.LOCAL_TASK_COMMITTED) {
                "任务建议已提交，取消未生效；请查看原任务。"
            } else "运行已经结束，取消未生效；请核对原结果。"
        execution.cancelResolution == CancelResolution.CANCELLED -> "取消已确认，未产生新的业务写入。"
        execution.cancelRequested -> "取消请求已记录，正在核对执行结果。"
        execution.phase == ExecutionPhase.QUEUED && receipt.state == RunState.RUNNING ->
            "批准已记录，等待后台执行；尚未生成任务建议。"
        execution.phase == ExecutionPhase.QUEUED -> "已排队，等待后台处理。"
        execution.phase == ExecutionPhase.WAITING_FOR_HUMAN -> "等待人工确认；批准后仍需后台执行。"
        execution.phase in setOf(ExecutionPhase.CLAIMED, ExecutionPhase.MODEL_INFLIGHT,
                                ExecutionPhase.TOOL_INFLIGHT) -> "后台正在处理任务。"
        execution.effectStatus == EffectStatus.LOCAL_TASK_COMMITTED -> "任务建议已提交，等待负责人审阅。"
        else -> null
    }
}
