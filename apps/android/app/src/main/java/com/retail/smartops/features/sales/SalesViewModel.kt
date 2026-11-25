package com.retail.smartops.features.sales

import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.features.search.SearchMatchItem
import java.util.UUID
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

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
) {
    val lineSubtotal: Double
        get() = quantity * unitPrice * (1.0 - discountBps / 10000.0)

    fun toFormalDraftLine(): FormalDraftLine =
        FormalDraftLine(
            lineKey = lineKey,
            productId = productId,
            productName = productName,
            quantity = "%.4f".format(Locale.ROOT, quantity).trimEnd('0').trimEnd('.'),
            unitPrice = "%.2f".format(Locale.ROOT, unitPrice),
            discountBps = discountBps,
            quoteId = quoteId,
        )
}

data class SalesHandoffInfo(
    val handoffId: String,
    val state: String, // "requested", "sent", "receipt_unknown", "confirmed", "pending_delivery"
    val deliveryRef: String? = null,
    val externalRef: String? = null,
    val isSettled: Boolean = false, // Strictly false until verified receipt arrives
    val requestedAtMs: Long,
)

data class SalesUiState(
    val draftId: String = UUID.randomUUID().toString(),
    val lines: List<SalesLineItem> = emptyList(),
    val stage: String = "draft", // Local human confirmation, not the server ready gate.
    val status: String = "pending", // "pending" or "paid"
    val syncState: String = "local_only", // "local_only", "synced", "pending_sync", "corrupt"
    val currency: String = "CNY",
    val totalQuantity: Double = 0.0,
    val totalAmount: Double = 0.0,
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
)

private val salesIoExecutor: Executor = Executors.newSingleThreadExecutor { runnable ->
    Thread(runnable, "sales-io").apply { isDaemon = true }
}

