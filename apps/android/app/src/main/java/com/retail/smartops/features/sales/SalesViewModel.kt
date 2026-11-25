package com.retail.smartops.features.sales

import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.auth.RequestScope
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.features.search.SearchMatchItem
import java.util.UUID
import java.math.BigDecimal
import java.math.RoundingMode
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.getAndUpdate
import kotlinx.coroutines.flow.update

private const val LOCKED_SUBMISSION_MESSAGE = "原意图已固定，不能改内容或换键重派；请先核对原结果"

private val DRAFT_AMOUNT_MAX = BigDecimal("1000000000")

private fun validDecimal(value: BigDecimal, scale: Int, allowZero: Boolean): Boolean =
    value.signum() >= (if (allowZero) 0 else 1) && value <= DRAFT_AMOUNT_MAX &&
        value.stripTrailingZeros().scale() <= scale

private val ZERO_MONEY = BigDecimal.ZERO.setScale(2)
internal fun formatDraftMoney(value: BigDecimal): String = value.toPlainString()

internal fun formatDraftQuantity(value: Double): String = BigDecimal.valueOf(value)
    .stripTrailingZeros().toPlainString()

data class SalesLineItem(
    val lineKey: String = UUID.randomUUID().toString(),
    val productId: String,
    val productName: String,
    val quantity: Double,
    val unitPrice: Double,
    val discountBps: Int = 0,
    val quoteId: String? = null,
    val packagingUnit: String = "件",
    val note: String? = null,
    val unitSource: DraftUnitSource? = null,
) {
    internal val exactSubtotal: BigDecimal
        get() = BigDecimal.valueOf(unitPrice).multiply(BigDecimal.valueOf(10000L - discountBps, 4))
            .setScale(2, RoundingMode.HALF_UP).multiply(BigDecimal.valueOf(quantity))
            .setScale(2, RoundingMode.HALF_UP)

    internal val unitPriceText: String get() =
        formatDraftMoney(BigDecimal.valueOf(unitPrice).setScale(2, RoundingMode.UNNECESSARY))

    fun toFormalDraftLine(): FormalDraftLine =
        FormalDraftLine(
            lineKey = lineKey,
            productId = productId,
            productName = productName,
            quantity = formatDraftQuantity(quantity),
            unitPrice = unitPriceText,
            discountBps = discountBps,
            quoteId = quoteId,
            salesUnit = packagingUnit.takeIf { unitSource != null },
            unitSource = unitSource,
        )

    companion object {
        fun restore(line: FormalDraftLine): SalesLineItem {
            val quantity = line.quantity.toBigDecimal()
            val price = line.unitPrice.toBigDecimal()
            require(validDecimal(quantity, 6, false) && validDecimal(price, 2, true) &&
                line.discountBps in 0..10000) { "invalid_owned_draft_numbers" }
            return SalesLineItem(
                line.lineKey, line.productId, line.productName, quantity.toDouble(), price.toDouble(),
                line.discountBps, line.quoteId, line.salesUnit ?: "单位待核", unitSource = line.unitSource,
            )
        }
    }
}

data class SalesHandoffInfo(
    val handoffId: String,
    val state: String, // "requested", "sent", "receipt_unknown", "confirmed", "pending_delivery"
    val deliveryRef: String? = null,
    val externalRef: String? = null,
    val isSettled: Boolean = false, // Strictly false until verified receipt arrives
    val requestedAtMs: Long,
)

enum class DraftSaveStatus { EMPTY, SAVING, SAVED, FAILED, READ_ONLY }
data class SavedSalesDraft(val id: String, val updatedAtMs: Long, val syncState: String)

sealed interface SalesDraftArchive {
    data object Idle : SalesDraftArchive
    data object Loading : SalesDraftArchive
    data class Loaded(val drafts: List<SavedSalesDraft>) : SalesDraftArchive
    data object Failed : SalesDraftArchive
}

