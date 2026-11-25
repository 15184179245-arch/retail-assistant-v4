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
import com.retail.smartops.core.auth.ProcessOnlySessionStorage
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.AppDatabase
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.database.LocalSalesDraftEntity
import com.retail.smartops.core.database.SalesSyncIntentEntity
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionTransport
import java.security.MessageDigest
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
    val salesUnit: String? = null,
    val unitSource: DraftUnitSource? = null,
)

enum class DraftUnitSource { CATALOG, OPERATOR }

sealed class DecodeLinesResult {
    data class Success(val lines: List<FormalDraftLine>) : DecodeLinesResult()
    data class Corrupt(val raw: String, val error: String) : DecodeLinesResult()
}

object SalesDraftCodec {
    fun validSalesUnit(unit: String): Boolean =
        unit.isNotBlank() && unit.length <= 40 && unit.none(Char::isISOControl)

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
            line.quoteId?.let { obj.put("quote_id", it) }
            line.salesUnit?.let { obj.put("sales_unit", it) }
            line.unitSource?.let { obj.put("unit_source", it.name) }
            array.put(obj)
        }
        return array.toString()
    }

    /**
     * R7 gate: strictly differentiates corrupt/incompatible data from truly empty drafts.
     * Never swallows parsing errors or silently returns emptyList().
     */
    fun decodeLines(jsonString: String): DecodeLinesResult {
        if (jsonString.isBlank()) {
            return DecodeLinesResult.Corrupt(jsonString, "Missing persisted draft lines JSON")
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
                val unit = if (obj.isNull("sales_unit")) null else obj.opt("sales_unit") as? String
                    ?: error("invalid_unit_label")
                val source = if (obj.isNull("unit_source")) null else
                    DraftUnitSource.valueOf(obj.opt("unit_source") as? String ?: error("invalid_unit_source"))
                require((unit == null) == (source == null) && (unit == null || validSalesUnit(unit))) {
                    "invalid_unit_evidence"
                }
                lines.add(FormalDraftLine(lineKey, productId, productName, quantity, unitPrice,
                    discountBps, quoteId, unit, source))
            }
            require(lines.map { it.lineKey }.distinct().size == lines.size) { "duplicate_line_keys" }
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
    CONTRACT_REJECTED, PERMISSION_DENIED, RESULT_UNKNOWN, UNIT_PROOF_REQUIRED,
}

internal const val SALES_UNIT_HOLD_CODE = "sales_unit_binding_unavailable"
internal const val SALES_UNIT_HOLD_MESSAGE = "后台尚不能核验每一行的销售单位；保留本机原件与已有意图，本次未发起后台准备或交接。"

private fun LocalSalesDraftEntity.isUnitIndependentHeader(): Boolean =
    syncState != "corrupt" && status == "pending" && serverDraftId == null && linesJson.trim() == "[]"

