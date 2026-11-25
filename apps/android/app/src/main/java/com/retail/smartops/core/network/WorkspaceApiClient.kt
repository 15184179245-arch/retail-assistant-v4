package com.retail.smartops.core.network

import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.ui.StatusKind
import java.io.BufferedReader
import java.io.Closeable
import java.io.IOException
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Reference to a Work Object (either a source domain object or a workspace Artifact).
 */
data class WorkObjectRef(
    val kind: String,
    val objectId: String,
    val title: String? = null,
    val artifactId: String? = null,
    val version: Int? = null,
    val sourceRoute: String? = null,
) {
    fun toWireMap(): Map<String, Any?> = buildMap {
        put("kind", kind)
        put("object_id", objectId)
        title?.let { put("title", it) }
        artifactId?.let { put("artifact_id", it) }
        version?.let { put("version", it) }
        sourceRoute?.let { put("source_route", it) }
    }
}

/**
 * Cross-device thread workspace binding (`WSP-001` / C2C-140 §2.6).
 */
data class ThreadWorkspace(
    val threadId: String,
    val organizationId: String,
    val storeId: String,
    val subjectId: String,
    val openObjects: List<WorkObjectRef>,
    val activeObjectRef: WorkObjectRef?,
    val version: Int,
    val updatedAt: String? = null,
    val activeEditorHint: String? = null,
)

/**
 * AI suggestion created when an agent attempts to patch a human-locked field.
 */
data class PendingSuggestion(
    val field: String,
    val suggestedValue: Any?,
    val reason: String?,
    val opId: String,
    val createdAt: String? = null,
)

/**
 * Append-only operation log record for an Artifact (`WSP-001`).
 */
data class ArtifactOpRecord(
    val id: String,
    val artifactId: String,
    val versionBefore: Int,
    val versionAfter: Int,
    val actorType: String,
    val actorSubjectId: String,
    val opType: String,
    val patch: Map<String, Any?> = emptyMap(),
    val appliedFields: List<String> = emptyList(),
    val skippedFields: List<String> = emptyList(),
    val undoOfOpId: String? = null,
    val idempotencyKey: String? = null,
    val createdAt: String? = null,
    val message: String? = null,
)

/**
 * L1 Draft-layer Work Object Artifact (`WSP-001` / C2C-140 §2.2).
 */
data class WorkspaceArtifact(
    val id: String,
    val organizationId: String,
    val storeId: String,
    val ownerSubjectId: String,
    val threadId: String? = null,
    val kind: String,
    val title: String,
    val status: String,
    val version: Int,
    val payload: Map<String, Any?> = emptyMap(),
    val sourceRefs: List<Map<String, Any?>> = emptyList(),
    val lockedFields: List<String> = emptyList(),
    val aiModifiedFields: List<String> = emptyList(),
    val pendingSuggestions: List<PendingSuggestion> = emptyList(),
    val submittedReceiptRef: String? = null,
    val createdAt: String? = null,
    val updatedAt: String? = null,
    val lastOp: ArtifactOpRecord? = null,
    val skippedFields: List<String> = emptyList(),
    val message: String? = null,
    val activeEditorNotice: String? = null,
)

enum class L2ConfirmationStatus {
    VERIFIED,
    SETTLED,
    WAITING,
    UNVERIFIED,
    CONFLICT,
    FORBIDDEN,
    OFFLINE,
    ERROR,
}

/**
 * Result of an L2 formal action confirmation (`C2C-140` §2.2 & §5.1).
 * Timeout/unknown outcomes MUST map to `UNVERIFIED` (`? 待核实`) with zero auto-resend.
 */
data class L2ConfirmationResult(
    val status: L2ConfirmationStatus,
    val statusKind: StatusKind,
    val idempotencyKey: String,
    val receiptRef: String? = null,
    val artifact: WorkspaceArtifact? = null,
    val message: String,
    val hint: String? = null,
    val requiresStatusCheck: Boolean = false,
)

/**
 * Cursor-based SSE event from `/api/v1/workspace/threads/{threadId}/events`.
 */
data class WorkspaceSseEvent(
    val cursor: Long,
    val eventType: String,
    val threadId: String,
    val artifactId: String? = null,
    val actorType: String? = null,
    val actorSubjectId: String? = null,
    val deviceType: String? = null,
    val payload: Map<String, Any?> = emptyMap(),
    val timestamp: String? = null,
)

interface SseStreamSession : Closeable {
    fun readNextLine(): String?
}

interface SseStreamTransport {
    @Throws(IOException::class)
    fun open(path: String, token: String): SseStreamSession
}

class UrlConnectionSseTransport(private val baseUrl: String) : SseStreamTransport {
    override fun open(path: String, token: String): SseStreamSession {
        val connection = (URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 10_000
            readTimeout = 30_000
            setRequestProperty("Accept", "text/event-stream")
            setRequestProperty("Cache-Control", "no-cache")
            setRequestProperty("Authorization", "Bearer $token")
        }
        val code = connection.responseCode
        if (code !in 200..299) {
            connection.disconnect()
            throw IOException("SSE connection failed with HTTP $code")
        }
        val reader = BufferedReader(InputStreamReader(connection.inputStream, StandardCharsets.UTF_8))
        return object : SseStreamSession {
            override fun readNextLine(): String? = reader.readLine()
            override fun close() {
                runCatching { reader.close() }
                runCatching { connection.disconnect() }
            }
        }
    }
}