data class SalesUiState(
    val draftId: String = UUID.randomUUID().toString(),
    val lines: List<SalesLineItem> = emptyList(),
    val stage: String = "draft", // Local human confirmation, not the server ready gate.
    val status: String = "pending", // "pending" or "paid"
    val syncState: String = "local_only", // "local_only", "synced", "pending_sync", "corrupt"
    val currency: String = "CNY",
    val totalQuantity: Double = 0.0,
    val totalAmount: BigDecimal = ZERO_MONEY,
    val handoff: SalesHandoffInfo? = null,
    val ambiguityWarning: String? = null,
    val error: String? = null,
    val message: String? = null,
    val isLoading: Boolean = false,
    val isOffline: Boolean = false,
    val serverDraftId: String? = null,
    val preparationIntentKey: String? = null,
    val preparationDisposition: SalesIntentDisposition? = null,
    val handoffGate: String? = null,
    val submissionLocked: Boolean = false,
    // Transient human selection/form only: not a verified quote or persisted source snapshot.
    val pendingProduct: SearchMatchItem? = null,
    val quantityInput: String = "",
    val priceInput: String = "",
    val manualUnitInput: String = "",
    val saveStatus: DraftSaveStatus = DraftSaveStatus.EMPTY,
    val archive: SalesDraftArchive = SalesDraftArchive.Idle,
    val isOpeningDraft: Boolean = false,
) {
    internal val catalogSalesUnit: String?
        get() = pendingProduct?.unit?.trim()?.takeIf(SalesDraftCodec::validSalesUnit)
}

private val salesIoExecutor: Executor = Executors.newSingleThreadExecutor { runnable ->
    Thread(runnable, "sales-io").apply { isDaemon = true }
}

/** Reserve before work dispatch; retire without waiting for Room or the session monitor. */
private class SalesStoreLifetime(
    private val worker: Executor,
    private val retirementDispatcher: CoroutineDispatcher,
    private val afterDrain: () -> Unit,
) {
    private val lock = Any()
    @Volatile var isRetired = false
        private set
    private var pending = 0

    fun enqueue(work: () -> Unit): Boolean {
        synchronized(lock) {
            if (isRetired) return false
            pending++
        }
        val claimed = AtomicBoolean()
        return try {
            worker.execute {
                if (!claimed.compareAndSet(false, true)) return@execute
                try { work() } finally { done() }
            }
            true
        } catch (rejected: RejectedExecutionException) {
            // A direct executor can relay a running task's exception, not a dispatch rejection.
            if (!claimed.compareAndSet(false, true)) throw rejected
            done()
            false
        }
    }

    fun retire() {
        val drained = synchronized(lock) {
            if (isRetired) return
            isRetired = true
            pending == 0
        }
        if (drained) finishOffCaller()
    }

    private fun done() {
        val drained = synchronized(lock) { --pending == 0 && isRetired }
        if (drained) finishOffCaller()
    }

    private fun finishOffCaller() {
        // The shared coroutine I/O dispatcher does not depend on a work executor accepting new jobs.
        retirementDispatcher.dispatch(EmptyCoroutineContext, Runnable { afterDrain() })
    }
}

