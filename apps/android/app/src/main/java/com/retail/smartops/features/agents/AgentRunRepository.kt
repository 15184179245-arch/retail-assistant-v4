package com.retail.smartops.features.agents

import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.parseIsoInstant
import java.net.URLEncoder
import java.util.UUID

const val MAX_AGENT_MESSAGE_CHARS = 4_200_000

enum class RunState(val wire: String) {
    CREATED("created"), RUNNING("running"), WAITING_FOR_HUMAN("waiting_for_human"),
    WAITING_FOR_EXTERNAL("waiting_for_external"), COMPLETED("completed"), FAILED("failed"), CANCELLED("cancelled");
    val terminal: Boolean get() = this in setOf(COMPLETED, FAILED, CANCELLED)
}
enum class AnswerStatus { AVAILABLE, NOT_RECORDED, UNAVAILABLE }
data class AnswerCitation(val kind: String, val objectId: String, val versionRef: String,
    val version: String, val label: String, val sourceRef: String?, val observedAt: String?)
data class AgentStep(val id: String, val sequenceNo: Long, val kind: String, val state: String,
    val toolKey: String?, val toolVersion: String?, val errorCode: String?)
data class PendingAction(val toolKey: String, val version: String, val arguments: Map<String, Any?>,
    val inputDigest: String, val expiresAt: String, val runVersion: Long, val stepVersion: Long)
data class AgentRunReceipt(val runId: String, val state: RunState, val answer: String?,
    val answerStatus: AnswerStatus, val answerUnavailableReason: String?, val citations: List<AnswerCitation>,
    val steps: List<AgentStep>, val pendingAction: PendingAction?, val taskId: String?, val errorCode: String?,
    val execution: AgentExecution? = null)
data class CreateRunCommand(val runId: String, val text: String) {
    init { require(UUID.fromString(runId).toString() == runId); require(text.isNotBlank() && text.length <= MAX_AGENT_MESSAGE_CHARS) }
}

interface AgentRunGateway {
    fun create(session: Session, command: CreateRunCommand): ApiResult<AgentRunReceipt>
    fun read(session: Session, runId: String): ApiResult<AgentRunReceipt>
    fun confirm(session: Session, runId: String, pending: PendingAction,
                approve: Boolean, reason: String): ApiResult<AgentRunReceipt>
    fun cancel(session: Session, runId: String): ApiResult<AgentRunReceipt>
}

class AgentRunRepository(private val api: HttpApiClient, private val sessions: SessionRepository) : AgentRunGateway {
    override fun create(session: Session, command: CreateRunCommand) = request(session, "POST", "", command.runId,
        mapOf("text" to command.text, "run_id" to command.runId), mapOf("Idempotency-Key" to command.runId))
    override fun read(session: Session, runId: String) = request(session, "GET", "/${segment(runId)}", runId)
    override fun cancel(session: Session, runId: String) =
        request(session, "POST", "/${segment(runId)}/cancel", runId, emptyMap())
    override fun confirm(session: Session, runId: String, pending: PendingAction,
                         approve: Boolean, reason: String): ApiResult<AgentRunReceipt> {
        require(reason.isNotBlank() && reason.length <= 500)
        return request(session, "POST", "/${segment(runId)}/confirmation", runId, mapOf(
            "decision" to if (approve) "approve" else "reject", "input_digest" to pending.inputDigest,
            "reason" to reason.trim(), "run_version" to pending.runVersion, "step_version" to pending.stepVersion))
    }