/** Read-only local inspection result; never a preparation or payment receipt. */
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

    /** Formal preparation must not allocate a key until the backend can bind each line's units. */
    fun queuePreparation(scope: CacheIdentity, draftId: String): SalesSyncIntentEntity {
        val held = inspectPreparationOffline(scope, draftId)
        error(held.code)
    }

    /** Inspect the owned original without PATCH/GET/POST, state changes, new keys or retries. */
    fun inspectPreparationOffline(scope: CacheIdentity, draftId: String): SalesPreparationResult {
        val (draft, intents) = readOwnedDraft(scope, draftId) ?: return SalesPreparationResult(
            SalesIntentDisposition.CONTRACT_REJECTED, "owned_draft_not_found", "本机没有可读取的对应草稿。",
        )
        val originalKey = intents.singleOrNull { it.action == "prepare_handoff_v4" }?.idempotencyKey
            ?: intents.singleOrNull { it.action == "create_draft_v4" }?.idempotencyKey
        val decoded = SalesDraftCodec.decodeLines(draft.linesJson)
        return when {
            draft.syncState == "corrupt" || decoded is DecodeLinesResult.Corrupt -> SalesPreparationResult(
                SalesIntentDisposition.RESULT_UNKNOWN, "draft_lines_require_review",
                "本机原件需核对；保留原意图，不追加请求。", draft.serverDraftId, originalKey,
            )
            decoded is DecodeLinesResult.Success && decoded.lines.isEmpty() -> SalesPreparationResult(
                SalesIntentDisposition.CONTRACT_REJECTED, "draft_lines_empty",
                "空草稿不能发起交接。", draft.serverDraftId, originalKey,
            )
            else -> SalesPreparationResult(
                SalesIntentDisposition.UNIT_PROOF_REQUIRED, SALES_UNIT_HOLD_CODE,
                if (draft.serverDraftId != null || intents.any { it.state != "pending" || it.attempts != 0 }) {
                    SALES_UNIT_HOLD_MESSAGE + " 旧请求状态仍需核对。"
                } else SALES_UNIT_HOLD_MESSAGE,
                draft.serverDraftId, originalKey,
            )
        }
    }

    fun prepareForHandoff(scope: CacheIdentity, draftId: String,
        apiClient: HttpApiClient, sessions: SessionRepository): SalesPreparationResult =
        SalesSyncEngine(salesDraftDao, apiClient, sessions, clock).prepareForHandoff(scope, draftId)

    private fun owns(draft: LocalSalesDraftEntity, scope: CacheIdentity): Boolean =
        draft.scopeDigest == scope.digest() && draft.organizationId == scope.organizationId &&
            draft.storeId == scope.storeId && draft.employeeId == scope.subjectId

    fun listDrafts(scope: CacheIdentity): List<LocalSalesDraftEntity> =
        salesDraftDao.listDrafts(scope.digest(), scope.subjectId).filter { owns(it, scope) }

    fun readOwnedDraft(scope: CacheIdentity, draftId: String): Pair<LocalSalesDraftEntity, List<SalesSyncIntentEntity>>? =
        salesDraftDao.readOwnedDraftSnapshot(draftId, scope.digest(), scope.organizationId, scope.storeId, scope.subjectId)

    /** Only newly created v4 drafts get a new, durable create intent. Never backfill v3 rows. */
    fun createLocalDraft(
        draftId: String, scope: CacheIdentity, lines: List<FormalDraftLine>,
        currency: String = "CNY", stage: String = "draft",
    ): LocalSalesDraftEntity {
        val (draft, intent) = newSnapshot(draftId, scope, lines, currency, stage)
        salesDraftDao.createDraftAndIntent(draft, intent)
        return draft
    }

    fun persistLocalSnapshot(
        draftId: String, scope: CacheIdentity, lines: List<FormalDraftLine>, currency: String, stage: String,
    ): LocalSalesDraftEntity {
        val (draft, intent) = newSnapshot(draftId, scope, lines, currency, stage)
        return salesDraftDao.persistDraftAndInitialIntent(draft, intent)
    }

    private fun newSnapshot(
        draftId: String, scope: CacheIdentity, lines: List<FormalDraftLine>, currency: String, stage: String,
    ): Pair<LocalSalesDraftEntity, SalesSyncIntentEntity> {
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
        return draft to intent
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
    fun loadDraftLinesWithIntegrityCheck(
        scope: CacheIdentity, draftId: String,
    ): Pair<LocalSalesDraftEntity?, DecodeLinesResult> {
        val draft = readOwnedDraft(scope, draftId)?.first ?: return null to DecodeLinesResult.Success(emptyList())
        // Keep a read-only corruption result; never overwrite a concurrent snapshot just to diagnose it.
        return draft to SalesDraftCodec.decodeLines(draft.linesJson)
    }

    /** Preserve original bytes and only repair the exact owned, known-unsent snapshot. */
    fun recoverCorruptDraft(
        scope: CacheIdentity, draftId: String, repairedLines: List<FormalDraftLine>,
    ): LocalSalesDraftEntity {
        val existing = readOwnedDraft(scope, draftId)?.first ?: error("draft_not_found")
        require(existing.syncState == "corrupt" || SalesDraftCodec.decodeLines(existing.linesJson) is DecodeLinesResult.Corrupt) {
            "draft_not_corrupt"
        }
        val recovered = existing.copy(
            clientVersion = existing.clientVersion + 1,
            linesJson = SalesDraftCodec.encodeLines(repairedLines),
            stage = "draft",
            syncState = "local_only",
            lastError = null,
            rawBackup = existing.rawBackup ?: existing.linesJson,
            updatedAtMs = clock(),
        )
        check(salesDraftDao.saveRepairsOfOwnedDraft(existing, recovered)) { "repair_hold" }
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
        // Current wire contract proves no per-row unit. Only a genuinely empty,
        // currency-only header may get a new key through this lower-level API.
        require(action == "create_draft_v4" && draft.isUnitIndependentHeader() &&
            salesDraftDao.listDraftIntents(draftId).all { it.action == "create_draft_v4" }) { SALES_UNIT_HOLD_CODE }
        val payload = JSONObject(payloadJson)
        require(payload.length() == 1 && payload.opt("currency") == draft.currency) { "invalid_create_intent" }
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
            if (intent.scopeDigest != scope.digest() || intent.employeeId != scope.subjectId ||
                intent.organizationId != scope.organizationId || intent.storeId != scope.storeId) {
                return SyncOutcome.ScopeMismatch("草稿与意图不属于当前员工和门店")
            }
            // A previous process may have sent it. This state and key are immutable
            // here, including when the local draft is missing or corrupt.
            if (intent.state == "in_flight" || intent.attempts != 0) {
                return SyncOutcome.AwaitingReview("请求结果未知，请核对原意图")
            }
            val draft = salesDraftDao.getDraft(intent.draftId)
                ?: return SyncOutcome.AwaitingReview("找不到原草稿；保留原意图，不自动重派。")
            if (draft.scopeDigest != scope.digest() || draft.employeeId != scope.subjectId ||
                draft.organizationId != scope.organizationId || draft.storeId != scope.storeId) {
                return SyncOutcome.ScopeMismatch("草稿与意图不属于当前员工和门店")
            }
            if (intent.action != "create_draft_v4" || !draft.isUnitIndependentHeader()) {
                return SyncOutcome.AwaitingReview(SALES_UNIT_HOLD_MESSAGE)
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
     * CURRENT CONTRACT HOLD: the server's formally prepared line cannot describe or
     * verify its unit. There is no line or confirm sender in this candidate.
     */
    fun prepareForHandoff(scope: CacheIdentity, draftId: String): SalesPreparationResult {
        val captured = sessionRepository.captureRequest()
            ?: return SalesPreparationResult(SalesIntentDisposition.PERMISSION_DENIED, "authentication_required", "请先登录，未发送交接")
        val (session, request) = captured
        return try {
            var result = SalesPreparationResult(SalesIntentDisposition.PERMISSION_DENIED,
                "scope_changed", "身份或权限已变化，未读取原草稿。")
            sessionRepository.whileCurrent(session) {
                if (request.isActive() && CacheIdentity.from(session) == scope &&
                    sessionRepository.hasCapability(com.retail.smartops.core.auth.Capabilities.SALES_DRAFT)) {
                    result = SalesDraftRepository(salesDraftDao, clock).inspectPreparationOffline(scope, draftId)
                }
            }
            result
        } finally { sessionRepository.finishRequest(request) }
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
                var eligible = false
                var claimed = false
                if (!guarded {
                    val current = salesDraftDao.getDraft(draft.draftId)
                    eligible = current != null && current.scopeDigest == draft.scopeDigest &&
                        current.organizationId == draft.organizationId && current.storeId == draft.storeId &&
                        current.employeeId == draft.employeeId && current.currency == draft.currency &&
                        current.isUnitIndependentHeader() &&
                        salesDraftDao.listDraftIntents(draft.draftId).singleOrNull() == intent &&
                        intent.attempts == 0
                    if (eligible) claimed = salesDraftDao.claimPendingIntent(intent.intentId, clock()) == 1
                }) return SyncOutcome.ScopeMismatch("身份或权限已变化，停止提交")
                if (!eligible) return SyncOutcome.AwaitingReview(SALES_UNIT_HOLD_MESSAGE)
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
        // Background replay is not wired for production. Only a future, explicitly
        // live authenticated session may enable it; never recover a saved bearer here.
        val sessionRepo = SessionRepository(apiClient, ProcessOnlySessionStorage)
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