class SalesViewModel(
    private val salesDraftRepository: SalesDraftRepository? = null,
    private val sessionRepository: SessionRepository,
    private val apiClient: HttpApiClient? = null,
    private val clock: () -> Long = System::currentTimeMillis,
    ioExecutor: Executor = salesIoExecutor,
    private val closeOwnedDatabase: (() -> Unit)? = null,
    retirementDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    private val _state = MutableStateFlow(SalesUiState())
    val state: StateFlow<SalesUiState> = _state.asStateFlow()

    private val ownerGeneration = sessionRepository.status.value.generation
    // Archive reads use the same monitor as their owner, independently of local saves and handoff.
    private var archiveRequest: RequestScope? = null
    private val localIo = SalesStoreLifetime(ioExecutor, retirementDispatcher, ::finishRetirement)
    private val unsubscribeScope = sessionRepository.onScopeInvalidated(::clearOnAccountSwitch)

    private fun captureOwnedRequest(): Pair<Session, RequestScope>? {
        if (localIo.isRetired) return null
        val captured = sessionRepository.captureRequest() ?: return null
        if (captured.second.generation == ownerGeneration) return captured
        sessionRepository.finishRequest(captured.second)
        return null
    }

    private fun enqueueLocal(request: RequestScope, onFailure: () -> Unit, work: () -> Unit) {
        val accepted = localIo.enqueue {
            try { work() } catch (_: Exception) { onFailure() } finally {
                sessionRepository.finishRequest(request)
            }
        }
        if (!accepted) {
            try { onFailure() } finally { sessionRepository.finishRequest(request) }
        }
    }

    fun close() = localIo.retire()

    private fun finishRetirement() {
        try {
            synchronized(sessionRepository) {
                unsubscribeScope()
                retireArchiveRead()
                _state.update { it.copy(archive = SalesDraftArchive.Idle) }
            }
        } finally {
            // Only the database owned by this retired Sales graph, never the Search cache.
            closeOwnedDatabase?.invoke()
        }
    }

    private fun retireArchiveRead() {
        val request = archiveRequest
        archiveRequest = null
        if (request != null) sessionRepository.finishRequest(request)
    }

    fun clearOnAccountSwitch() = synchronized(sessionRepository) {
        retireArchiveRead()
        _state.value = SalesUiState(
            draftId = UUID.randomUUID().toString(),
            message = "已切换账号，已清空工作台草稿",
        )
    }

    fun newDraft() = openDraft(null)

    fun reopenDraft(draftId: String) = openDraft(draftId)

    fun refreshSavedDrafts() {
        val (repo, session, request) = synchronized(sessionRepository) {
            if (localIo.isRetired || archiveRequest != null) return
            val (session, request) = captureOwnedRequest() ?: return
            val repo = salesDraftRepository
            if (repo == null) {
                _state.update { it.copy(archive = SalesDraftArchive.Failed) }
                sessionRepository.finishRequest(request)
                return
            }
            archiveRequest = request
            _state.update { it.copy(archive = SalesDraftArchive.Loading) }
            Triple(repo, session, request)
        }

        fun finish(result: SalesDraftArchive?) = synchronized(sessionRepository) {
            sessionRepository.whileCurrent(session) {
                if (archiveRequest === request) {
                    archiveRequest = null
                    if (result != null && request.isActive()) _state.update {
                        it.copy(archive = if (localIo.isRetired) SalesDraftArchive.Idle else result)
                    }
                }
            }
        }

        enqueueLocal(request, { finish(SalesDraftArchive.Failed) }) {
            // Expiry can occur while this task is queued, before the next lifecycle tick.
            val mayRead = synchronized(sessionRepository) {
                !localIo.isRetired && sessionRepository.currentSession() == session &&
                    archiveRequest === request && request.isActive()
            }
            finish(if (!mayRead) null else SalesDraftArchive.Loaded(
                repo.listDrafts(CacheIdentity.from(session)).map {
                    SavedSalesDraft(it.draftId, it.updatedAtMs, it.syncState)
                },
            ))
        }
    }

    private fun openDraft(targetId: String?) {
        val current = _state.value
        if (current.isOpeningDraft || current.isLoading || targetId == current.draftId) return
        val repo = salesDraftRepository
        val (session, request) = captureOwnedRequest() ?: return
        _state.update { it.copy(isOpeningDraft = true) }
        fun showFailure() = sessionRepository.whileCurrent(session) {
            if (request.isActive()) _state.update { state ->
                if (state.draftId != current.draftId) state else state.copy(
                    isOpeningDraft = false, error = "无法安全打开该草稿，当前内容未丢弃。",
                )
            }
        }
        enqueueLocal(request, { showFailure() }) {
            if (!request.isActive() || sessionRepository.currentSession() != session) return@enqueueLocal
            // All production save/switch jobs use the same serial queue; do not
            // discard the current editor or invent a key when its save failed.
            val now = _state.value
            if (now.draftId != current.draftId || now.saveStatus in setOf(DraftSaveStatus.FAILED, DraftSaveStatus.SAVING)) {
                if (now.draftId == current.draftId) sessionRepository.whileCurrent(session) {
                    if (request.isActive()) _state.update { it.copy(isOpeningDraft = false,
                        error = "当前草稿还没保存成功，仍保留本页，请先重试保存。") }
                }
                return@enqueueLocal
            }
            val next = if (targetId == null) {
                SalesUiState(message = "已创建新销售草稿")
            } else {
                val loaded = repo?.readOwnedDraft(CacheIdentity.from(session), targetId)
                    ?: error("draft_not_found")
                val (draft, intents) = loaded
                val decoded = SalesDraftCodec.decodeLines(draft.linesJson)
                val lines = runCatching {
                    (decoded as? DecodeLinesResult.Success)?.lines?.map(SalesLineItem::restore)
                }.getOrNull()
                val flows = intents.filter { it.action == "prepare_handoff_v4" }
                val originalFlow = flows.singleOrNull()
                val readOnly = flows.isNotEmpty() || draft.status == "paid" ||
                    draft.syncState == "corrupt" || lines == null || intents.any { it.attempts != 0 }
                val totals = calculateTotals(lines.orEmpty())
                SalesUiState(
                    draftId = draft.draftId, lines = lines.orEmpty(), currency = draft.currency,
                    stage = draft.stage, status = draft.status, syncState = draft.syncState,
                    totalQuantity = totals.first, totalAmount = totals.second,
                    serverDraftId = draft.serverDraftId, preparationIntentKey = originalFlow?.idempotencyKey,
                    preparationDisposition = if (lines.isNullOrEmpty()) null else SalesIntentDisposition.UNIT_PROOF_REQUIRED,
                    handoffGate = if (lines.isNullOrEmpty()) null else SALES_UNIT_HOLD_CODE,
                    submissionLocked = readOnly,
                    saveStatus = if (readOnly) DraftSaveStatus.READ_ONLY else DraftSaveStatus.SAVED,
                    message = if (readOnly) "原草稿需核对，已只读打开，不会重新提交。" else "已恢复本机草稿。",
                )
            }
            sessionRepository.whileCurrent(session) {
                if (request.isActive()) _state.update {
                    if (it.draftId == current.draftId) next.copy(archive = it.archive) else it
                }
            }
        }
    }

    fun retryLocalSave() {
        if (_state.value.saveStatus == DraftSaveStatus.FAILED) persistLocalDraft()
    }

    fun selectConfirmedProduct(item: SearchMatchItem) {
        _state.update {
            it.copy(
                pendingProduct = item,
                quantityInput = "",
                priceInput = "",
                manualUnitInput = "",
                ambiguityWarning = null,
                error = null,
            )
        }
    }

    fun updatePendingQuantity(value: String) {
        _state.update { if (it.pendingProduct == null) it else it.copy(quantityInput = value, error = null) }
    }

    fun updatePendingPrice(value: String) {
        _state.update { if (it.pendingProduct == null) it else it.copy(priceInput = value, error = null) }
    }

    fun updatePendingUnit(value: String) {
        _state.update {
            if (it.pendingProduct == null || it.catalogSalesUnit != null) it
            else it.copy(manualUnitInput = value, error = null)
        }
    }

    fun clearPendingProduct() {
        _state.update {
            it.copy(pendingProduct = null, quantityInput = "", priceInput = "", manualUnitInput = "", error = null)
        }
    }

    fun confirmPendingProduct(): Boolean {
        val current = _state.value
        val product = current.pendingProduct
            ?: return rejectProductInput("请先在找货页人工确认商品")
        val quantity = current.quantityInput.trim().toBigDecimalOrNull()
        if (quantity == null || !validDecimal(quantity, 6, false)) {
            return rejectProductInput("请输入大于 0、最多六位小数的有效数量")
        }
        val price = current.priceInput.trim().toBigDecimalOrNull()
        if (price == null || !validDecimal(price, 2, true)) {
            return rejectProductInput("请填写有效单价，最多两位小数；空白不代表零价")
        }
        val catalogUnit = current.catalogSalesUnit
        val unit = catalogUnit ?: current.manualUnitInput.trim()
        if (!SalesDraftCodec.validSalesUnit(unit)) return rejectProductInput("请人工填写并确认有效的销售单位")
        val added = addItem(
            productId = product.productId,
            productName = product.name,
            quantity = quantity.toDouble(),
            unitPrice = price.toDouble(),
            packagingUnit = unit,
            unitSource = if (catalogUnit == null) DraftUnitSource.OPERATOR else DraftUnitSource.CATALOG,
            isAmbiguous = false, // Only the explicit confirmed-selection UI calls this flow.
        )
        if (added) clearPendingProduct()
        return added
    }

    private fun rejectProductInput(message: String): Boolean {
        _state.update { it.copy(error = message) }
        return false
    }

    /**
     * Add confirmed product to active cart.
     * R-003 & R-004: Ambiguous candidates or unconfirmed items MUST NOT be auto-added.
     */
    fun addItem(
        productId: String,
        productName: String,
        quantity: Double,
        unitPrice: Double,
        discountBps: Int = 0,
        quoteId: String? = null,
        packagingUnit: String = "件",
        note: String? = null,
        isAmbiguous: Boolean = false,
        unitSource: DraftUnitSource = DraftUnitSource.OPERATOR,
    ): Boolean {
        if (localIo.isRetired || _state.value.isOpeningDraft || sessionRepository.status.value.generation != ownerGeneration) return false
        if (_state.value.submissionLocked) return rejectLockedSubmission()
        if (isAmbiguous) {
            _state.update {
                it.copy(
                    ambiguityWarning = "该商品存在多规格或候选歧义，必须人工核对确认后再加单",
                    error = null,
                )
            }
            return false
        }

        if (!quantity.isFinite() || !validDecimal(BigDecimal.valueOf(quantity), 6, false)) {
            _state.update {
                it.copy(
                    error = "销售数量必须大于 0",
                    ambiguityWarning = null,
                )
            }
            return false
        }

        if (!unitPrice.isFinite() || !validDecimal(BigDecimal.valueOf(unitPrice), 2, true) ||
            !SalesDraftCodec.validSalesUnit(packagingUnit) || discountBps !in 0..10000
        ) {
            _state.update {
                it.copy(
                    error = "商品单价不能为负数",
                    ambiguityWarning = null,
                )
            }
            return false
        }

        val selectedDraftId = _state.value.draftId
        fun matches(line: SalesLineItem): Boolean =
            line.productId == productId && line.unitPrice == unitPrice && line.quoteId == quoteId &&
                line.packagingUnit == packagingUnit && line.unitSource == unitSource &&
                line.discountBps == discountBps && line.note == note
        val existing = _state.value.lines.firstOrNull(::matches)
        if (existing != null &&
            !validDecimal(BigDecimal.valueOf(existing.quantity) + BigDecimal.valueOf(quantity), 6, false)
        ) return rejectProductInput("合并数量超出允许范围")
        _state.update { current ->
            if (current.draftId != selectedDraftId) return@update current
            val existingIndex = current.lines.indexOfFirst(::matches)
            val updatedLines = current.lines.toMutableList()
            if (existingIndex >= 0) {
                val old = updatedLines[existingIndex]
                val merged = BigDecimal.valueOf(old.quantity).add(BigDecimal.valueOf(quantity))
                if (!validDecimal(merged, 6, false)) return@update current.copy(error = "合并数量超出允许范围")
                updatedLines[existingIndex] = old.copy(quantity = merged.toDouble())
            } else {
                updatedLines.add(
                    SalesLineItem(
                        productId = productId,
                        productName = productName,
                        quantity = quantity,
                        unitPrice = unitPrice,
                        discountBps = discountBps,
                        quoteId = quoteId,
                        packagingUnit = packagingUnit,
                        note = note,
                        unitSource = unitSource,
                    )
                )
            }
            val (totalQty, totalAmt) = calculateTotals(updatedLines)
            current.copy(
                lines = updatedLines,
                totalQuantity = totalQty,
                totalAmount = totalAmt,
                stage = "draft", // modifying lines resets ready stage
                ambiguityWarning = null,
                error = null,
                message = "已添加商品: $productName",
            )
        }
        if (_state.value.draftId != selectedDraftId) return false
        persistLocalDraft()
        return true
    }

    fun adjustLineQuantity(lineKey: String, step: Long) {
        val line = _state.value.lines.firstOrNull { it.lineKey == lineKey } ?: return
        updateLineQuantity(lineKey, (BigDecimal.valueOf(line.quantity) + BigDecimal.valueOf(step)).toDouble())
    }

    fun updateLineQuantity(lineKey: String, newQuantity: Double) {
        if (localIo.isRetired || _state.value.isOpeningDraft || sessionRepository.status.value.generation != ownerGeneration) return
        if (_state.value.submissionLocked) { rejectLockedSubmission(); return }
        if (!newQuantity.isFinite() || !validDecimal(BigDecimal.valueOf(newQuantity), 6, false)) {
            rejectProductInput("数量须大于 0；删除请使用该行的删除按钮。")
            return
        }
        _state.update { current ->
            val updatedLines = current.lines.map { line ->
                if (line.lineKey == lineKey) line.copy(quantity = newQuantity) else line
            }
            val (totalQty, totalAmt) = calculateTotals(updatedLines)
            current.copy(
                lines = updatedLines,
                totalQuantity = totalQty,
                totalAmount = totalAmt,
                stage = "draft",
                error = null,
            )
        }
        persistLocalDraft()
    }

    fun updateLinePrice(lineKey: String, newPrice: Double) {
        if (localIo.isRetired || _state.value.isOpeningDraft || sessionRepository.status.value.generation != ownerGeneration) return
        if (_state.value.submissionLocked) { rejectLockedSubmission(); return }
        if (!newPrice.isFinite() || !validDecimal(BigDecimal.valueOf(newPrice), 2, true)) {
            _state.update { it.copy(error = "商品单价不能为负数") }
            return
        }
        _state.update { current ->
            val updatedLines = current.lines.map { line ->
                if (line.lineKey == lineKey) line.copy(unitPrice = newPrice) else line
            }
            val (totalQty, totalAmt) = calculateTotals(updatedLines)
            current.copy(
                lines = updatedLines,
                totalQuantity = totalQty,
                totalAmount = totalAmt,
                stage = "draft",
                error = null,
            )
        }
        persistLocalDraft()
    }

    fun removeLine(lineKey: String) {
        if (localIo.isRetired || sessionRepository.status.value.generation != ownerGeneration) return
        val previous = _state.getAndUpdate { current ->
            if (current.isOpeningDraft || current.lines.none { it.lineKey == lineKey }) return@getAndUpdate current
            if (current.submissionLocked) return@getAndUpdate current.copy(error = LOCKED_SUBMISSION_MESSAGE)
            val updatedLines = current.lines.filterNot { it.lineKey == lineKey }
            val (totalQty, totalAmt) = calculateTotals(updatedLines)
            current.copy(
                lines = updatedLines,
                totalQuantity = totalQty,
                totalAmount = totalAmt,
                stage = "draft",
                error = null,
                message = "已移除商品行",
            )
        }
        // Use the actual preimage of this update, not an earlier read or a retried lambda's flag.
        if (!previous.isOpeningDraft && !previous.submissionLocked && previous.lines.any { it.lineKey == lineKey }) {
            persistLocalDraft()
        }
    }

    /**
     * Mark draft as ready for cashier handoff.
     * Empty drafts or drafts with non-positive values cannot be marked ready.
     */
    fun markReady(): Boolean {
        if (localIo.isRetired || _state.value.isOpeningDraft || sessionRepository.status.value.generation != ownerGeneration) return false
        if (_state.value.submissionLocked) return rejectLockedSubmission()
        val current = _state.value
        if (current.lines.isEmpty()) {
            _state.update { it.copy(error = "草稿为空，无法标记为就绪") }
            return false
        }
        if (current.lines.any { !it.quantity.isFinite() || !it.unitPrice.isFinite() ||
                it.quantity <= 0.0 || it.unitPrice <= 0.0 }) {
            _state.update { it.copy(error = "草稿中包含非法数量或单价") }
            return false
        }

        _state.update {
            it.copy(
                stage = "ready",
                error = null,
                message = "已完成本机人工确认。销售单位尚未由后台核验，未发起交接。",
            )
        }
        persistLocalDraft()
        return true
    }

    /** Read the original reason for HOLD; this candidate cannot issue formal sales requests. */
    fun submitHandoff(notes: String? = null): Boolean {
        val current = _state.value
        if (current.isLoading || current.isOpeningDraft) return false
        if (!notes.isNullOrBlank()) {
            _state.update { it.copy(error = "交接接口不支持 notes 字段，未发送请求", message = null,
                preparationDisposition = SalesIntentDisposition.CONTRACT_REJECTED) }
            return false
        }
        if (current.saveStatus !in setOf(DraftSaveStatus.SAVED, DraftSaveStatus.READ_ONLY)) {
            _state.update { it.copy(error = "请先保存本机草稿，未发送任何请求。") }
            return false
        }
        val repo = salesDraftRepository ?: return false
        val (session, request) = captureOwnedRequest() ?: return false
        val scope = CacheIdentity.from(session)
        _state.update { it.copy(isLoading = true, error = null, message = null) }
        fun showFailure() = sessionRepository.whileCurrent(session) {
            if (request.isActive()) _state.update { state ->
                if (state.draftId != current.draftId) state else state.copy(isLoading = false,
                    error = "本机原件需核对；未读取原结果，未发送任何请求。")
            }
        }
        enqueueLocal(request, { showFailure() }) {
            sessionRepository.whileCurrent(session) {
                if (!request.isActive() || _state.value.draftId != current.draftId) return@whileCurrent
                val result = if (apiClient == null) repo.inspectPreparationOffline(scope, current.draftId)
                    else repo.prepareForHandoff(scope, current.draftId, apiClient, sessionRepository)
                _state.update { state ->
                    if (state.draftId != current.draftId) state else state.copy(
                        isLoading = false, preparationDisposition = result.disposition,
                        preparationIntentKey = result.intentKey ?: state.preparationIntentKey,
                        handoffGate = result.code, error = result.message, message = null,
                    )
                }
            }
        }
        return false
    }

    /**
     * Formal receipt verification hook.
     * Only when verified receipt arrives from cashier POS can status become 'paid'.
     */
    fun recordReceiptOutcome(isReceiptPaid: Boolean, externalReceiptRef: String?) {
        if (_state.value.handoff == null) return
        _state.update { current ->
            if (isReceiptPaid) {
                current.copy(
                    status = "paid",
                    handoff = current.handoff?.copy(
                        state = "confirmed",
                        externalRef = externalReceiptRef,
                        isSettled = true,
                    ),
                    message = "收银回执已核验确认结算 (参考号: $externalReceiptRef)",
                    error = null,
                )
            } else {
                current.copy(
                    status = "pending",
                    handoff = current.handoff?.copy(
                        state = "receipt_unknown",
                        externalRef = externalReceiptRef,
                        isSettled = false,
                    ),
                    message = "收银回执未确认付款，保持待核对状态 (未结算)",
                    error = null,
                )
            }
        }
        persistLocalDraft()
    }

    private fun rejectLockedSubmission(): Boolean {
        _state.update { it.copy(error = LOCKED_SUBMISSION_MESSAGE) }
        return false
    }

    private fun calculateTotals(lines: List<SalesLineItem>): Pair<Double, BigDecimal> =
        lines.fold(BigDecimal.ZERO) { qty, line -> qty + BigDecimal.valueOf(line.quantity) }.toDouble() to
            lines.fold(ZERO_MONEY) { sum, line -> sum + line.exactSubtotal }

    private fun persistLocalDraft() {
        val repo = salesDraftRepository
        val captured = captureOwnedRequest()
        if (repo == null || captured == null) {
            captured?.let { sessionRepository.finishRequest(it.second) }
            _state.update { it.copy(saveStatus = DraftSaveStatus.FAILED, error = "本机草稿尚未保存。") }
            return
        }
        val (session, request) = captured
        val current = _state.value
        val scope = CacheIdentity.from(session)
        _state.update { it.copy(saveStatus = DraftSaveStatus.SAVING) }
        fun updateSaved(outcome: DraftSaveStatus) {
            _state.update { live ->
                if (live.draftId != current.draftId || live.lines != current.lines || live.stage != current.stage) live
                else live.copy(saveStatus = outcome,
                    error = if (outcome == DraftSaveStatus.FAILED) "本地草稿保存失败，未提交；请保留当前内容" else null)
            }
        }
        fun showFailure() = sessionRepository.whileCurrent(session) {
            if (request.isActive()) updateSaved(DraftSaveStatus.FAILED)
        }
        enqueueLocal(request, { showFailure() }) {
            sessionRepository.whileCurrent(session) {
                if (!request.isActive()) return@whileCurrent
                repo.persistLocalSnapshot(current.draftId, scope,
                    current.lines.map(SalesLineItem::toFormalDraftLine), current.currency, current.stage)
                updateSaved(DraftSaveStatus.SAVED)
            }
        }
    }
}
