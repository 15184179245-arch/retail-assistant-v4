package com.retail.smartops.features.agents

import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.network.ApiResult
import java.util.UUID
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicLong

enum class ChatPhase { SIGNED_OUT, IDLE, BUSY, KNOWN, NEEDS_CHECK }
data class AgentChatState(val phase: ChatPhase, val runId: String? = null,
    val receipt: AgentRunReceipt? = null, val failure: ApiResult.Failure? = null)

/** UI-thread controller. Work runs on executor; deliver must enqueue on the same UI thread.
 * Retain across recompositions; close on owner destruction. No network retries are automatic.
 */
class AgentChatViewModel(
    private val sessions: SessionRepository,
    private val gateway: AgentRunGateway,
    private val locators: AgentRunLocatorStore,
    private val executor: Executor,
    private val deliver: (() -> Unit) -> Unit,
    private val online: () -> Boolean = { true },
    private val newRunId: () -> String = { UUID.randomUUID().toString() },
    private val changed: (AgentChatState) -> Unit = {},
) : AutoCloseable {
    private var boundSession: Session? = sessions.currentSession()
    private var current = AgentChatState(if (boundSession == null) ChatPhase.SIGNED_OUT else ChatPhase.IDLE)
    private var operation = 0L
    private var command: CreateRunCommand? = null
    private var readConfirmed404 = false
    private var closed = false
    private val identityEpoch = AtomicLong(0)
    @Volatile private var locatorUsable = true
    private val removeListener = sessions.onScopeInvalidated {
        val event = identityEpoch.incrementAndGet()
        try { locators.clear() } catch (_: Exception) { locatorUsable = false }
        deliver {
            if (!closed && identityEpoch.get() == event) {
                // Do not erase new work started after this invalidation was queued.
                syncIdentity()
                publish()
            }
        }
    }

    val state: AgentChatState get() { syncIdentity(); return current }

    private fun syncIdentity() {
        val session = sessions.currentSession()
        if (session != boundSession) {
            boundSession = session
            operation++
            command = null
            readConfirmed404 = false
            current = AgentChatState(if (session == null) ChatPhase.SIGNED_OUT else ChatPhase.IDLE)
        }
    }
    private fun publish() { if (!closed) changed(state) }

    /** Explicit restore: reads the saved run only, never resends a question. */
    fun restore() {
        syncIdentity()
        if (closed || current.phase == ChatPhase.BUSY) return
        val session = boundSession ?: return
        val locator = try {
            check(locatorUsable)
            locators.load(AgentIdentity.from(session))
        } catch (_: Exception) { localFailure("LOCATOR_STORAGE_ERROR"); return }
        if (locator != null) {
            current = AgentChatState(ChatPhase.NEEDS_CHECK, locator.runId)
            refresh()
        }
    }

    fun submit(text: String) {
        syncIdentity()
        if (closed || current.phase == ChatPhase.BUSY || current.phase == ChatPhase.NEEDS_CHECK
            || current.receipt?.state?.terminal == false) return
        val session = boundSession ?: return
        if (!online()) { localFailure("OFFLINE"); return }
        if (text.isBlank() || text.trim().length > MAX_AGENT_MESSAGE_CHARS) { localFailure("INPUT_INVALID"); return }
        val next = CreateRunCommand(newRunId(), text.trim())
        try {
            check(locatorUsable)
            if (!sessions.whileCurrent(session) {
                locators.save(RunLocator(AgentIdentity.from(session), next.runId))
            }) { syncIdentity(); publish(); return }
        } catch (_: Exception) { localFailure("LOCATOR_STORAGE_ERROR"); return }
        command = next
        current = AgentChatState(ChatPhase.IDLE, next.runId)
        execute(createCommand = true) { gateway.create(it, next) }
    }

    fun refresh() {
        syncIdentity()
        if (closed || current.phase == ChatPhase.BUSY || current.runId == null) return
        val id = current.runId!!
        execute(readOperation = true) { gateway.read(it, id) }
    }

    /** User must first query the original run; this only replays the original create. */
    fun retryCreate() {
        syncIdentity()
        if (closed || current.phase != ChatPhase.NEEDS_CHECK || !readConfirmed404) return
        val original = command ?: return // process restart lost parameters: GET only
        execute(createCommand = true) { gateway.create(it, original) }
    }

    /** Only after a person has checked the original run and chosen to abandon its local locator. */
    fun discardAfterReview() {
        syncIdentity()
        if (closed || current.phase != ChatPhase.NEEDS_CHECK || !readConfirmed404) return
        val session = boundSession ?: return
        val epoch = identityEpoch.get()
        try {
            check(locatorUsable)
            if (!sessions.whileCurrent(session) { locators.clear() }) { syncIdentity(); publish(); return }
        } catch (_: Exception) { localFailure("LOCATOR_STORAGE_ERROR"); return }
        syncIdentity()
        if (boundSession != session || identityEpoch.get() != epoch) return
        operation++
        command = null
        readConfirmed404 = false
        current = AgentChatState(ChatPhase.IDLE)
        publish()
    }

    fun confirm(approve: Boolean, reason: String) {
        syncIdentity()
        if (closed || current.phase != ChatPhase.KNOWN || !online()) return
        val receipt = current.receipt ?: return
        if (receipt.execution?.allowsHumanDecision == false) return
        val pending = receipt.pendingAction ?: return
        if (reason.isBlank() || reason.length > 500) return
        // Immutable displayed snapshot. Never fetch a new version and silently approve it.
        execute { gateway.confirm(it, receipt.runId, pending, approve, reason) }
    }

    fun cancel() {
        syncIdentity()
        if (closed || current.phase != ChatPhase.KNOWN || !online()
            || current.receipt?.state?.terminal != false
            || current.receipt?.execution?.cancelRequested == true) return
        val id = current.runId ?: return
        execute { session ->
            val result = gateway.cancel(session, id)
            if (result == ApiResult.Cancelled || (result is ApiResult.Failure && result.status == 401)) result
            else gateway.read(session, id) // only a durable read establishes the outcome
        }
    }

    private fun execute(createCommand: Boolean = false, readOperation: Boolean = false,
                        action: (Session) -> ApiResult<AgentRunReceipt>) {
        val session = boundSession ?: return
        if (!online()) { localFailure("OFFLINE"); return }
        val id = current.runId ?: return
        val epoch = identityEpoch.get()
        val sequence = ++operation
        readConfirmed404 = false
        // Hide old answer/confirmation while querying; a failed revalidation cannot retain them.
        current = AgentChatState(ChatPhase.BUSY, id)
        publish()
        try {
            executor.execute {
                val result = try {
                    if (epoch != identityEpoch.get() || sessions.currentSession() != session) ApiResult.Cancelled
                    else action(session)
                }
                    catch (_: Exception) { ApiResult.Failure(0, "REQUEST_UNKNOWN", "请查询原运行，避免重复操作。", null) }
                deliver {
                    syncIdentity()
                    if (closed || sequence != operation || epoch != identityEpoch.get() || boundSession != session) return@deliver
                    current = when (result) {
                        is ApiResult.Success -> {
                            if (result.data.runId != id) AgentChatState(ChatPhase.NEEDS_CHECK, id,
                                failure = ApiResult.Failure(0, "RUN_ID_MISMATCH", "请查询原运行。", null))
                            else {
                                command = null // a known receipt ends create replay eligibility
                                readConfirmed404 = false
                                AgentChatState(ChatPhase.KNOWN, id, result.data)
                            }
                        }
                        is ApiResult.Failure -> {
                            if (createCommand && result.status in setOf(400, 403, 422)) command = null
                            readConfirmed404 = readOperation && result.status == 404
                            AgentChatState(ChatPhase.NEEDS_CHECK, id, failure = result)
                        }
                        ApiResult.Cancelled -> AgentChatState(ChatPhase.NEEDS_CHECK, id)
                    }
                    publish()
                }
            }
        } catch (_: Exception) {
            current = AgentChatState(ChatPhase.NEEDS_CHECK, id,
                failure = ApiResult.Failure(0, "REQUEST_UNKNOWN", "请查询原运行。", null))
            publish()
        }
    }

    private fun localFailure(code: String) {
        current = AgentChatState(if (current.runId == null) ChatPhase.IDLE else ChatPhase.NEEDS_CHECK,
            current.runId, failure = ApiResult.Failure(0, code, "请检查连接或本地存储后继续。", null))
        publish()
    }
    override fun close() {
        closed = true
        operation++
        removeListener()
        command = null
        current = AgentChatState(ChatPhase.SIGNED_OUT)
    }
}
