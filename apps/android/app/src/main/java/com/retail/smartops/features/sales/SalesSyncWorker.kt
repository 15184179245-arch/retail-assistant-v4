package com.retail.smartops.features.sales

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.retail.smartops.BuildConfig
import com.retail.smartops.core.auth.SecureSessionStorage
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.AppDatabase
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.database.LocalSalesDraftEntity
import com.retail.smartops.core.database.SalesSyncIntentEntity
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionTransport
import java.security.MessageDigest
import java.math.BigDecimal
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

data class FormalDraftLine(
    val lineKey: String,
    val productId: String,
    val productName: String,
    val quantity: String,
    val unitPrice: String,
    val discountBps: Int = 0,
    val quoteId: String? = null,
)

sealed class DecodeLinesResult {
    data class Success(val lines: List<FormalDraftLine>) : DecodeLinesResult()
    data class Corrupt(val raw: String, val error: String) : DecodeLinesResult()
}

object SalesDraftCodec {
    fun encodeLines(lines: List<FormalDraftLine>): String {
        val array = JSONArray()
        for (line in lines) {
            val obj = JSONObject()
            obj.put("line_key", line.lineKey)
            obj.put("product_id", line.productId)
            obj.put("product_name", line.productName)
            obj.put("quantity", line.quantity)
            obj.put("unit_price", line.unitPrice)
            obj.put("discount_bps", line.discountBps)
            if (line.quoteId != null) {
                obj.put("quote_id", line.quoteId)
            }
            array.put(obj)
        }
        return array.toString()
    }

    /**
     * R7 gate: strictly differentiates corrupt/incompatible data from truly empty drafts.
     * Never swallows parsing errors or silently returns emptyList().
     */
    fun decodeLines(jsonString: String?): DecodeLinesResult {
        if (jsonString.isNullOrBlank()) {
            return DecodeLinesResult.Success(emptyList())
        }
        return try {
            val array = JSONArray(jsonString)
            val lines = mutableListOf<FormalDraftLine>()
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                val lineKey = obj.getString("line_key")
                val productId = obj.getString("product_id")
                val productName = obj.getString("product_name")
                val quantity = obj.getString("quantity")
                val unitPrice = obj.getString("unit_price")
                val discountBps = obj.optInt("discount_bps", 0)
                val quoteId = if (obj.has("quote_id") && !obj.isNull("quote_id")) obj.getString("quote_id") else null
                lines.add(FormalDraftLine(lineKey, productId, productName, quantity, unitPrice, discountBps, quoteId))
            }
            DecodeLinesResult.Success(lines)
        } catch (e: Exception) {
            DecodeLinesResult.Corrupt(jsonString, e.message ?: "Invalid JSON syntax")
        }
    }
}