    private fun request(expected: Session, method: String, suffix: String, runId: String,
                        body: Map<String, Any?>? = null, headers: Map<String, String> = emptyMap()): ApiResult<AgentRunReceipt> {
        val (session, scope) = sessions.captureRequest() ?: return failure(401, "LOGIN_REQUIRED")
        if (session != expected) { sessions.finishRequest(scope); return ApiResult.Cancelled }
        var active: Boolean
        val result = try {
            val response = api.authenticatedJson(method, "/api/v1/agent-runs$suffix", session.accessToken, body, headers)
            if (response is ApiResult.Failure && response.status == 401) sessions.handleUnauthorized(scope)
            when (response) {
                is ApiResult.Success -> try {
                    val receipt = parseReceipt(response.data, requirePending = method == "GET" || suffix.isEmpty())
                    if (receipt.runId != runId) failure(0, "RUN_ID_MISMATCH") else ApiResult.Success(receipt)
                } catch (_: IllegalArgumentException) { failure(0, "INVALID_RECEIPT") }
                is ApiResult.Failure -> response
                ApiResult.Cancelled -> ApiResult.Cancelled
            }
        } finally { active = sessions.finishRequest(scope) }
        return if (active) result else ApiResult.Cancelled
    }
    private fun segment(value: String): String {
        require(value.matches(Regex("[A-Za-z0-9][A-Za-z0-9_.:-]{0,35}")))
        return URLEncoder.encode(value, "UTF-8")
    }
}

private fun failure(status: Int, code: String) = ApiResult.Failure(status, code, "请核对原运行状态后继续。", null)
private fun Map<*, *>.text(key: String): String = (this[key] as? String)?.takeIf { it.isNotBlank() }
    ?: throw IllegalArgumentException(key)
private fun Map<*, *>.optionalText(key: String): String? {
    val value = this[key] ?: return null
    require(value is String); return value
}
private fun Map<*, *>.positive(key: String): Long {
    val value = this[key]; require(value is Int || value is Long)
    return (value as Number).toLong().also { require(it > 0) }
}
private fun objectList(value: Any?): List<Map<*, *>> {
    require(value is List<*>); return value.map { require(it is Map<*, *>); it }
}
internal fun parseReceipt(data: Map<String, Any?>, requirePending: Boolean = true): AgentRunReceipt {
    val state = RunState.entries.firstOrNull { it.wire == data.text("state") } ?: errorState()
    val status = when (data["answer_status"]) {
        "available" -> AnswerStatus.AVAILABLE
        null, "not_recorded" -> AnswerStatus.NOT_RECORDED
        "unavailable" -> AnswerStatus.UNAVAILABLE
        else -> errorState()
    }
    val visible = state == RunState.COMPLETED && status == AnswerStatus.AVAILABLE
    val answer = if (visible) data.text("answer") else null
    val citations = if (visible) objectList(data["citations"] ?: emptyList<Any>()).map {
        val kind = it.text("kind"); require(kind in setOf("knowledge", "catalog"))
        val time = it.optionalText("observed_at"); require(time == null || parseIsoInstant(time) != null)
        AnswerCitation(kind, it.text("object_id"), it.text("version_ref"), it.text("version"),
            it.text("label"), it.optionalText("source_ref"), time)
    } else emptyList()
    val steps = objectList(data["steps"] ?: emptyList<Any>()).map {
        val kind = it.text("kind"); require(kind in setOf("model", "tool", "checkpoint"))
        val stepState = it.text("state")
        require(stepState in setOf("pending", "running", "succeeded", "failed", "unknown", "cancelled"))
        AgentStep(it.text("id"), it.positive("sequence_no"), kind, stepState,
            it.optionalText("tool_key"), it.optionalText("tool_version"), it.optionalText("error_code"))
    }
    val pending = data["pending_action"]?.let {
        require(state == RunState.WAITING_FOR_HUMAN && it is Map<*, *>)
        val digest = it.text("input_digest"); require(digest.matches(Regex("[0-9a-f]{64}")))
        val expires = it.text("expires_at"); require(parseIsoInstant(expires) != null)
        val arguments = it["arguments"]; require(arguments is Map<*, *> && arguments.keys.all { key -> key is String })
        @Suppress("UNCHECKED_CAST")
        PendingAction(it.text("tool_key"), it.text("version"), (arguments as Map<String, Any?>).toMap(),
            digest, expires, it.positive("run_version"), it.positive("step_version"))
    }
    require(!requirePending || state != RunState.WAITING_FOR_HUMAN || pending != null)
    return AgentRunReceipt(data.text("run_id"), state, answer, status,
        data.optionalText("answer_unavailable_reason"), citations, steps, pending,
        data.optionalText("task_id"), data.optionalText("error_code"), parseExecution(data["execution"], state))
}
private fun errorState(): Nothing = throw IllegalArgumentException("Unknown Agent state")