class SalesViewModel(
    private val salesDraftRepository: SalesDraftRepository? = null,
    private val sessionRepository: SessionRepository,
    private val apiClient: HttpApiClient? = null,
    private val clock: () -> Long = System::currentTimeMillis,
    private val ioExecutor: Executor = salesIoExecutor,
) {
    private val _state = MutableStateFlow(SalesUiState())
    val state: StateFlow<SalesUiState> = _state.asStateFlow()

    init {
        sessionRepository.onScopeInvalidated {
            clearOnAccountSwitch()
        }
    }

    fun clearOnAccountSwitch() {
        _state.value = SalesUiState(
            draftId = UUID.randomUUID().toString(),
            message = "已切换账号，已清空工作台草稿",
        )
    }

    fun newDraft() {
        val newId = UUID.randomUUID().toString()
        _state.value = SalesUiState(
            draftId = newId,
            message = "已创建新销售草稿",
        )
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
            if (it.pendingProduct == null || !it.pendingProduct.unit.isNullOrBlank()) it
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
        val quantity = current.quantityInput.trim().toDoubleOrNull()
        if (quantity == null || !quantity.isFinite() || quantity <= 0.0) {
            return rejectProductInput("请输入大于 0 的有效数量")
        }
        val price = current.priceInput.trim().toDoubleOrNull()
        if (price == null || !price.isFinite() || price < 0.0) {
            return rejectProductInput("请输入不小于 0 的有效单价；空白不代表零价")
        }
        val unit = product.unit?.takeIf { it.isNotBlank() } ?: current.manualUnitInput.trim()
        if (unit.isBlank()) return rejectProductInput("来源未提供单位，请人工填写并确认销售单位")
        val added = addItem(
            productId = product.productId,
            productName = product.name,
            quantity = quantity,
            unitPrice = price,
            packagingUnit = unit,
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
    ): Boolean {
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

        if (quantity <= 0.0) {
            _state.update {
                it.copy(
                    error = "销售数量必须大于 0",
                    ambiguityWarning = null,
                )
            }
            return false
        }

        if (unitPrice < 0.0) {
            _state.update {
                it.copy(
                    error = "商品单价不能为负数",
                    ambiguityWarning = null,
                )
            }
            return false
        }

        _state.update { current ->
            val existingIndex = current.lines.indexOfFirst {
                it.productId == productId && it.unitPrice == unitPrice && it.quoteId == quoteId &&
                    it.packagingUnit == packagingUnit
            }
            val updatedLines = current.lines.toMutableList()
            if (existingIndex >= 0) {
                val old = updatedLines[existingIndex]
                updatedLines[existingIndex] = old.copy(quantity = old.quantity + quantity)
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
        persistLocalDraft()
        return true
    }

    fun updateLineQuantity(lineKey: String, newQuantity: Double) {
        if (_state.value.submissionLocked) { rejectLockedSubmission(); return }
        if (newQuantity <= 0.0) {
            removeLine(lineKey)
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
        if (_state.value.submissionLocked) { rejectLockedSubmission(); return }
        if (newPrice < 0.0) {
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
        if (_state.value.submissionLocked) { rejectLockedSubmission(); return }
        _state.update { current ->
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
        persistLocalDraft()
    }

    /**
     * Mark draft as ready for cashier handoff.
     * Empty drafts or drafts with non-positive values cannot be marked ready.
     */
    fun markReady(): Boolean {
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
                message = "已完成人工确认；提交时先核对后台商品行，交接还需已核价格来源",
            )
        }
        persistLocalDraft()
        return true
    }

    /**
     * Prepare the owned draft and confirmed lines on IO; price/ready/handoff remain HOLD.
     * The return value never claims a successful handoff in this limited slice.
     */
    fun submitHandoff(notes: String? = null): Boolean {
        val current = _state.value
        if (current.isLoading) return false
        if (!notes.isNullOrBlank()) {
            _state.update { it.copy(error = "交接接口不支持 notes 字段，未发送请求", message = null,
                preparationDisposition = SalesIntentDisposition.CONTRACT_REJECTED) }
            return false
        }
        if (current.stage != "ready" || current.lines.isEmpty() || current.status != "pending") {
            _state.update { it.copy(error = "必须先人工确认有效商品行，且草稿尚未结算", message = null,
                preparationDisposition = SalesIntentDisposition.CONTRACT_REJECTED) }
            return false
        }
        val repo = salesDraftRepository
        if (repo == null) {
            _state.update { it.copy(error = "本地意图存储不可用，未发送请求", message = null,
                preparationDisposition = SalesIntentDisposition.CONTRACT_REJECTED) }
            return false
        }
        val captured = sessionRepository.captureRequest()
        if (captured == null) {
            _state.update { it.copy(error = "请先登录，未发送交接", message = null,
                preparationDisposition = SalesIntentDisposition.PERMISSION_DENIED) }
            return false
        }
        val (session, request) = captured
        val capabilities = sessionRepository.getCapabilities()
        val capabilityVersion = sessionRepository.getCapabilityVersion()
        val scope = CacheIdentity(session.organizationId, session.storeId, session.subjectId)
        _state.update { it.copy(isLoading = true, submissionLocked = true, error = null,
            message = "正在核对后台草稿与商品行，尚未发送交接") }
        fun guarded(block: () -> Unit): Boolean {
            var active = false
            sessionRepository.whileCurrent(session) {
                if (request.isActive() && _state.value.draftId == current.draftId &&
                    sessionRepository.getCapabilities() == capabilities &&
                    sessionRepository.getCapabilityVersion() == capabilityVersion) { block(); active = true }
            }
            return active
        }
        ioExecutor.execute {
            var enteredPreparation = false
            try {
                var queued: com.retail.smartops.core.database.SalesSyncIntentEntity? = null
                if (!guarded { queued = repo.queuePreparation(scope, current.draftId) }) return@execute
                val client = apiClient
                val result = if (client == null) repo.inspectPreparationOffline(scope, current.draftId) else {
                    enteredPreparation = true
                    repo.prepareForHandoff(scope, current.draftId, client, sessionRepository)
                }
                guarded {
                    _state.update { state -> state.copy(isLoading = false,
                        serverDraftId = result.serverDraftId ?: state.serverDraftId,
                        preparationIntentKey = result.intentKey ?: queued!!.idempotencyKey,
                        preparationDisposition = result.disposition,
                        isOffline = result.disposition == SalesIntentDisposition.OFFLINE_NOT_SENT,
                        handoffGate = if (result.disposition == SalesIntentDisposition.ACCEPTED) "price_source_hold" else result.code,
                        syncState = if (result.disposition == SalesIntentDisposition.ACCEPTED) "synced" else state.syncState,
                        error = if (result.disposition in setOf(SalesIntentDisposition.ACCEPTED,
                            SalesIntentDisposition.OFFLINE_NOT_SENT)) null else result.message,
                        message = result.message,
                    ) } // No manufactured handoff object, receipt or payment state.
                }
            } catch (error: Exception) {
                guarded {
                    _state.update { it.copy(isLoading = false, isOffline = false,
                        preparationDisposition = if (enteredPreparation || error.message == "legacy_server_identity_unknown" ||
                            error !is IllegalArgumentException) SalesIntentDisposition.RESULT_UNKNOWN
                            else SalesIntentDisposition.CONTRACT_REJECTED,
                        error = "本地草稿或原意图需核对，未声明交接成功", message = null,
                        handoffGate = error.message ?: "local_intent_hold") }
                }
            } finally { sessionRepository.finishRequest(request) }
        }
        // This frozen slice prepares lines only. A price-source HOLD is never a successful handoff.
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
        _state.update { it.copy(error = "原意图已固定，不能改内容或换键重派；请先核对原结果") }
        return false
    }

    private fun calculateTotals(lines: List<SalesLineItem>): Pair<Double, Double> {
        var qty = 0.0
        var amt = 0.0
        for (line in lines) {
            qty += line.quantity
            amt += line.lineSubtotal
        }
        return qty to amt
    }

    private fun persistLocalDraft() {
        val repo = salesDraftRepository ?: return
        val session = sessionRepository.currentSession() ?: return
        val current = _state.value
        val scope = CacheIdentity(
            organizationId = session.organizationId,
            storeId = session.storeId,
            subjectId = session.subjectId,
        )
        ioExecutor.execute {
            try {
                sessionRepository.whileCurrent(session) {
                    if (_state.value.draftId != current.draftId) return@whileCurrent
                    val lines = current.lines.map { it.toFormalDraftLine() }
                    if (repo.getDraft(current.draftId) == null) {
                        repo.createLocalDraft(current.draftId, scope, lines, current.currency, current.stage)
                    } else {
                        repo.saveDraft(
                            draftId = current.draftId, scope = scope, lines = lines,
                            currency = current.currency, stage = current.stage,
                            status = current.status, syncState = current.syncState,
                        )
                    }
                }
            } catch (_: Exception) {
                sessionRepository.whileCurrent(session) {
                    _state.update { if (it.draftId != current.draftId) it else it.copy(
                        error = "本地草稿保存失败，未提交；请保留当前内容", message = null,
                    ) }
                }
            }
        }
    }
}