/**
 * Workspace & L1/L2 Co-Editing API Client (`WSP-001` / `WSP-002`).
 * All authenticated calls respect `SessionRepository` request scopes and never auto-resend unknown L2 outcomes.
 */
class WorkspaceApiClient(
    private val api: HttpApiClient,
    private val sessions: SessionRepository,
    private val sseTransport: SseStreamTransport? = null,
    private val json: JsonAdapter = OrgJsonAdapter(),
    private val baseUrl: String? = null,
) {
    fun getThreadWorkspace(expected: Session, threadId: String): ApiResult<ThreadWorkspace> =
        scopedCall(expected, "GET", "/api/v1/workspace/threads/${encodeSegment(threadId)}") { data ->
            parseThreadWorkspace(data) ?: throw IllegalArgumentException("Invalid ThreadWorkspace")
        }

    fun updateThreadWorkspace(
        expected: Session,
        threadId: String,
        openObjects: List<WorkObjectRef>,
        activeObjectRef: WorkObjectRef?,
        baseVersion: Int,
        device: String = "android",
    ): ApiResult<ThreadWorkspace> = scopedCall(
        expected = expected,
        method = "PUT",
        path = "/api/v1/workspace/threads/${encodeSegment(threadId)}",
        body = mapOf(
            "open_objects" to openObjects.map { it.toWireMap() },
            "active_object_ref" to activeObjectRef?.toWireMap(),
            "base_version" to baseVersion,
            "device" to device,
        ),
    ) { data ->
        parseThreadWorkspace(data) ?: throw IllegalArgumentException("Invalid ThreadWorkspace")
    }

    fun listArtifacts(
        expected: Session,
        threadId: String? = null,
    ): ApiResult<List<WorkspaceArtifact>> {
        val query = threadId?.takeIf { it.isNotBlank() }?.let { "?thread_id=${encodeSegment(it)}" }.orEmpty()
        return scopedCall(expected, "GET", "/api/v1/workspace/artifacts$query") { data ->
            val items = data["items"] as? List<*> ?: emptyList<Any?>()
            items.mapNotNull { item ->
                @Suppress("UNCHECKED_CAST")
                (item as? Map<String, Any?>)?.let { parseArtifact(it) }
            }
        }
    }

    fun createArtifact(
        expected: Session,
        threadId: String?,
        kind: String,
        title: String,
        payload: Map<String, Any?>,
        sourceRefs: List<Map<String, Any?>> = emptyList(),
        idempotencyKey: String? = null,
    ): ApiResult<WorkspaceArtifact> {
        val headers = idempotencyKey?.takeIf { it.isNotBlank() }?.let { mapOf("Idempotency-Key" to it) } ?: emptyMap()
        return scopedCall(
            expected = expected,
            method = "POST",
            path = "/api/v1/workspace/artifacts",
            body = buildMap {
                put("kind", kind)
                put("title", title)
                put("payload", payload)
                put("source_refs", sourceRefs)
                threadId?.let { put("thread_id", it) }
                idempotencyKey?.takeIf { it.isNotBlank() }?.let { put("idempotency_key", it) }
            },
            headers = headers,
        ) { data ->
            parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
        }
    }

    fun getArtifact(expected: Session, artifactId: String): ApiResult<WorkspaceArtifact> =
        scopedCall(expected, "GET", "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}") { data ->
            parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
        }

    fun patchArtifact(
        expected: Session,
        artifactId: String,
        baseVersion: Int,
        patch: Map<String, Any?>,
        actorType: String = "human",
        reason: String? = null,
        title: String? = null,
        idempotencyKey: String? = null,
    ): ApiResult<WorkspaceArtifact> {
        val headers = idempotencyKey?.takeIf { it.isNotBlank() }?.let { mapOf("Idempotency-Key" to it) } ?: emptyMap()
        return scopedCall(
            expected = expected,
            method = "PATCH",
            path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}",
            body = buildMap {
                put("base_version", baseVersion)
                put("patch", patch)
                put("actor_type", actorType)
                reason?.takeIf { it.isNotBlank() }?.let { put("reason", it) }
                title?.takeIf { it.isNotBlank() }?.let { put("title", it) }
                idempotencyKey?.takeIf { it.isNotBlank() }?.let { put("idempotency_key", it) }
            },
            headers = headers,
        ) { data ->
            if (idempotencyKey.isNullOrBlank()) {
                parseFreshL1Mutation(data, expected, artifactId, baseVersion, "patch", actorType = actorType)
            } else {
                parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
            }
        }
    }

    fun undoArtifactOp(
        expected: Session,
        artifactId: String,
        targetOpId: String,
        baseVersion: Int,
        idempotencyKey: String? = null,
    ): ApiResult<WorkspaceArtifact> {
        val headers = idempotencyKey?.takeIf { it.isNotBlank() }?.let { mapOf("Idempotency-Key" to it) } ?: emptyMap()
        return scopedCall(
            expected = expected,
            method = "POST",
            path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/undo",
            body = buildMap {
                put("target_op_id", targetOpId)
                put("base_version", baseVersion)
                idempotencyKey?.takeIf { it.isNotBlank() }?.let { put("idempotency_key", it) }
            },
            headers = headers,
        ) { data ->
            if (idempotencyKey.isNullOrBlank()) {
                parseFreshL1Mutation(data, expected, artifactId, baseVersion, "undo", targetOpId = targetOpId)
            } else {
                parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
            }
        }
    }

    fun lockField(
        expected: Session,
        artifactId: String,
        field: String,
        baseVersion: Int,
    ): ApiResult<WorkspaceArtifact> = scopedCall(
        expected = expected,
        method = "POST",
        path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/lock",
        body = mapOf("fields" to listOf(field), "base_version" to baseVersion),
    ) { data ->
        parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
    }

    fun unlockField(
        expected: Session,
        artifactId: String,
        field: String,
        baseVersion: Int,
    ): ApiResult<WorkspaceArtifact> = scopedCall(
        expected = expected,
        method = "POST",
        path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/unlock",
        body = mapOf("field" to field, "fields" to listOf(field), "base_version" to baseVersion),
    ) { data ->
        parseFreshL1Mutation(data, expected, artifactId, baseVersion, "unlock")
    }

    fun acceptSuggestion(
        expected: Session,
        artifactId: String,
        opId: String,
        field: String,
        baseVersion: Int,
    ): ApiResult<WorkspaceArtifact> = scopedCall(
        expected = expected,
        method = "POST",
        path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/suggestions/${encodeSegment(opId)}/accept",
        body = mapOf(
            "field" to field,
            "base_version" to baseVersion,
        ),
    ) { data ->
        parseFreshL1Mutation(data, expected, artifactId, baseVersion, "accept_suggestion", targetOpId = opId)
    }

    fun ignoreSuggestion(
        expected: Session,
        artifactId: String,
        opId: String,
        field: String,
        baseVersion: Int,
    ): ApiResult<WorkspaceArtifact> = scopedCall(
        expected = expected,
        method = "POST",
        path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/suggestions/${encodeSegment(opId)}/ignore",
        body = mapOf(
            "field" to field,
            "base_version" to baseVersion,
        ),
    ) { data ->
        parseFreshL1Mutation(data, expected, artifactId, baseVersion, "ignore_suggestion", targetOpId = opId)
    }

    fun resolveBlocks(
        expected: Session,
        blocks: List<Map<String, Any?>>,
        threadId: String? = null,
    ): ApiResult<List<Map<String, Any?>>> = scopedCall(
        expected = expected,
        method = "POST",
        path = "/api/v1/workspace/blocks/resolve",
        body = buildMap {
            put("blocks", blocks)
            threadId?.takeIf { it.isNotBlank() }?.let { put("thread_id", it) }
        },
    ) { data ->
        @Suppress("UNCHECKED_CAST")
        val rawList = data["blocks"] as? List<*>
        if (rawList != null) {
            @Suppress("UNCHECKED_CAST")
            rawList.mapNotNull { it as? Map<String, Any?> }
        } else {
            @Suppress("UNCHECKED_CAST")
            val single = data["block"] as? Map<String, Any?>
            listOfNotNull(single)
        }
    }

    fun pinBlockToArtifact(
        expected: Session,
        threadId: String?,
        blockJson: Map<String, Any?>,
        title: String? = null,
        idempotencyKey: String? = null,
    ): ApiResult<WorkspaceArtifact> {
        val headers = idempotencyKey?.takeIf { it.isNotBlank() }?.let { mapOf("Idempotency-Key" to it) } ?: emptyMap()
        return scopedCall(
            expected = expected,
            method = "POST",
            path = "/api/v1/workspace/blocks/pin",
            body = buildMap {
                put("block", blockJson)
                threadId?.let { put("thread_id", it) }
                title?.let { put("title", it) }
                idempotencyKey?.takeIf { it.isNotBlank() }?.let { put("idempotency_key", it) }
            },
            headers = headers,
        ) { data ->
            parseArtifact(data) ?: throw IllegalArgumentException("Invalid Artifact")
        }
    }

    /**
     * L2 formal submission with mandatory idempotency key and strict timeout -> UNVERIFIED handling.
     * NEVER auto-resends on timeout or unknown network outcome.
     */
    fun submitL2Confirmation(
        expected: Session,
        artifactId: String,
        baseVersion: Int,
        idempotencyKey: String = UUID.randomUUID().toString(),
        reason: String = "",
        online: Boolean = true,
    ): L2ConfirmationResult {
        if (!online) {
            return L2ConfirmationResult(
                status = L2ConfirmationStatus.OFFLINE,
                statusKind = StatusKind.UNVERIFIED,
                idempotencyKey = idempotencyKey,
                message = "待网络：当前离线，禁止完成 L2 正式确认",
                hint = "请恢复网络连接后再点确认提交；离线本地草稿已保留。",
                requiresStatusCheck = false,
            )
        }
        if (idempotencyKey.isBlank()) {
            return L2ConfirmationResult(
                status = L2ConfirmationStatus.ERROR,
                statusKind = StatusKind.ERROR,
                idempotencyKey = idempotencyKey,
                message = "缺少幂等键，已阻止 L2 正式提交",
                hint = "请刷新动作卡以重新生成幂等键。",
            )
        }

        val response = try {
            scopedCall(
                expected = expected,
                method = "POST",
                path = "/api/v1/workspace/artifacts/${encodeSegment(artifactId)}/submit",
                body = buildMap {
                    put("base_version", baseVersion)
                    put("idempotency_key", idempotencyKey)
                    if (reason.isNotBlank()) put("note", reason.trim())
                },
                headers = mapOf("Idempotency-Key" to idempotencyKey),
            ) { data -> data }
        } catch (_: SocketTimeoutException) {
            return unverifiedTimeoutOutcome(idempotencyKey)
        } catch (_: IOException) {
            return unverifiedTimeoutOutcome(idempotencyKey)
        }

        return when (response) {
            is ApiResult.Success -> {
                val raw = response.data
                val artifact = parseArtifact(raw)
                val receiptRef = (raw["submitted_receipt_ref"] as? String)
                    ?: (raw["receipt_ref"] as? String)
                    ?: artifact?.submittedReceiptRef
                val outcomeState = (raw["outcome_status"] as? String)
                    ?: (raw["status_kind"] as? String)
                    ?: artifact?.status
                when {
                    outcomeState == "unverified" || outcomeState == "unknown" ->
                        unverifiedTimeoutOutcome(idempotencyKey, receiptRef, artifact)
                    outcomeState == "waiting" ->
                        L2ConfirmationResult(
                            status = L2ConfirmationStatus.WAITING,
                            statusKind = StatusKind.WAITING,
                            idempotencyKey = idempotencyKey,
                            receiptRef = receiptRef,
                            artifact = artifact,
                            message = "等待审批或后台处理",
                            hint = "请查看审批队列或稍后查询实际状态。",
                            requiresStatusCheck = true,
                        )
                    outcomeState == "settled" ->
                        L2ConfirmationResult(
                            status = L2ConfirmationStatus.SETTLED,
                            statusKind = StatusKind.SETTLED,
                            idempotencyKey = idempotencyKey,
                            receiptRef = receiptRef,
                            artifact = artifact,
                            message = "已收到外部回执，尚未完成三方核对",
                            hint = "回执凭据：${receiptRef ?: "已记录"}",
                        )
                    else ->
                        L2ConfirmationResult(
                            status = L2ConfirmationStatus.VERIFIED,
                            statusKind = StatusKind.VERIFIED,
                            idempotencyKey = idempotencyKey,
                            receiptRef = receiptRef,
                            artifact = artifact,
                            message = "已核验并写入正式回执",
                            hint = receiptRef?.let { "回执号：$it" },
                        )
                }
            }
            is ApiResult.Failure -> when {
                response.isConflict -> L2ConfirmationResult(
                    status = L2ConfirmationStatus.CONFLICT,
                    statusKind = StatusKind.ERROR,
                    idempotencyKey = idempotencyKey,
                    message = "版本冲突 (409)：${response.userMessage}",
                    hint = response.hint ?: "请先刷新对象对比最新改动，再决定是否重新确认。",
                )
                response.isForbidden -> L2ConfirmationResult(
                    status = L2ConfirmationStatus.FORBIDDEN,
                    statusKind = StatusKind.ERROR,
                    idempotencyKey = idempotencyKey,
                    message = "权限不足 (403)：${response.userMessage}",
                    hint = response.hint,
                )
                response.isOffline || response.status in setOf(502, 503, 504) ||
                    response.code in setOf("TIMEOUT", "GATEWAY_TIMEOUT", "REQUEST_UNKNOWN", "NETWORK_ERROR") ->
                    unverifiedTimeoutOutcome(idempotencyKey)
                else -> L2ConfirmationResult(
                    status = L2ConfirmationStatus.ERROR,
                    statusKind = StatusKind.ERROR,
                    idempotencyKey = idempotencyKey,
                    message = response.userMessage,
                    hint = response.hint,
                )
            }
            ApiResult.Cancelled -> L2ConfirmationResult(
                status = L2ConfirmationStatus.UNVERIFIED,
                statusKind = StatusKind.UNVERIFIED,
                idempotencyKey = idempotencyKey,
                message = "会话已切换或请求取消，结果未知，请重新核对原记录",
                requiresStatusCheck = true,
            )
        }
    }

    /**
     * Read-only status reconciliation entry for an UNVERIFIED L2 confirmation.
     * Checks the Artifact / receipt without ever resending the mutation.
     */
    fun checkL2ActualStatus(
        expected: Session,
        artifactId: String,
        idempotencyKey: String,
    ): L2ConfirmationResult {
        return when (val result = getArtifact(expected, artifactId)) {
            is ApiResult.Success -> {
                val artifact = result.data
                val receipt = artifact.submittedReceiptRef
                val matchedOp = artifact.lastOp?.idempotencyKey == idempotencyKey
                if (!receipt.isNullOrBlank() || (artifact.status == "submitted" && matchedOp)) {
                    L2ConfirmationResult(
                        status = L2ConfirmationStatus.VERIFIED,
                        statusKind = StatusKind.VERIFIED,
                        idempotencyKey = idempotencyKey,
                        receiptRef = receipt,
                        artifact = artifact,
                        message = "已查明实际状态：正式单据已生成",
                        hint = receipt?.let { "回执号：$it" },
                    )
                } else {
                    L2ConfirmationResult(
                        status = L2ConfirmationStatus.UNVERIFIED,
                        statusKind = StatusKind.UNVERIFIED,
                        idempotencyKey = idempotencyKey,
                        artifact = artifact,
                        message = "尚未发现与幂等键匹配的已落账回执",
                        hint = "若确认未执行，请先刷新工作对象后再由人工决定是否重新发起。",
                        requiresStatusCheck = true,
                    )
                }
            }
            is ApiResult.Failure -> L2ConfirmationResult(
                status = L2ConfirmationStatus.UNVERIFIED,
                statusKind = StatusKind.UNVERIFIED,
                idempotencyKey = idempotencyKey,
                message = "暂无法核对实际状态：${result.userMessage}",
                hint = result.hint,
                requiresStatusCheck = true,
            )
            ApiResult.Cancelled -> L2ConfirmationResult(
                status = L2ConfirmationStatus.UNVERIFIED,
                statusKind = StatusKind.UNVERIFIED,
                idempotencyKey = idempotencyKey,
                message = "会话已失效，无法核对旧状态",
                requiresStatusCheck = false,
            )
        }
    }

    /**
     * Opens a managed SSE cursor stream controller for a thread.
     * Supports cursor-resumed reconnect and immediate disconnect on scope invalidation.
     */
    fun createEventStreamController(
        threadId: String,
        initialCursor: Long = 0L,
        onEvent: (WorkspaceSseEvent) -> Unit,
        onConnectionChanged: (Boolean) -> Unit = {},
    ): WorkspaceEventStreamController = WorkspaceEventStreamController(
        sessions = sessions,
        sseTransport = sseTransport,
        json = json,
        threadId = threadId,
        initialCursor = initialCursor,
        onEvent = onEvent,
        onConnectionChanged = onConnectionChanged,
    )

    private fun unverifiedTimeoutOutcome(
        idempotencyKey: String,
        receiptRef: String? = null,
        artifact: WorkspaceArtifact? = null,
    ) = L2ConfirmationResult(
        status = L2ConfirmationStatus.UNVERIFIED,
        statusKind = StatusKind.UNVERIFIED,
        idempotencyKey = idempotencyKey,
        receiptRef = receiptRef,
        artifact = artifact,
        message = "超时或结果未知（? 待核实）：严禁自动重发",
        hint = "请点击「先查实际状态」核对服务端是否已成功写入。",
        requiresStatusCheck = true,
    )

    private fun <T> scopedCall(
        expected: Session,
        method: String,
        path: String,
        body: Map<String, Any?>? = null,
        headers: Map<String, String> = emptyMap(),
        parser: (Map<String, Any?>) -> T,
    ): ApiResult<T> {
        val (session, scope) = sessions.captureRequest()
            ?: return ApiResult.Failure.Unauthorized(userMessage = "请先登录后操作工作台。")
        if (session != expected) {
            sessions.finishRequest(scope)
            return ApiResult.Cancelled
        }
        var active = false
        val result = try {
            val response = try {
                api.authenticatedJson(method, path, session.accessToken, body, headers)
            } catch (unsupportedMethod: IllegalArgumentException) {
                val resolvedBaseUrl = baseUrl
                if (method in setOf("PUT", "PATCH") && !resolvedBaseUrl.isNullOrBlank()) {
                    executeDirectHttp(resolvedBaseUrl, method, path, session.accessToken, body, headers)
                } else {
                    throw unsupportedMethod
                }
            }
            if (response is ApiResult.Failure && response.status == 401) {
                sessions.handleUnauthorized(scope)
            }
            when (response) {
                is ApiResult.Success -> try {
                    ApiResult.Success(parser(response.data))
                } catch (err: Exception) {
                    ApiResult.Failure.InvalidEnvelope(
                        userMessage = "工作台响应结构无法解析。",
                        hint = err.message,
                    )
                }
                is ApiResult.Failure -> response
                ApiResult.Cancelled -> ApiResult.Cancelled
            }
        } finally {
            active = sessions.finishRequest(scope)
        }
        return if (active) result else ApiResult.Cancelled
    }

    private fun executeDirectHttp(
        rootUrl: String,
        method: String,
        path: String,
        token: String,
        body: Map<String, Any?>?,
        headers: Map<String, String>,
    ): ApiResult<Map<String, Any?>> {
        val rawBody = body?.let(json::encodeObject)
        val connection = (URL(rootUrl.trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 10_000
            readTimeout = 15_000
            doOutput = rawBody != null
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Authorization", "Bearer $token")
            headers.forEach { (key, value) -> setRequestProperty(key, value) }
        }
        return try {
            if (rawBody != null) {
                connection.outputStream.use { it.write(rawBody.toByteArray(StandardCharsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(StandardCharsets.UTF_8)?.use { it.readText() }.orEmpty()
            if (status == 401) {
                return ApiResult.Failure.Unauthorized(userMessage = "登录已失效，请重新登录。")
            }
            val envelope = json.parseObject(text) ?: return ApiResult.Failure.InvalidEnvelope(
                userMessage = "服务端返回格式异常，请稍后重试。",
            )
            if (status !in 200..299 || envelope["success"] != true) {
                return ApiResult.createFailure(
                    status = status,
                    code = envelope["code"] as? String ?: "UNKNOWN",
                    userMessage = envelope["user_message"] as? String ?: "请求失败，请稍后重试。",
                    hint = envelope["hint"] as? String,
                )
            }
            @Suppress("UNCHECKED_CAST")
            val data = envelope["data"] as? Map<String, Any?>
                ?: return ApiResult.Failure.InvalidEnvelope(userMessage = "响应缺少 data 字段。")
            ApiResult.Success(data)
        } catch (_: IOException) {
            ApiResult.Failure.NetworkError(userMessage = "无法连接服务，请检查网络后重试。")
        } finally {
            connection.disconnect()
        }
    }

    private fun encodeSegment(raw: String): String {
        require(raw.isNotBlank()) { "Path segment must not be blank" }
        return URLEncoder.encode(raw, StandardCharsets.UTF_8.name())
    }

    internal fun parseThreadWorkspace(raw: Map<String, Any?>): ThreadWorkspace? {
        val source = (raw["workspace"] as? Map<*, *>) ?: raw
        val threadId = source["thread_id"] as? String ?: return null
        val orgId = source["organization_id"] as? String ?: ""
        val storeId = source["store_id"] as? String ?: ""
        val subjectId = source["subject_id"] as? String ?: ""
        val openList = (source["open_objects"] as? List<*>)
            ?.mapNotNull { parseWorkObjectRef(it as? Map<*, *>) }
            ?: emptyList()
        val activeRef = parseWorkObjectRef(source["active_object_ref"] as? Map<*, *>)
        val version = (source["version"] as? Number)?.toInt() ?: 1
        val updatedAt = source["updated_at"] as? String
        val lastEditorDevice = (source["last_editor_device"] as? String)
            ?: (raw["last_editor_device"] as? String)
        val activeEditorHint = (source["recent_editor_hint"] as? String)
            ?: (raw["recent_editor_hint"] as? String)
            ?: (source["active_editor_hint"] as? String)
            ?: (raw["active_editor_hint"] as? String)
            ?: lastEditorDevice?.takeIf { it.isNotBlank() && it != "android" }?.let { "此对象正在另一端编辑" }
        return ThreadWorkspace(
            threadId = threadId,
            organizationId = orgId,
            storeId = storeId,
            subjectId = subjectId,
            openObjects = openList,
            activeObjectRef = activeRef,
            version = version,
            updatedAt = updatedAt,
            activeEditorHint = activeEditorHint,
        )
    }

    internal fun parseWorkObjectRef(raw: Map<*, *>?): WorkObjectRef? {
        if (raw == null) return null
        val kind = raw["kind"] as? String ?: return null
        val objectId = (raw["object_id"] as? String)
            ?: (raw["artifact_id"] as? String)
            ?: (raw["id"] as? String)
            ?: return null
        return WorkObjectRef(
            kind = kind,
            objectId = objectId,
            title = raw["title"] as? String,
            artifactId = raw["artifact_id"] as? String,
            version = (raw["version"] as? Number)?.toInt(),
            sourceRoute = raw["source_route"] as? String,
        )
    }

    /** The unkeyed L1 writes have one fresh Host CAS operation, not a read or a replay. */
    private fun parseFreshL1Mutation(
        data: Map<String, Any?>,
        expected: Session,
        artifactId: String,
        baseVersion: Int,
        opType: String,
        actorType: String = "human",
        targetOpId: String? = null,
    ): WorkspaceArtifact {
        fun wholeNumber(value: Any?): Long? = when (value) {
            is Int -> value.toLong()
            is Long -> value
            else -> null
        }
        fun Map<*, *>.texts(vararg fields: String) = fields.all { this[it] is String }
        fun Map<*, *>.textList(field: String) = (this[field] as? List<*>)?.all { it is String } == true

        val artifact = requireNotNull(data["artifact"] as? Map<*, *>) { "Missing mutation artifact" }
        val op = requireNotNull(data["op"] as? Map<*, *>) { "Missing mutation operation" }
        val version = wholeNumber(artifact["version"])
        require(artifactId.isNotBlank() && artifact["id"] == artifactId &&
            artifact["organization_id"] == expected.organizationId && artifact["store_id"] == expected.storeId &&
            artifact["owner_subject_id"] == expected.subjectId &&
            artifact.texts("thread_id", "kind", "title", "status", "created_at", "updated_at") &&
            baseVersion > 0 && version != null && version == baseVersion.toLong() + 1 && version <= Int.MAX_VALUE &&
            artifact["payload"] is Map<*, *> && artifact.textList("locked_fields") &&
            (artifact["source_refs"] as? List<*>)?.all { it is Map<*, *> } == true &&
            (artifact["pending_suggestions"] as? List<*>)?.all { item ->
                val suggestion = item as? Map<*, *>
                suggestion != null && suggestion.texts("field", "op_id", "actor_subject_id", "created_at") &&
                    suggestion.containsKey("suggested_value") &&
                    (suggestion["reason"] == null || suggestion["reason"] is String)
            } == true
        ) { "Incomplete or mismatched mutation artifact" }

        require((op["id"] as? String)?.isNotBlank() == true && op["artifact_id"] == artifactId &&
            op["op_type"] == opType && op["actor_type"] == actorType && op["actor_subject_id"] == expected.subjectId &&
            wholeNumber(op["version_before"]) == baseVersion.toLong() && wholeNumber(op["version_after"]) == version &&
            op["patch"] is Map<*, *> && op.textList("applied_fields") && op.textList("skipped_fields") &&
            op["created_at"] is String && (targetOpId == null || op["undo_of_op_id"] == targetOpId) &&
            (!data.containsKey("idempotent_replay") || data["idempotent_replay"] == false)
        ) { "Incomplete or mismatched fresh mutation operation" }

        return requireNotNull(parseArtifact(data)) { "Invalid mutation artifact" }
    }

    @Suppress("UNCHECKED_CAST")
    internal fun parseArtifact(raw: Map<String, Any?>): WorkspaceArtifact? {
        val source = (raw["artifact"] as? Map<String, Any?>) ?: raw
        val id = source["id"] as? String ?: return null
        val kind = source["kind"] as? String ?: return null
        val title = source["title"] as? String ?: ""
        val status = source["status"] as? String ?: "draft"
        val version = (source["version"] as? Number)?.toInt() ?: 1
        val payload = (source["payload"] as? Map<String, Any?>) ?: emptyMap()
        val sourceRefs = (source["source_refs"] as? List<*>)?.mapNotNull { it as? Map<String, Any?> } ?: emptyList()
        val lockedFields = (source["locked_fields"] as? List<*>)?.mapNotNull { it as? String } ?: emptyList()
        val explicitAiModified = (source["ai_modified_fields"] as? List<*>)?.mapNotNull { it as? String }
        val suggestions = (source["pending_suggestions"] as? List<*>)?.mapNotNull { item ->
            val m = item as? Map<*, *> ?: return@mapNotNull null
            val field = m["field"] as? String ?: return@mapNotNull null
            val opId = m["op_id"] as? String ?: ""
            PendingSuggestion(
                field = field,
                suggestedValue = m["suggested_value"],
                reason = m["reason"] as? String,
                opId = opId,
                createdAt = m["created_at"] as? String,
            )
        } ?: emptyList()
        val lastOpMap = (raw["op"] as? Map<*, *>) ?: (source["last_op"] as? Map<*, *>)
        val lastOp = lastOpMap?.let { parseArtifactOp(it) }
        val opsList = (raw["ops"] as? List<*>)?.mapNotNull { (it as? Map<*, *>)?.let(::parseArtifactOp) } ?: emptyList()
        val derivedLastOp = lastOp ?: opsList.lastOrNull()
        val aiModifiedFields = explicitAiModified
            ?: derivedLastOp
                ?.takeIf { it.actorType == "agent" && it.opType == "patch" }
                ?.let { op -> op.appliedFields.ifEmpty { op.patch.keys.toList() } }
                ?.filterNot { it in lockedFields }
            ?: emptyList()
        val skippedFields = (raw["skipped_fields"] as? List<*>)?.mapNotNull { it as? String }
            ?: derivedLastOp?.skippedFields
            ?: emptyList()
        return WorkspaceArtifact(
            id = id,
            organizationId = source["organization_id"] as? String ?: "",
            storeId = source["store_id"] as? String ?: "",
            ownerSubjectId = source["owner_subject_id"] as? String ?: "",
            threadId = source["thread_id"] as? String,
            kind = kind,
            title = title,
            status = status,
            version = version,
            payload = payload,
            sourceRefs = sourceRefs,
            lockedFields = lockedFields,
            aiModifiedFields = aiModifiedFields,
            pendingSuggestions = suggestions,
            submittedReceiptRef = source["submitted_receipt_ref"] as? String,
            createdAt = source["created_at"] as? String,
            updatedAt = source["updated_at"] as? String,
            lastOp = derivedLastOp,
            skippedFields = skippedFields,
            message = (raw["message"] as? String) ?: derivedLastOp?.message,
            activeEditorNotice = (raw["active_editor_notice"] as? String)
                ?: (source["active_editor_notice"] as? String),
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseArtifactOp(raw: Map<*, *>): ArtifactOpRecord? {
        val id = raw["id"] as? String ?: return null
        val artifactId = raw["artifact_id"] as? String ?: ""
        return ArtifactOpRecord(
            id = id,
            artifactId = artifactId,
            versionBefore = (raw["version_before"] as? Number)?.toInt() ?: 0,
            versionAfter = (raw["version_after"] as? Number)?.toInt() ?: 0,
            actorType = raw["actor_type"] as? String ?: "human",
            actorSubjectId = raw["actor_subject_id"] as? String ?: "",
            opType = raw["op_type"] as? String ?: "patch",
            patch = (raw["patch"] as? Map<String, Any?>) ?: emptyMap(),
            appliedFields = (raw["applied_fields"] as? List<*>)?.mapNotNull { it as? String } ?: emptyList(),
            skippedFields = (raw["skipped_fields"] as? List<*>)?.mapNotNull { it as? String } ?: emptyList(),
            undoOfOpId = raw["undo_of_op_id"] as? String,
            idempotencyKey = raw["idempotency_key"] as? String,
            createdAt = raw["created_at"] as? String,
            message = raw["message"] as? String,
        )
    }
}

/**
 * Manages SSE cursor subscription (`?cursor=<lastCursor>`), reconnect after transient drop,
 * and immediate scope-invalidation disconnect so user switches never leak stale events.
 */
class WorkspaceEventStreamController(
    private val sessions: SessionRepository,
    private val sseTransport: SseStreamTransport?,
    private val json: JsonAdapter,
    private val threadId: String,
    initialCursor: Long = 0L,
    private val onEvent: (WorkspaceSseEvent) -> Unit,
    private val onConnectionChanged: (Boolean) -> Unit = {},
) : Closeable {
    private val cursor = AtomicLong(initialCursor)
    private val disposed = AtomicBoolean(false)
    @Volatile
    private var activeSessionStream: SseStreamSession? = null
    @Volatile
    var isConnected: Boolean = false
        private set
    @Volatile
    var reconnectCount: Int = 0
        private set

    private val removeScopeListener = sessions.onScopeInvalidated {
        disconnectAndReset()
    }

    val lastCursor: Long get() = cursor.get()

    /**
     * Reads SSE frames synchronously from the current cursor. On transient disconnect while the
     * session scope remains valid and not disposed, returns false so caller/scheduler can call
     * [reconnect] from [lastCursor].
     */
    fun pumpOnce(expected: Session, maxReconnects: Int = 2): Boolean {
        if (disposed.get()) return false
        val transport = sseTransport ?: return false
        var attempt = 0
        while (!disposed.get() && attempt <= maxReconnects) {
            val (current, scope) = sessions.captureRequest() ?: return false
            if (current != expected || !scope.isActive()) {
                sessions.finishRequest(scope)
                disconnectAndReset()
                return false
            }
            val encodedThread = URLEncoder.encode(threadId, StandardCharsets.UTF_8.name())
            val path = "/api/v1/workspace/threads/$encodedThread/events?cursor=${cursor.get()}"
            var stream: SseStreamSession? = null
            try {
                stream = transport.open(path, current.accessToken)
                activeSessionStream = stream
                isConnected = true
                onConnectionChanged(true)
                consumeStream(stream, scope, expected)
                if (disposed.get() || !scope.isActive() || sessions.currentSession() != expected) {
                    return false
                }
                // Stream closed cleanly by server / EOF -> try cursor resume
                attempt++
                if (attempt <= maxReconnects) {
                    reconnectCount++
                }
            } catch (_: IOException) {
                isConnected = false
                onConnectionChanged(false)
                if (disposed.get() || !scope.isActive() || sessions.currentSession() != expected) {
                    return false
                }
                attempt++
                if (attempt <= maxReconnects) {
                    reconnectCount++
                }
            } finally {
                runCatching { stream?.close() }
                if (activeSessionStream === stream) activeSessionStream = null
                sessions.finishRequest(scope)
            }
        }
        isConnected = false
        onConnectionChanged(false)
        return false
    }

    private fun consumeStream(
        stream: SseStreamSession,
        scope: com.retail.smartops.core.auth.RequestScope,
        expected: Session,
    ) {
        var eventId: String? = null
        var eventName: String? = null
        val dataLines = mutableListOf<String>()

        while (!disposed.get() && scope.isActive() && sessions.currentSession() == expected) {
            val line = stream.readNextLine() ?: break
            when {
                line.isEmpty() -> {
                    if (dataLines.isNotEmpty()) {
                        dispatchFrame(eventId, eventName, dataLines.joinToString("\n"), scope, expected)
                    }
                    eventId = null
                    eventName = null
                    dataLines.clear()
                }
                line.startsWith(":") -> Unit
                line.startsWith("id:") -> eventId = line.removePrefix("id:").trim()
                line.startsWith("event:") -> eventName = line.removePrefix("event:").trim()
                line.startsWith("data:") -> dataLines.add(line.removePrefix("data:").trimStart())
            }
        }
        if (dataLines.isNotEmpty() && !disposed.get() && scope.isActive() && sessions.currentSession() == expected) {
            dispatchFrame(eventId, eventName, dataLines.joinToString("\n"), scope, expected)
        }
    }

    private fun dispatchFrame(
        eventId: String?,
        eventName: String?,
        rawData: String,
        scope: com.retail.smartops.core.auth.RequestScope,
        expected: Session,
    ) {
        if (disposed.get() || !scope.isActive() || sessions.currentSession() != expected) return
        val parsed = json.parseObject(rawData) ?: return
        val frameCursor = (parsed["cursor"] as? Number)?.toLong()
            ?: eventId?.toLongOrNull()
            ?: (cursor.get() + 1L)
        if (frameCursor <= cursor.get()) return
        cursor.set(frameCursor)
        @Suppress("UNCHECKED_CAST")
        val payloadMap = (parsed["payload"] as? Map<String, Any?>) ?: parsed
        val event = WorkspaceSseEvent(
            cursor = frameCursor,
            eventType = eventName ?: (parsed["event_type"] as? String) ?: "workspace.updated",
            threadId = (parsed["thread_id"] as? String) ?: threadId,
            artifactId = parsed["artifact_id"] as? String,
            actorType = parsed["actor_type"] as? String,
            actorSubjectId = parsed["actor_subject_id"] as? String,
            deviceType = parsed["device_type"] as? String,
            payload = payloadMap,
            timestamp = parsed["timestamp"] as? String,
        )
        onEvent(event)
    }

    fun disconnectAndReset() {
        disposed.set(true)
        isConnected = false
        onConnectionChanged(false)
        runCatching { activeSessionStream?.close() }
        activeSessionStream = null
        cursor.set(0L)
    }

    override fun close() {
        removeScopeListener()
        disconnectAndReset()
    }
}