object EventDigestHelper {
    fun compute(action: String, draftId: String, clientVersion: Int, payload: String): String {
        val md = MessageDigest.getInstance("SHA-256")
        val input = "$action\u0000$draftId\u0000$clientVersion\u0000$payload"
        return md.digest(input.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
}

sealed class SyncOutcome {
    data class Success(val syncedCount: Int) : SyncOutcome()
    data class RetryNeeded(val reason: String) : SyncOutcome()
    data class AuthRequired(val reason: String) : SyncOutcome()
    data class ScopeMismatch(val reason: String) : SyncOutcome()
    data class ConflictDetected(val draftId: String, val reason: String) : SyncOutcome()
    data class AwaitingReview(val reason: String) : SyncOutcome()
    data class Rejected(val status: Int, val code: String, val reason: String) : SyncOutcome()
}

enum class SalesIntentDisposition {
    OFFLINE_NOT_SENT, CONTRACT_REJECTED, PERMISSION_DENIED, RESULT_UNKNOWN, ACCEPTED,
}

/** ACCEPTED describes preparation only; it is never a handoff or payment receipt. */
data class SalesPreparationResult(
    val disposition: SalesIntentDisposition,
    val code: String,
    val message: String,
    val serverDraftId: String? = null,
    val intentKey: String? = null,
)

class SalesDraftRepository(
    private val salesDraftDao: com.retail.smartops.core.database.SalesDraftDao,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    constructor(
        database: AppDatabase,
        clock: () -> Long = System::currentTimeMillis,
    ) : this(database.salesDraft(), clock)

    fun getDraft(draftId: String): LocalSalesDraftEntity? =
        salesDraftDao.getDraft(draftId)

    fun queuePreparation(scope: CacheIdentity, draftId: String): SalesSyncIntentEntity {
        val draft = salesDraftDao.getDraft(draftId) ?: error("draft_not_found")
        require(owns(draft, scope)) { "draft_scope_mismatch" }
        require(draft.stage == "ready") { "draft_not_locally_confirmed" }
        val decoded = SalesDraftCodec.decodeLines(draft.linesJson)
        require(decoded is DecodeLinesResult.Success && decoded.lines.isNotEmpty()) { "draft_lines_invalid" }
        if (draft.serverDraftId == null && salesDraftDao.listDraftIntents(draftId).none { it.action == "create_draft_v4" }) {
            error("legacy_server_identity_unknown")
        }
        val payload = JSONObject().put("currency", draft.currency)
            .put("lines", JSONArray(draft.linesJson)).toString()
        return queueSyncIntent(scope, draftId, "prepare_handoff_v4",
            "client004:prepare:" + UUID.randomUUID().toString(), payload)
    }

    /** No transport is available; never relabel a previously claimed intent as unsent. */
    fun inspectPreparationOffline(scope: CacheIdentity, draftId: String): SalesPreparationResult {
        val draft = salesDraftDao.getDraft(draftId) ?: error("draft_not_found")
        require(owns(draft, scope)) { "draft_scope_mismatch" }
        val intents = salesDraftDao.listDraftIntents(draftId)
        val root = intents.single { it.action == "prepare_handoff_v4" }
        fun result(kind: SalesIntentDisposition, code: String, message: String) =
            SalesPreparationResult(kind, code, message, draft.serverDraftId, root.idempotencyKey)
        if (root.state == "prepared" && !draft.serverDraftId.isNullOrBlank()) {
            return result(SalesIntentDisposition.ACCEPTED, "price_source_hold",
                "后台商品行此前已确认；缺少已核价格来源，交接未发送")
        }
        if (intents.any { it.state in setOf("auth_required", "forbidden") }) {
            return result(SalesIntentDisposition.PERMISSION_DENIED, "stored_permission_denied",
                "原请求被身份或权限规则拒绝，不能改称离线待发")
        }
        if (intents.any { it.state in setOf("rejected", "conflict", "failed") }) {
            return result(SalesIntentDisposition.CONTRACT_REJECTED, "stored_request_rejected",
                "原请求已被拒绝，不能改称离线待发")
        }
        if (intents.any { it.state != "pending" || it.attempts != 0 }) {
            return result(SalesIntentDisposition.RESULT_UNKNOWN, "stored_progress_requires_check",
                "原意图已有提交记录；当前无法核对，保持原键且不重发")
        }
        return result(SalesIntentDisposition.OFFLINE_NOT_SENT, "offline_not_sent",
            "已保存原准备意图，尚未发出请求；不是交接成功")
    }

    fun prepareForHandoff(scope: CacheIdentity, draftId: String,
        apiClient: HttpApiClient, sessions: SessionRepository): SalesPreparationResult =
        SalesSyncEngine(salesDraftDao, apiClient, sessions, clock).prepareForHandoff(scope, draftId)

    private fun owns(draft: LocalSalesDraftEntity, scope: CacheIdentity): Boolean =
        draft.scopeDigest == scope.digest() && draft.organizationId == scope.organizationId &&
            draft.storeId == scope.storeId && draft.employeeId == scope.subjectId

    fun listDrafts(scope: CacheIdentity): List<LocalSalesDraftEntity> =
        salesDraftDao.listDrafts(scope.digest(), scope.subjectId)

    /** Only newly created v4 drafts get a new, durable create intent. Never backfill v3 rows. */
    fun createLocalDraft(
        draftId: String, scope: CacheIdentity, lines: List<FormalDraftLine>,
        currency: String = "CNY", stage: String = "draft",
    ): LocalSalesDraftEntity {
        val now = clock()
        val draft = LocalSalesDraftEntity(
            draftId = draftId, scopeDigest = scope.digest(), organizationId = scope.organizationId,
            storeId = scope.storeId, employeeId = scope.subjectId, clientVersion = 1,
            currency = currency, linesJson = SalesDraftCodec.encodeLines(lines), stage = stage,
            status = "pending", syncState = "local_only", createdAtMs = now, updatedAtMs = now,
        )
        val payload = JSONObject().put("currency", currency).toString()
        val key = "client004:create:" + UUID.randomUUID().toString()
        val intent = SalesSyncIntentEntity(
            intentId = UUID.randomUUID().toString(), draftId = draftId, scopeDigest = scope.digest(),
            organizationId = scope.organizationId, storeId = scope.storeId, employeeId = scope.subjectId,
            idempotencyKey = key, action = "create_draft_v4",
            eventDigest = EventDigestHelper.compute("create_draft_v4", draftId, 1, payload),
            payloadJson = payload, state = "pending", attempts = 0, lastError = null,
            createdAtMs = now, updatedAtMs = now,
        )
        salesDraftDao.createDraftAndIntent(draft, intent)
        return draft
    }

    fun saveDraft(
        draftId: String,
        scope: CacheIdentity,
        lines: List<FormalDraftLine>,
        currency: String = "CNY",
        stage: String = "draft",
        status: String = "pending",
        syncState: String = "local_only",
    ): LocalSalesDraftEntity {
        val existing = salesDraftDao.getDraft(draftId)
        require(existing == null || owns(existing, scope)) { "draft_scope_mismatch" }
        val now = clock()
        val clientVersion = (existing?.clientVersion ?: 0) + 1
        val linesJson = SalesDraftCodec.encodeLines(lines)
        val draft = LocalSalesDraftEntity(
            draftId = draftId,
            scopeDigest = scope.digest(),
            organizationId = scope.organizationId,
            storeId = scope.storeId,
            employeeId = scope.subjectId,
            clientVersion = clientVersion,
            serverVersion = existing?.serverVersion,
            serverDraftId = existing?.serverDraftId,
            currency = currency,
            linesJson = linesJson,
            stage = stage,
            status = status,
            syncState = syncState,
            lastError = null,
            rawBackup = null,
            createdAtMs = existing?.createdAtMs ?: now,
            updatedAtMs = now,
        )
        return salesDraftDao.saveOwnedDraft(draft)
    }

    /**
     * R7 gate: loads draft lines with corruption detection.
     * If corrupt, marks entity as 'corrupt' with raw backup and lastError preserved.
     */
    fun loadDraftLinesWithIntegrityCheck(draftId: String): Pair<LocalSalesDraftEntity?, DecodeLinesResult> {
        val draft = salesDraftDao.getDraft(draftId) ?: return null to DecodeLinesResult.Success(emptyList())
        val decoded = SalesDraftCodec.decodeLines(draft.linesJson)
        return if (decoded is DecodeLinesResult.Corrupt) {
            val updated = draft.copy(
                syncState = "corrupt",
                lastError = "CORRUPT_PAYLOAD: ${decoded.error}",
                rawBackup = decoded.raw,
                updatedAtMs = clock(),
            )
            salesDraftDao.putDraft(updated)
            updated to decoded
        } else {
            draft to decoded
        }
    }

    /**
     * R7 recovery gate: recovers corrupt draft into valid state preserving audit backup.
     */
    fun recoverCorruptDraft(
        draftId: String,
        repairedLines: List<FormalDraftLine>,
    ): LocalSalesDraftEntity {
        val existing = salesDraftDao.getDraft(draftId)
            ?: throw IllegalStateException("Draft not found for recovery: $draftId")
        val now = clock()
        val recovered = existing.copy(
            clientVersion = existing.clientVersion + 1,
            linesJson = SalesDraftCodec.encodeLines(repairedLines),
            syncState = "local_only",
            lastError = null,
            rawBackup = existing.rawBackup ?: existing.linesJson, // keep original raw for audit
            updatedAtMs = now,
        )
        salesDraftDao.putDraft(recovered)
        return recovered
    }

    fun queueSyncIntent(
        scope: CacheIdentity,
        draftId: String,
        action: String,
        idempotencyKey: String,
        payloadJson: String,
    ): SalesSyncIntentEntity {
        val draft = salesDraftDao.getDraft(draftId)
            ?: throw IllegalStateException("Draft not found: $draftId")
        require(owns(draft, scope)) { "draft_scope_mismatch" }
        require(idempotencyKey.isNotBlank()) { "idempotency_key_required" }
        salesDraftDao.findIntentByKey(idempotencyKey)?.let { existing ->
            require(existing.draftId == draftId && existing.action == action &&
                existing.scopeDigest == scope.digest() && existing.employeeId == scope.subjectId &&
                existing.organizationId == scope.organizationId && existing.storeId == scope.storeId &&
                existing.payloadJson == payloadJson) { "idempotency_conflict" }
            return existing // Never replace a sent/unknown intent or rotate its key.
        }
        val now = clock()
        val digest = EventDigestHelper.compute(action, draftId, draft.clientVersion, payloadJson)
        val intent = SalesSyncIntentEntity(
            intentId = UUID.randomUUID().toString(),
            draftId = draftId,
            scopeDigest = scope.digest(),
            organizationId = scope.organizationId,
            storeId = scope.storeId,
            employeeId = scope.subjectId,
            idempotencyKey = idempotencyKey,
            action = action,
            eventDigest = digest,
            payloadJson = payloadJson,
            state = "pending",
            attempts = 0,
            lastError = null,
            createdAtMs = now,
            updatedAtMs = now,
        )
        // Insertion and the pending projection share one Room transaction; do not
        // put back a stale draft snapshot over a concurrently accepted server ID.
        return salesDraftDao.getOrCreateIntent(intent)
    }
}

class SalesSyncEngine(
    private val salesDraftDao: com.retail.smartops.core.database.SalesDraftDao,
    private val apiClient: HttpApiClient,
    private val sessionRepository: SessionRepository,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    constructor(
        database: AppDatabase,
        apiClient: HttpApiClient,
        sessionRepository: SessionRepository,
        clock: () -> Long = System::currentTimeMillis,
    ) : this(database.salesDraft(), apiClient, sessionRepository, clock)

    /** Notifications are a monotonic fence; equal capability values cannot revive an operation. */
    private class OperationFence(
        sessions: SessionRepository,
        private val parentActive: () -> Boolean = { true },
    ) : java.io.Closeable {
        private val invalidated = AtomicBoolean(false)
        // Register before captureRequest, including the empty-session path.
        private val unregister = sessions.onScopeInvalidated { invalidated.set(true) }
        fun isActive(): Boolean = !invalidated.get() && parentActive()
        override fun close() { unregister() }
    }

    suspend fun syncPending(scope: CacheIdentity): SyncOutcome {
        val session = sessionRepository.currentSession()
            ?: return SyncOutcome.AuthRequired("会话已失效或未登录")
        if (session.organizationId != scope.organizationId || session.storeId != scope.storeId ||
            session.subjectId != scope.subjectId) return SyncOutcome.ScopeMismatch("当前身份与意图所有者不匹配")
        var synced = 0
        for (queued in salesDraftDao.listPendingIntents(scope.digest(), scope.subjectId)) {
            // An earlier root flow in this loop may have accepted this child already.
            val intent = salesDraftDao.getIntent(queued.intentId) ?: continue
            if (intent.state !in setOf("pending", "in_flight")) continue
            val draft = salesDraftDao.getDraft(intent.draftId)
            if (draft == null || draft.syncState == "corrupt") {
                salesDraftDao.updateIntentState(intent.intentId, "failed",
                    if (draft == null) "draft_not_found" else "draft_corrupt_data_hold", clock())
                continue
            }
            if (intent.organizationId != scope.organizationId || intent.storeId != scope.storeId ||
                draft.scopeDigest != scope.digest() || draft.employeeId != scope.subjectId ||
                draft.organizationId != scope.organizationId || draft.storeId != scope.storeId) {
                return SyncOutcome.ScopeMismatch("草稿与意图不属于当前员工和门店")
            }
            // A previous process may have sent this request. No automatic POST, even with the same key.
            if (intent.state == "in_flight") return SyncOutcome.AwaitingReview("请求结果未知，请核对原意图")
            if (intent.action == "prepare_handoff_v4") {
                val prepared = prepareForHandoff(scope, draft.draftId)
                when (prepared.disposition) {
                    SalesIntentDisposition.ACCEPTED -> { synced++; continue }
                    SalesIntentDisposition.PERMISSION_DENIED -> return SyncOutcome.Rejected(403, prepared.code, prepared.message)
                    SalesIntentDisposition.CONTRACT_REJECTED -> return SyncOutcome.Rejected(400, prepared.code, prepared.message)
                    else -> return SyncOutcome.AwaitingReview(prepared.message)
                }
            }
            if (intent.action != "create_draft_v4") {
                return SyncOutcome.AwaitingReview("历史意图或后台草稿身份未核实，禁止自动重放")
            }
            val outcome = createDraft(intent, draft, scope)
            if (outcome is SyncOutcome.Success) synced += outcome.syncedCount else return outcome
        }
        val unknown = salesDraftDao.listDrafts(scope.digest(), scope.subjectId).any { draft ->
            salesDraftDao.listDraftIntents(draft.draftId).any { it.state in setOf("unknown", "in_flight") }
        }
        return if (unknown) SyncOutcome.AwaitingReview("存在结果未知的原意图，等待核对") else SyncOutcome.Success(synced)
    }

    /**
     * Frozen CLIENT-004 preparation slice: create -> add lines -> confirm lines.
     * No quote, ready, handoff or receipt POST is enabled without verified price provenance.
     */
    fun prepareForHandoff(scope: CacheIdentity, draftId: String): SalesPreparationResult {
        return OperationFence(sessionRepository).use { operation ->
            val repo = SalesDraftRepository(salesDraftDao, clock)
            val captured = sessionRepository.captureRequest()
                ?: return SalesPreparationResult(SalesIntentDisposition.PERMISSION_DENIED, "authentication_required", "请先登录，未发送交接")
            val (session, request) = captured
            val capabilities = sessionRepository.getCapabilities()
            val capabilityVersion = sessionRepository.getCapabilityVersion()
            var root: SalesSyncIntentEntity? = null
            var serverId: String? = null
            fun result(kind: SalesIntentDisposition, code: String, message: String) =
                SalesPreparationResult(kind, code, message, serverId, root?.idempotencyKey)
            fun guarded(block: () -> Unit): Boolean {
                var active = false
                sessionRepository.whileCurrent(session) {
                    if (operation.isActive() && request.isActive() && sessionRepository.getCapabilities() == capabilities &&
                        sessionRepository.getCapabilityVersion() == capabilityVersion &&
                        session.organizationId == scope.organizationId && session.storeId == scope.storeId &&
                        session.subjectId == scope.subjectId) { block(); active = true }
                }
                return active
            }
            fun stale() = result(SalesIntentDisposition.PERMISSION_DENIED, "scope_changed", "身份或权限已变化，旧结果未回填")
            fun storedFailure(intent: SalesSyncIntentEntity): SalesPreparationResult = when (intent.state) {
                "forbidden", "auth_required" -> result(SalesIntentDisposition.PERMISSION_DENIED,
                    intent.lastError ?: "permission_denied", "原请求被身份或权限规则拒绝，未发送交接")
                "rejected", "conflict", "failed" -> result(SalesIntentDisposition.CONTRACT_REJECTED,
                    intent.lastError ?: "request_rejected", "原请求已被拒绝，请核对；未发送交接")
                else -> result(SalesIntentDisposition.RESULT_UNKNOWN, intent.lastError ?: "result_unknown",
                    "原请求结果未知，保留原键待核对，禁止重发")
            }
            fun failure(response: ApiResult<*>, intent: SalesSyncIntentEntity? = null): SalesPreparationResult {
                if (response is ApiResult.Failure) {
                    val state = when {
                        response.status == 401 -> "auth_required"
                        response.status == 403 -> "forbidden"
                        response.status in 400..499 -> "rejected"
                        else -> "unknown"
                    }
                    if (intent != null) salesDraftDao.updateIntentState(intent.intentId, state,
                        "${response.status}:${response.code}", clock())
                    return result(when (state) {
                        "auth_required", "forbidden" -> SalesIntentDisposition.PERMISSION_DENIED
                        "rejected" -> SalesIntentDisposition.CONTRACT_REJECTED
                        else -> SalesIntentDisposition.RESULT_UNKNOWN
                    }, response.code, response.userMessage)
                }
                if (intent != null) salesDraftDao.updateIntentState(intent.intentId, "unknown", "cancelled_after_claim", clock())
                return result(SalesIntentDisposition.RESULT_UNKNOWN, "cancelled_result_unknown", "请求结果未知，未重发原意图")
            }
            try {
                var draft: LocalSalesDraftEntity? = null
                if (!guarded { draft = salesDraftDao.getDraft(draftId) }) return stale()
                val local = draft ?: return result(SalesIntentDisposition.CONTRACT_REJECTED, "draft_not_found", "本地草稿不存在")
                if (local.scopeDigest != scope.digest() || local.organizationId != scope.organizationId ||
                    local.storeId != scope.storeId || local.employeeId != scope.subjectId) return stale()
                serverId = local.serverDraftId
                val decoded = SalesDraftCodec.decodeLines(local.linesJson)
                if (decoded !is DecodeLinesResult.Success || decoded.lines.isEmpty()) {
                    return result(SalesIntentDisposition.CONTRACT_REJECTED, "draft_lines_invalid", "草稿行不完整，未发送交接")
                }
                val lines = decoded.lines
                if (lines.map { it.lineKey }.toSet().size != lines.size || lines.any {
                    it.lineKey.isBlank() || it.lineKey.length > 200 || it.productId.isBlank() ||
                        it.productName.isBlank() || it.productId.length > 200 || it.productName.length > 200 ||
                        it.discountBps !in 0..10000 || !validAmount(it.quantity) || !validAmount(it.unitPrice)
                }) return result(SalesIntentDisposition.CONTRACT_REJECTED, "line_contract_invalid", "商品行不符合服务端数量或价格契约")
                // A bare historic quote ID does not prove a binding to this server draft.
                if (lines.any { it.quoteId != null }) return result(SalesIntentDisposition.CONTRACT_REJECTED,
                    "quote_binding_unverified", "报价尚未核实对应后台草稿，不挪用旧报价；交接未发送")
                try {
                    if (!guarded { root = repo.queuePreparation(scope, draftId) }) return stale()
                } catch (error: IllegalStateException) {
                    return result(SalesIntentDisposition.RESULT_UNKNOWN, error.message ?: "intent_hold", "旧草稿后台身份或意图需先核对")
                } catch (error: IllegalArgumentException) {
                    return result(SalesIntentDisposition.CONTRACT_REJECTED, error.message ?: "intent_invalid", "草稿与原意图不一致，未发送")
                }
                val flow = root!!
                if (flow.state == "prepared") {
                    if (serverId == null || !safeId(serverId!!)) return result(SalesIntentDisposition.RESULT_UNKNOWN,
                        "accepted_identity_missing", "已确认准备步骤的后台身份丢失，禁止重发")
                    return result(SalesIntentDisposition.ACCEPTED, "price_source_hold",
                        "后台草稿和商品行已确认；缺少已核价格来源，交接未发送")
                }
                if (flow.state != "pending") return storedFailure(flow)
                val creation = salesDraftDao.listDraftIntents(draftId).singleOrNull { it.action == "create_draft_v4" }
                if (serverId == null) {
                    if (creation == null) return result(SalesIntentDisposition.RESULT_UNKNOWN, "legacy_server_identity_unknown", "旧草稿后台身份未知，未发送依赖请求")
                    if (creation.state != "pending") return storedFailure(creation)
                    if (!guarded {}) return stale()
                    when (val created = createDraft(creation, local, scope,
                        parentActive = { operation.isActive() && request.isActive() })) {
                        is SyncOutcome.Success -> Unit
                        is SyncOutcome.Rejected -> return result(if (created.status == 403) SalesIntentDisposition.PERMISSION_DENIED else SalesIntentDisposition.CONTRACT_REJECTED,
                            created.code, created.reason)
                        is SyncOutcome.AuthRequired -> return result(SalesIntentDisposition.PERMISSION_DENIED, "authentication_required", created.reason)
                        is SyncOutcome.ConflictDetected -> return result(SalesIntentDisposition.CONTRACT_REJECTED, "create_conflict", created.reason)
                        else -> return result(SalesIntentDisposition.RESULT_UNKNOWN, "create_result_unknown", "后台草稿身份未核实，禁止重派")
                    }
                    if (!guarded { serverId = salesDraftDao.getDraft(draftId)?.serverDraftId }) return stale()
                }
                val id = serverId ?: return result(SalesIntentDisposition.RESULT_UNKNOWN, "server_id_missing", "后台草稿ID未确认")
                if (!safeId(id)) return result(SalesIntentDisposition.RESULT_UNKNOWN, "server_id_invalid", "后台草稿ID格式不合契约")
                if (!guarded {}) return stale()
                val read = try { apiClient.authenticatedJson("GET", "/api/v1/sales/drafts/$id", session.accessToken) }
                    catch (_: Exception) { ApiResult.Failure.NetworkError() }
                if (!guarded {}) return stale()
                if (read !is ApiResult.Success) {
                    var denied: SalesPreparationResult? = null
                    if (!guarded { denied = failure(read) }) return stale()
                    if (read is ApiResult.Failure && read.status == 401) sessionRepository.handleUnauthorized(request)
                    return denied!!
                }
                var snapshot = read.data
                if (!validDraft(snapshot, local, scope, id, lines)) return result(SalesIntentDisposition.CONTRACT_REJECTED,
                    "draft_snapshot_mismatch", "后台草稿与本地来源不一致，未发送依赖请求")

                fun sendStep(action: String, path: String, body: Map<String, Any?>,
                    accepts: (Map<String, Any?>) -> Boolean): SalesPreparationResult? {
                    val payload = JSONObject().put("path", path).put("body", JSONObject(body)).toString()
                    var step: SalesSyncIntentEntity? = null
                    if (salesDraftDao.listDraftIntents(draftId).none { it.action == action } && accepts(snapshot)) {
                        return result(SalesIntentDisposition.RESULT_UNKNOWN, "unowned_server_step",
                            "后台已有同形步骤但缺原本地意图，先核对，不重新提交")
                    }
                    try {
                        if (!guarded { step = repo.queueSyncIntent(scope, draftId, action,
                            "client004:step:" + UUID.randomUUID().toString(), payload) }) return stale()
                    } catch (_: Exception) { return result(SalesIntentDisposition.CONTRACT_REJECTED, "step_binding_conflict", "原步骤与当前来源不一致") }
                    val intent = step!!
                    if (intent.state == "synced") {
                        return if (accepts(snapshot)) null else result(SalesIntentDisposition.RESULT_UNKNOWN,
                            "accepted_step_changed", "已接受步骤与当前后台快照不一致，禁止重发")
                    }
                    if (intent.state != "pending") return storedFailure(intent)
                    var claimed = false
                    if (!guarded { claimed = salesDraftDao.claimPendingIntent(intent.intentId, clock()) == 1 }) return stale()
                    if (!claimed) return result(SalesIntentDisposition.RESULT_UNKNOWN, "step_in_flight", "原步骤正在处理或结果未知，未重复发送")
                    // Lines/confirm do NOT have a server Idempotency-Key contract. Never replay their uncertain POSTs.
                    val response = try { apiClient.authenticatedJson("POST", path, session.accessToken, body) }
                        catch (_: Exception) { ApiResult.Failure.NetworkError() }
                    var rejected: SalesPreparationResult? = null
                    if (!guarded {
                        if (response is ApiResult.Success) {
                            if (!validDraft(response.data, local, scope, id, lines) || !accepts(response.data)) {
                                salesDraftDao.updateIntentState(intent.intentId, "unknown", "invalid_step_response", clock())
                                rejected = result(SalesIntentDisposition.RESULT_UNKNOWN, "invalid_step_response", "后台步骤结果不合契约，保留原意图待核对")
                            } else {
                                salesDraftDao.putIntent(intent.copy(state = "synced", attempts = intent.attempts + 1,
                                    lastError = null, updatedAtMs = clock()))
                                snapshot = response.data
                            }
                        } else rejected = failure(response, intent)
                    }) return stale()
                    if (response is ApiResult.Failure && response.status == 401) sessionRepository.handleUnauthorized(request)
                    return rejected
                }

                val confirmedLineIds = mutableMapOf<String, String>()
                for (line in lines) {
                    val action = "add_line_v4:${line.lineKey}"
                    val body = mapOf<String, Any?>("line_key" to line.lineKey, "product_id" to line.productId,
                        "product_name" to line.productName, "quantity" to line.quantity,
                        "unit_price" to line.unitPrice, "discount_bps" to line.discountBps)
                    val added = sendStep(action, "/api/v1/sales/drafts/$id/lines", body) { data ->
                        serverLines(data).count { lineMatches(it, line) } == 1
                    }
                    if (added != null) return added
                    val remoteLine = serverLines(snapshot).single { lineMatches(it, line) }
                    val lineId = remoteLine["id"] as String
                    val confirmed = sendStep("confirm_line_v4:${line.lineKey}",
                        "/api/v1/sales/drafts/$id/lines/$lineId/confirm", emptyMap()) { data ->
                        serverLines(data).any { lineMatches(it, line) && it["id"] == lineId &&
                            it["confirmed_by"] == scope.subjectId && validTimestamp(it["confirmed_at"]) }
                    }
                    if (confirmed != null) return confirmed
                    confirmedLineIds[line.lineKey] = lineId
                }
                // Intermediate snapshots may be subsets while adding lines. The final
                // acceptance must prove the entire original group, stable IDs and confirmations.
                if (!validDraft(snapshot, local, scope, id, lines) ||
                    serverLines(snapshot).size != lines.size || lines.any { expected ->
                        val remote = serverLines(snapshot).singleOrNull { lineMatches(it, expected) }
                        remote == null || remote["id"] != confirmedLineIds[expected.lineKey] ||
                            remote["confirmed_by"] != scope.subjectId || !validTimestamp(remote["confirmed_at"])
                    }) return result(SalesIntentDisposition.RESULT_UNKNOWN, "incomplete_confirmed_snapshot",
                        "最终后台快照未保全全部原商品行和确认，保持原意图待核对")
                var completed = false
                if (!guarded {
                    completed = salesDraftDao.completePreparation(flow.intentId, id, local.linesJson,
                        (snapshot["version"] as Number).toInt(), clock())
                }) return stale()
                return if (completed) result(SalesIntentDisposition.ACCEPTED, "price_source_hold",
                    "后台草稿和商品行已确认；缺少已核价格来源，交接未发送")
                else result(SalesIntentDisposition.RESULT_UNKNOWN, "local_snapshot_changed", "本地来源已变化，保留结果待核对")
            } finally { sessionRepository.finishRequest(request) }
        }
    }

    private fun safeId(value: String) = Regex("[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}").matches(value)
    private fun validAmount(value: String): Boolean = try {
        val amount = BigDecimal(value)
        amount > BigDecimal.ZERO && amount <= BigDecimal("1000000000") && amount.scale() <= 6
    } catch (_: Exception) { false }
    private fun sameAmount(actual: Any?, expected: String): Boolean = try {
        actual is String && BigDecimal(actual).compareTo(BigDecimal(expected)) == 0
    } catch (_: Exception) { false }
    private fun validTimestamp(value: Any?): Boolean = try {
        if (value !is String) false else { java.time.OffsetDateTime.parse(value); true }
    } catch (_: Exception) { false }
    @Suppress("UNCHECKED_CAST")
    private fun serverLines(data: Map<String, Any?>): List<Map<String, Any?>> =
        (data["lines"] as? List<*>)?.filterIsInstance<Map<String, Any?>>() ?: emptyList()
    private fun lineMatches(data: Map<String, Any?>, line: FormalDraftLine): Boolean =
        data.keys.containsAll(setOf("id", "quote_id", "line_key", "product_id", "product_name", "quantity",
            "unit_price", "discount_bps", "line_amount", "confirmed_by", "confirmed_at")) &&
            data["id"] is String && safeId(data["id"] as String) && data["line_key"] == line.lineKey &&
            data["product_id"] == line.productId && data["product_name"] == line.productName &&
            sameAmount(data["quantity"], line.quantity) && sameAmount(data["unit_price"], line.unitPrice) &&
            (data["discount_bps"] as? Number)?.toDouble() == line.discountBps.toDouble() && data["quote_id"] == null
    private fun validDraft(data: Map<String, Any?>, local: LocalSalesDraftEntity, scope: CacheIdentity,
        id: String, expected: List<FormalDraftLine>): Boolean {
        val version = data["version"] as? Number ?: return false
        val rawLines = data["lines"] as? List<*> ?: return false
        val lines = serverLines(data)
        return data.keys.containsAll(setOf("id", "organization_id", "store_id", "creator_id", "quote_id",
            "stage", "status", "sync_state", "version", "currency", "total_amount", "voided_at", "expires_at",
            "created_at", "updated_at", "lines", "handoffs")) &&
            validTimestamp(data["created_at"]) && validTimestamp(data["updated_at"]) &&
            data["id"] == id && data["organization_id"] == scope.organizationId &&
            data["store_id"] == scope.storeId && data["creator_id"] == scope.subjectId &&
            data["currency"] == local.currency && data["stage"] == "draft" && data["status"] == "pending" &&
            data["voided_at"] == null && data["handoffs"] is List<*> && (data["handoffs"] as List<*>).isEmpty() &&
            version.toInt() > 0 && version.toDouble() == version.toInt().toDouble() &&
            lines.size == rawLines.size && lines.map { it["line_key"] }.toSet().size == lines.size &&
            lines.map { it["id"] }.toSet().size == lines.size &&
            lines.all { remote -> expected.any { lineMatches(remote, it) } }
    }

    private fun createDraft(
        intent: SalesSyncIntentEntity, draft: LocalSalesDraftEntity, scope: CacheIdentity,
        parentActive: () -> Boolean = { true },
    ): SyncOutcome {
        return OperationFence(sessionRepository, parentActive).use { operation ->
            val captured = sessionRepository.captureRequest()
                ?: return SyncOutcome.AuthRequired("会话已失效或未登录")
            val (session, request) = captured
            val capabilities = sessionRepository.getCapabilities()
            val capabilityVersion = sessionRepository.getCapabilityVersion()
            fun guarded(block: () -> Unit): Boolean {
                var active = false
                sessionRepository.whileCurrent(session) {
                    if (operation.isActive() && request.isActive() && sessionRepository.getCapabilities() == capabilities &&
                        sessionRepository.getCapabilityVersion() == capabilityVersion &&
                        session.organizationId == scope.organizationId && session.storeId == scope.storeId &&
                        session.subjectId == scope.subjectId) { block(); active = true }
                }
                return active
            }
            try {
                val payload = try { JSONObject(intent.payloadJson) } catch (_: Exception) {
                    return SyncOutcome.Rejected(0, "corrupt_intent", "意图数据损坏，未发送")
                }
                if (payload.length() != 1 || payload.optString("currency") != draft.currency ||
                    !Regex("[A-Z]{3}").matches(draft.currency) || intent.idempotencyKey.isBlank()) {
                    return SyncOutcome.Rejected(0, "invalid_create_intent", "创建草稿请求不合契约，未发送")
                }
                var claimed = false
                if (!guarded { claimed = salesDraftDao.claimPendingIntent(intent.intentId, clock()) == 1 }) {
                    return SyncOutcome.ScopeMismatch("身份或权限已变化，停止提交")
                }
                if (!claimed) return SyncOutcome.AwaitingReview("原意图已由其他请求处理，禁止重复提交")
                val result = try {
                    apiClient.authenticatedJson("POST", "/api/v1/sales/drafts", session.accessToken,
                        mapOf("currency" to draft.currency), mapOf("Idempotency-Key" to intent.idempotencyKey))
                } catch (_: Exception) { ApiResult.Failure.NetworkError() }
                var outcome: SyncOutcome = SyncOutcome.ScopeMismatch("身份或权限已变化，旧结果未回填")
                if (!guarded {
                    outcome = when (result) {
                        is ApiResult.Success -> {
                            val data = result.data
                            val id = data["id"] as? String
                            val version = data["version"] as? Number
                            val valid = id != null && Regex("[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}").matches(id) &&
                                data["organization_id"] == scope.organizationId && data["store_id"] == scope.storeId &&
                                data["creator_id"] == scope.subjectId && data["currency"] == draft.currency &&
                                data["stage"] == "draft" && data["status"] == "pending" &&
                                data["lines"] is List<*> && (data["lines"] as List<*>).isEmpty() &&
                                data["handoffs"] is List<*> && (data["handoffs"] as List<*>).isEmpty() &&
                                version != null && version.toDouble() == version.toInt().toDouble() && version.toInt() > 0
                            if (!valid) {
                                salesDraftDao.updateIntentState(intent.intentId, "unknown", "invalid_create_response", clock())
                                SyncOutcome.AwaitingReview("后台创建结果不合契约，保留原键待核对")
                            } else if (salesDraftDao.completeDraftCreation(intent.intentId, id!!, version!!.toInt(), clock())) {
                                SyncOutcome.Success(1)
                            } else SyncOutcome.AwaitingReview("后台结果已返回但本地身份绑定冲突，请核对")
                        }
                        is ApiResult.Failure -> {
                            val state = when {
                                result.status == 401 -> "auth_required"
                                result.status == 403 -> "forbidden"
                                result.status == 409 -> "conflict"
                                result.status in 400..499 -> "rejected"
                                else -> "unknown" // Network errors do not prove that nothing was sent.
                            }
                            salesDraftDao.updateIntentState(intent.intentId, state, "${result.status}:${result.code}", clock())
                            when (state) {
                                "auth_required" -> SyncOutcome.AuthRequired(result.userMessage)
                                "conflict" -> {
                                    val latest = salesDraftDao.getDraft(draft.draftId)!!
                                    salesDraftDao.putDraft(latest.copy(syncState = "conflict",
                                        lastError = "409:${result.code}", updatedAtMs = clock()))
                                    SyncOutcome.ConflictDetected(draft.draftId, result.userMessage)
                                }
                                "unknown" -> SyncOutcome.AwaitingReview("请求结果未知，禁止自动重发，请核对原键")
                                else -> SyncOutcome.Rejected(result.status, result.code, result.userMessage)
                            }
                        }
                        ApiResult.Cancelled -> {
                            salesDraftDao.updateIntentState(intent.intentId, "unknown", "cancelled_after_claim", clock())
                            SyncOutcome.AwaitingReview("请求已取消但结果未知，请核对原意图")
                        }
                    }
                }) return outcome
                if (result is ApiResult.Failure && result.status == 401) sessionRepository.handleUnauthorized(request)
                return outcome
            } finally { sessionRepository.finishRequest(request) }
        }
    }

}

class SalesSyncWorker(
    appContext: Context,
    workerParams: WorkerParameters,
) : CoroutineWorker(appContext, workerParams) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val orgId = inputData.getString(KEY_ORG_ID) ?: return@withContext Result.failure()
        val storeId = inputData.getString(KEY_STORE_ID) ?: return@withContext Result.failure()
        val employeeId = inputData.getString(KEY_EMPLOYEE_ID) ?: return@withContext Result.failure()
        val scope = CacheIdentity(orgId, storeId, employeeId)

        val db = AppDatabase.open(applicationContext, scope)
        val transport = UrlConnectionTransport(BuildConfig.API_BASE_URL)
        val apiClient = HttpApiClient(transport)
        val sessionRepo = SessionRepository(apiClient, SecureSessionStorage(applicationContext))
        val engine = SalesSyncEngine(db, apiClient, sessionRepo)

        when (engine.syncPending(scope)) {
            is SyncOutcome.Success -> Result.success()
            is SyncOutcome.RetryNeeded -> Result.retry()
            is SyncOutcome.AuthRequired -> Result.failure()
            is SyncOutcome.ScopeMismatch -> Result.failure()
            is SyncOutcome.ConflictDetected -> Result.failure()
            is SyncOutcome.AwaitingReview -> Result.failure() // Never automatically retry uncertain writes.
            is SyncOutcome.Rejected -> Result.failure()
        }
    }

    companion object {
        const val KEY_SCOPE_DIGEST = "scope_digest"
        const val KEY_ORG_ID = "org_id"
        const val KEY_STORE_ID = "store_id"
        const val KEY_EMPLOYEE_ID = "employee_id"
        const val WORK_NAME_PREFIX = "sales_sync_"

        fun enqueue(context: Context, scope: CacheIdentity): UUID {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val workRequest = OneTimeWorkRequestBuilder<SalesSyncWorker>()
                .setConstraints(constraints)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
                .setInputData(
                    workDataOf(
                        KEY_SCOPE_DIGEST to scope.digest(),
                        KEY_ORG_ID to scope.organizationId,
                        KEY_STORE_ID to scope.storeId,
                        KEY_EMPLOYEE_ID to scope.subjectId,
                    )
                )
                .build()
            WorkManager.getInstance(context)
                .enqueueUniqueWork(
                    WORK_NAME_PREFIX + scope.digest(),
                    ExistingWorkPolicy.APPEND_OR_REPLACE,
                    workRequest,
                )
            return workRequest.id
        }
    }
}
