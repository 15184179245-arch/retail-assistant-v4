package com.retail.smartops.features.agents

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Handler
import android.os.Looper
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import com.retail.smartops.core.ui.ErrorPanel
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.parseIsoInstant
import java.util.UUID
import java.util.concurrent.Executors
import kotlinx.coroutines.delay

private const val SUGGEST_REVIEW_TOOL_KEY = "tasks.suggest_review"
private const val SUGGEST_REVIEW_TOOL_VERSION = "1"
private const val MAX_QUESTION_LENGTH = MAX_AGENT_MESSAGE_CHARS
private const val MAX_REASON_LENGTH = 500

// In-process page-local record of cancel requests. It survives leaving/re-entering the page so
// a GET restore cannot expose an approvable pending action after the user already cancelled.
private val rememberedCancelRequests: MutableSet<String> =
    java.util.Collections.synchronizedSet(mutableSetOf())

internal const val SUGGEST_REVIEW_IMPACT =
    "仅创建 suggested 任务建议；不分派、不接受、不修改正式营业数据。"

private val SUGGEST_REVIEW_FIELDS =
    listOf("title", "description", "reason", "reviewer_id", "proposal_ref")
private val SUGGEST_REVIEW_REQUIRED =
    setOf("title", "description", "reason", "reviewer_id")

/** One argument of a pending tool call, rendered in full for human review. */
data class AgentArgumentRow(
    val key: String,
    val value: String,
    val required: Boolean,
    val registered: Boolean,
)

/** Everything the page may show and enable for one pending confirmation snapshot. */
data class PendingReview(
    val toolKey: String,
    val toolVersion: String,
    val supportedTool: Boolean,
    val argumentsValid: Boolean,
    val argumentRows: List<AgentArgumentRow>,
    val reviewerId: String?,
    val impact: String,
    val expiresAt: String,
    val expired: Boolean,
    val inputDigest: String,
    val runVersion: Long,
    val stepVersion: Long,
    val approveAllowed: Boolean,
    val rejectAllowed: Boolean,
    val approvalBlockedReason: String?,
)

/** Page action availability, derived from the same functions production Compose uses. */
data class AgentActions(
    val canSubmit: Boolean,
    val canRefresh: Boolean,
    val canCancel: Boolean,
    val canApprove: Boolean,
    val canReject: Boolean,
    val canDiscardLocator: Boolean,
    val busy: Boolean,
    val offline: Boolean,
)

/**
 * Strict mirror of the registered `tasks.suggest_review@1` input contract.
 * Unknown keys, non-string values, blank required fields and over-length fields all fail closed.
 */
fun isValidSuggestReviewArguments(arguments: Map<String, Any?>): Boolean {
    if (!SUGGEST_REVIEW_FIELDS.containsAll(arguments.keys)) return false

    fun text(key: String, minLength: Int, maxLength: Int): Boolean {
        val value = arguments[key]
        if (value !is String) return false
        if (value.length !in minLength..maxLength) return false
        return value.trim().isNotEmpty()
    }

    if (!text("title", 1, 200)) return false
    if (!text("description", 1, 4000)) return false
    if (!text("reason", 1, 4000)) return false
    if (!text("reviewer_id", 1, 36)) return false

    val proposalRef = arguments["proposal_ref"]
    return proposalRef == null || (proposalRef is String && proposalRef.length <= 200)
}

private fun renderArgument(value: Any?): String = when (value) {
    null -> "（空）"
    is String -> if (value.isEmpty()) "（空字符串）" else value
    is Map<*, *> -> value.entries.joinToString(prefix = "{", postfix = "}") {
        "${it.key}=${renderArgument(it.value)}"
    }
    is List<*> -> value.joinToString(prefix = "[", postfix = "]") { renderArgument(it) }
    else -> value.toString()
}

private fun argumentRows(
    pending: PendingAction,
    supported: Boolean,
): List<AgentArgumentRow> {
    val keys = if (supported) {
        (pending.arguments.keys + SUGGEST_REVIEW_REQUIRED).distinct().sortedWith(
            compareBy(
                { key ->
                    SUGGEST_REVIEW_FIELDS.indexOf(key).let {
                        if (it < 0) SUGGEST_REVIEW_FIELDS.size else it
                    }
                },
                { key -> key },
            ),
        )
    } else {
        pending.arguments.keys.sorted()
    }
    return keys.map { key ->
        AgentArgumentRow(
            key = key,
            value = if (pending.arguments.containsKey(key)) {
                renderArgument(pending.arguments[key])
            } else {
                "（缺失）"
            },
            required = supported && key in SUGGEST_REVIEW_REQUIRED,
            registered = supported && key in SUGGEST_REVIEW_FIELDS,
        )
    }
}

/**
 * Reviews a pending confirmation from the displayed receipt only. Approval requires the
 * registered tool, strictly valid full arguments, a live deadline, a usable network and a
 * phase that is still trusted; rejection only requires the run to be known and online.
 */
fun reviewPendingAction(
    state: AgentChatState,
    connected: Boolean,
    cancelRequested: Boolean,
    nowMillis: Long,
): PendingReview? {
    val receipt = state.receipt ?: return null
    if (receipt.state != RunState.WAITING_FOR_HUMAN) return null
    val pending = receipt.pendingAction ?: return null

    val supported = pending.toolKey == SUGGEST_REVIEW_TOOL_KEY &&
        pending.version == SUGGEST_REVIEW_TOOL_VERSION
    val argumentsValid = supported && isValidSuggestReviewArguments(pending.arguments)
    val expiresAtMillis = parseIsoInstant(pending.expiresAt)
    val expired = expiresAtMillis == null || expiresAtMillis <= nowMillis
    val phaseKnown = state.phase == ChatPhase.KNOWN
    val serverBlocked = receipt.execution?.allowsHumanDecision == false

    val blocked = buildList {
        if (!supported) add("该工具未在本版本登记，只能拒绝或重新读取。")
        if (supported && !argumentsValid) add("参数缺失、含未登记字段或类型不正确，不能批准。")
        if (expired) add("确认已过期，请重新读取。")
        if (!connected) add("当前离线，禁止批准。")
        if (cancelRequested) add("已请求取消，终态前禁止再次确认。")
        if (serverBlocked) add("后台状态已变化，请重新读取；当前不能确认。")
        if (!phaseKnown) add("当前不在可确认状态。")
    }

    return PendingReview(
        toolKey = pending.toolKey,
        toolVersion = pending.version,
        supportedTool = supported,
        argumentsValid = argumentsValid,
        argumentRows = argumentRows(pending, supported),
        reviewerId = (pending.arguments["reviewer_id"] as? String)?.takeIf { it.isNotBlank() },
        impact = if (supported) {
            SUGGEST_REVIEW_IMPACT
        } else {
            "未在本版本登记的工具：影响未知，禁止批准；只能拒绝或重新读取。"
        },
        expiresAt = pending.expiresAt,
        expired = expired,
        inputDigest = pending.inputDigest,
        runVersion = pending.runVersion,
        stepVersion = pending.stepVersion,
        approveAllowed = blocked.isEmpty(),
        rejectAllowed = phaseKnown && connected && !cancelRequested && !serverBlocked,
        approvalBlockedReason = blocked.takeIf { it.isNotEmpty() }?.joinToString("；"),
    )
}

/**
 * Only the current, known-state receipt may be rendered. A previous receipt must never be
 * re-displayed during BUSY/NEEDS_CHECK: after a permission/source revalidation failure the old
 * answer, citations and pending arguments are no longer authorised to be shown.
 */
fun receiptForDisplay(state: AgentChatState): AgentRunReceipt? =
    if (state.phase == ChatPhase.KNOWN) state.receipt else null

/** A waiting run without a pending snapshot must explain why nothing can be approved. */
fun pendingUnavailableNotice(receipt: AgentRunReceipt): String? =
    if (receipt.state == RunState.WAITING_FOR_HUMAN && receipt.pendingAction == null) {
        "等待人工确认，但服务端没有给出可确认的快照；请重新读取，不能批准或拒绝。"
    } else {
        null
    }

/**
 * Derives every enabled/disabled control on the page. `get404RunId` is the run id for which
 * this page itself observed a GET response with status 404; only then may a confirmed 404
 * offer manual locator abandonment (a create/confirm/cancel POST 404 must never qualify).
 */
fun agentActions(
    state: AgentChatState,
    connected: Boolean,
    cancelRequested: Boolean,
    get404RunId: String?,
    nowMillis: Long,
): AgentActions {
    val receipt = state.receipt
    val busy = state.phase == ChatPhase.BUSY
    val known = state.phase == ChatPhase.KNOWN
    val review = reviewPendingAction(state, connected, cancelRequested, nowMillis)
    // Only a GET that the page itself observed returning 404 unlocks local abandonment.
    // A POST (create/confirm/cancel) 404 must never surface this affordance.
    val canDiscardLocator = connected &&
        state.phase == ChatPhase.NEEDS_CHECK &&
        state.runId != null &&
        state.runId == get404RunId &&
        state.failure?.status == 404

    return AgentActions(
        canSubmit = connected &&
            !busy &&
            state.phase != ChatPhase.NEEDS_CHECK &&
            state.phase != ChatPhase.SIGNED_OUT &&
            (receipt == null || receipt.state.terminal),
        canRefresh = connected &&
            !busy &&
            state.phase != ChatPhase.SIGNED_OUT &&
            state.runId != null,
        canCancel = connected &&
            known &&
            receipt != null &&
            !receipt.state.terminal &&
            !cancelRequested && receipt.execution?.cancelRequested != true,
        canApprove = review?.approveAllowed == true,
        canReject = review?.rejectAllowed == true,
        canDiscardLocator = canDiscardLocator,
        busy = busy,
        offline = !connected,
    )
}

fun decisionEnabled(review: PendingReview?, approve: Boolean, reason: String): Boolean {
    if (review == null) return false
    if (reason.isBlank() || reason.length > MAX_REASON_LENGTH) return false
    return if (approve) review.approveAllowed else review.rejectAllowed
}

fun agentPhaseLabel(state: AgentChatState): String = when (state.phase) {
    ChatPhase.SIGNED_OUT -> "登录已失效，请重新登录"
    ChatPhase.IDLE -> if (state.runId == null) "待开始" else "待开始（上一运行已结束）"
    ChatPhase.BUSY -> "正在处理，请勿重复提交或确认"
    ChatPhase.KNOWN -> state.receipt?.let {
        agentExecutionNotice(it) ?: "运行状态：${runStateLabel(it.state)}"
    } ?: "状态未知"
    ChatPhase.NEEDS_CHECK -> "待核对：原运行结果未知，请先读取或人工处理"
}

fun runStateLabel(state: RunState): String = when (state) {
    RunState.CREATED -> "已创建"
    RunState.RUNNING -> "运行中"
    RunState.WAITING_FOR_HUMAN -> "等待人工确认"
    RunState.WAITING_FOR_EXTERNAL -> "等待外部处理"
    RunState.COMPLETED -> "已完成"
    RunState.FAILED -> "已失败"
    RunState.CANCELLED -> "已取消"
}

private fun stepStateLabel(state: String): String = when (state) {
    "pending" -> "等待执行"
    "running" -> "执行中"
    "succeeded" -> "已完成"
    "failed" -> "失败"
    "unknown" -> "结果未知"
    "cancelled" -> "已取消"
    else -> state
}

private fun stepKindLabel(kind: String): String = when (kind) {
    "model" -> "模型"
    "tool" -> "工具"
    "checkpoint" -> "检查点"
    else -> kind
}

/** A visible invalidation notice; an answer must never be silently reconstructed. */
fun agentAnswerNotice(receipt: AgentRunReceipt): String? = when (receipt.answerStatus) {
    AnswerStatus.UNAVAILABLE -> buildString {
        append("回答或来源已失效、被撤回，或当前权限不足；正文与引用已隐藏，不能据此确认业务事实。")
        receipt.answerUnavailableReason?.let { append("（原因：").append(it).append("）") }
    }
    AnswerStatus.NOT_RECORDED ->
        if (receipt.state == RunState.COMPLETED) {
            "此运行没有可恢复的回答收据，不能补造旧回答。"
        } else {
            null
        }
    AnswerStatus.AVAILABLE -> null
}

fun agentCitationLines(citation: AnswerCitation): List<String> = listOf(
    "来源：${citation.label}",
    "类型：${citation.kind}",
    "版本：${citation.version}",
    "版本标识：${citation.versionRef}",
    "引用：${citation.sourceRef ?: citation.objectId}",
    "来源时间：${citation.observedAt ?: "未记录"}",
)

fun agentStepText(step: AgentStep): String = buildString {
    append("#").append(step.sequenceNo).append(" ").append(stepKindLabel(step.kind))
    append(" · ").append(stepStateLabel(step.state))
    step.toolKey?.let { append(" · 来源：").append(it).append("@").append(step.toolVersion ?: "?") }
    step.errorCode?.let { append(" · 错误：").append(it) }
}

fun agentFailureText(failure: ApiResult.Failure): String = buildString {
    append("请求未确认：").append(failure.code)
    if (failure.status > 0) append("（HTTP ").append(failure.status).append("）")
    append(" · ").append(failure.userMessage)
    failure.hint?.takeIf { it.isNotBlank() }?.let { append(" · 提示：").append(it) }
}

private enum class AgentDecision(val approve: Boolean) {
    APPROVE(true),
    REJECT(false),
}

@Composable
fun AgentChatScreen(
    sessions: SessionRepository,
    gateway: AgentRunGateway,
    onLogout: () -> Unit,
    onOpenDraft: () -> Unit,
    nowMillis: () -> Long = System::currentTimeMillis,
) {
    val context = LocalContext.current
    val mainHandler = remember { Handler(Looper.getMainLooper()) }
    val networkState = rememberNetworkState(context, mainHandler)
    val online = networkState.value
    val executor = remember { Executors.newSingleThreadExecutor() }

    var state by remember { mutableStateOf(AgentChatState(ChatPhase.IDLE)) }
    var question by remember { mutableStateOf("") }
    var submittedQuestion by remember { mutableStateOf<String?>(null) }
    var getRequestRunId by remember { mutableStateOf<String?>(null) }
    var get404RunId by remember { mutableStateOf<String?>(null) }
    var cancelRequestedRunId by rememberSaveable { mutableStateOf<String?>(null) }
    var decision by remember { mutableStateOf<AgentDecision?>(null) }
    var decisionReason by remember { mutableStateOf("") }
    var decisionDigest by remember { mutableStateOf<String?>(null) }
    var abandonDialog by remember { mutableStateOf(false) }
    var clock by remember { mutableStateOf(nowMillis()) }

    val controller = remember(sessions, gateway) {
        AgentChatViewModel(
            sessions = sessions,
            gateway = gateway,
            locators = PreferencesAgentRunLocatorStore(context.applicationContext),
            executor = executor,
            deliver = { block -> mainHandler.post(block) },
            online = { networkState.value },
            newRunId = { UUID.randomUUID().toString() },
            changed = { updated -> state = updated },
        )
    }

    DisposableEffect(controller) {
        onDispose {
            controller.close()
            executor.shutdown()
        }
    }

    DisposableEffect(sessions) {
        val unsubscribe = sessions.onScopeInvalidated {
            // Cancel intent is page-local per identity; never carry it across account switches.
            rememberedCancelRequests.clear()
            mainHandler.post { cancelRequestedRunId = null }
        }
        onDispose { unsubscribe() }
    }

    LaunchedEffect(controller) {
        state = controller.state
        // Restore is GET-only: entering or rotating never re-sends a question, confirmation or cancel.
        controller.restore()
        // restore() starts its GET synchronously, so the requested run id is visible only after it.
        val afterRestore = controller.state
        getRequestRunId = if (afterRestore.phase == ChatPhase.BUSY) afterRestore.runId else null
    }

    LaunchedEffect(state.runId) {
        val runId = state.runId
        if (runId != null && rememberedCancelRequests.contains(runId)) {
            cancelRequestedRunId = runId
        }
        if (runId == null) cancelRequestedRunId = null
    }

    LaunchedEffect(state.runId, state.receipt?.state) {
        val runId = state.runId
        if (runId != null && state.receipt?.state?.terminal == true) {
            rememberedCancelRequests.remove(runId)
            if (cancelRequestedRunId == runId) cancelRequestedRunId = null
        }
    }

    // Only a completed GET may establish the 404 that unlocks manual locator abandonment.
    LaunchedEffect(state.phase, state.runId, state.failure?.status, getRequestRunId) {
        val requested = getRequestRunId ?: return@LaunchedEffect
        if (state.phase == ChatPhase.BUSY) return@LaunchedEffect
        if (state.runId != requested) {
            getRequestRunId = null
            return@LaunchedEffect
        }
        get404RunId = if (state.phase == ChatPhase.NEEDS_CHECK && state.failure?.status == 404) {
            requested
        } else {
            null
        }
        getRequestRunId = null
    }

    LaunchedEffect(state.runId, state.receipt?.pendingAction?.inputDigest) {
        clock = nowMillis()
        while (state.receipt?.pendingAction != null) {
            delay(15_000)
            clock = nowMillis()
        }
    }

    LaunchedEffect(state.receipt?.pendingAction?.inputDigest, state.phase) {
        val currentDigest = state.receipt?.pendingAction?.inputDigest
        if (decision != null && (currentDigest == null || currentDigest != decisionDigest)) {
            decision = null
            decisionReason = ""
            decisionDigest = null
        }
    }

    val cancelRequested = state.runId != null && cancelRequestedRunId == state.runId
    val review = reviewPendingAction(state, online, cancelRequested, clock)
    val actions = agentActions(state, online, cancelRequested, get404RunId, clock)
    val currentRunId = state.runId

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("Agent 助手", style = MaterialTheme.typography.headlineSmall)
            OutlinedButton(onClick = onOpenDraft) { Text("手工草稿") }
        }

        Text(
            "开发测试环境 · 未完成真实模型/真实账号验收 · 仅合成数据",
            color = Color(0xFF8A2600),
            fontWeight = FontWeight.SemiBold,
        )
        Text(
            "Agent 只能调用已登记工具；写入类动作会停下来等待人工确认。本页不会自动重试。",
            style = MaterialTheme.typography.bodySmall,
        )

        if (!online) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFFF3E0)),
                border = BorderStroke(1.dp, Color(0xFFB26A00)),
            ) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Text("当前离线", fontWeight = FontWeight.Bold)
                    Text("离线时不能发起提交、确认、取消或重新读取。页面可能仍显示之前收到的回执，不代表此刻已重新核验。手工草稿仍可用。")
                }
            }
        }

        OutlinedTextField(
            value = question,
            onValueChange = { if (it.length <= MAX_QUESTION_LENGTH) question = it },
            label = { Text("你要 Agent 做什么？（最多 $MAX_QUESTION_LENGTH 字）") },
            modifier = Modifier.fillMaxWidth(),
            minLines = 2,
            maxLines = 6,
            enabled = !actions.busy,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = {
                    val text = question
                    submittedQuestion = text
                    question = ""
                    get404RunId = null
                    getRequestRunId = null
                    cancelRequestedRunId = null
                    controller.submit(text)
                },
                enabled = actions.canSubmit && question.isNotBlank(),
                modifier = Modifier.weight(1f),
            ) {
                Text(if (actions.busy) "处理中…" else "提交问题")
            }
            OutlinedButton(onClick = onOpenDraft) { Text("返回手工草稿") }
        }

        submittedQuestion?.let {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(10.dp)) {
                    Text("本次问题（设备不保存此预览副本；服务端受理后会记录问题）", fontWeight = FontWeight.SemiBold)
                    Text(it)
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text("当前状态", fontWeight = FontWeight.Bold)
                Text(agentPhaseLabel(state))
                if (actions.busy) {
                    Text(
                        "当前操作进行中，暂不能提交、确认、拒绝或取消；请等待服务端收据。",
                        color = Color(0xFF8A2600),
                    )
                }
                currentRunId?.let { Text("运行编号：$it") }
                receiptForDisplay(state)?.taskId?.let { Text("任务建议编号：$it") }
                state.failure?.let {
                    ErrorPanel(
                        userMessage = it.userMessage.ifBlank { "请求未确认：${it.code}" },
                        hint = it.hint,
                        code = it.code,
                    )
                }
            }
        }

        receiptForDisplay(state)?.let { receipt ->
            ReceiptCard(
                receipt = receipt,
                review = review,
                actions = actions,
                cancelRequested = cancelRequested,
                onRefresh = {
                    // Re-check at click time: stale composition may still offer a button after another action started.
                    if (controller.state.phase != ChatPhase.BUSY && currentRunId != null) {
                        getRequestRunId = currentRunId
                        controller.refresh()
                        if (controller.state.phase != ChatPhase.BUSY) getRequestRunId = null
                    }
                },
                onCancel = {
                    // Only a controller-accepted cancel from a quiescent known run may be recorded.
                    if (controller.state.phase == ChatPhase.KNOWN) {
                        controller.cancel()
                        if (controller.state.phase == ChatPhase.BUSY) {
                            cancelRequestedRunId = currentRunId
                            currentRunId?.let { rememberedCancelRequests.add(it) }
                        }
                    }
                },
                onApprove = {
                    if (review?.approveAllowed == true) {
                        decision = AgentDecision.APPROVE
                        decisionDigest = review.inputDigest
                        decisionReason = ""
                    }
                },
                onReject = {
                    if (review?.rejectAllowed == true) {
                        decision = AgentDecision.REJECT
                        decisionDigest = review.inputDigest
                        decisionReason = ""
                    }
                },
            )
        }

        if (state.phase == ChatPhase.NEEDS_CHECK) {
            RecoveryCard(
                actions = actions,
                readAttempted = get404RunId != null && get404RunId == currentRunId,
                failureStatus = state.failure?.status,
                onRefresh = {
                    if (controller.state.phase == ChatPhase.NEEDS_CHECK && currentRunId != null) {
                        getRequestRunId = currentRunId
                        controller.refresh()
                        if (controller.state.phase != ChatPhase.BUSY) getRequestRunId = null
                    }
                },
                onAbandon = { abandonDialog = true },
            )
        }

        Text(
            "服务端运行、审计与幂等记录由后端保留；本页只负责受控发起、读取与人工确认。",
            style = MaterialTheme.typography.bodySmall,
            color = Color(0xFF57636C),
        )
    }

    val dialogDecision = decision
    if (dialogDecision != null) {
        // Never render a stale pending snapshot while the controller is busy or the result is unknown.
        val snapshot = if (state.phase == ChatPhase.KNOWN) review else null
        AlertDialog(
            onDismissRequest = {
                decision = null
                decisionReason = ""
                decisionDigest = null
            },
            title = {
                Text(if (dialogDecision.approve) "二次确认：执行待确认动作" else "二次确认：拒绝待确认动作")
            },
            text = {
                Column(
                    modifier = Modifier.verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    if (snapshot == null || snapshot.inputDigest != decisionDigest) {
                        Text(
                            "页面状态已变化：本次确认快照已失效，请关闭后重新核对。",
                            color = Color(0xFFB3261E),
                        )
                    } else {
                        Text("以下为本次确认的固定快照，提交时不会重新拼装参数。")
                        Text("工具：${snapshot.toolKey} @ ${snapshot.toolVersion}")
                        Text("运行编号：${currentRunId ?: "未知"}")
                        snapshot.reviewerId?.let { Text("负责人（reviewer_id）：$it") }
                        Text("完整参数：", fontWeight = FontWeight.SemiBold)
                        snapshot.argumentRows.forEach { row ->
                            Text("${row.key}：${row.value}")
                        }
                        Text("影响：${snapshot.impact}")
                        Text("有效期至：${snapshot.expiresAt}")
                        Text("摘要：${snapshot.inputDigest}")
                        Text("版本：run_version=${snapshot.runVersion}，step_version=${snapshot.stepVersion}")
                    }
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = decisionReason,
                        onValueChange = { if (it.length <= MAX_REASON_LENGTH) decisionReason = it },
                        label = { Text("理由（必填，1-$MAX_REASON_LENGTH 字）") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 2,
                    )
                    if (decisionReason.isBlank()) {
                        Text("请填写本次决定理由。", color = Color(0xFFB3261E))
                    }
                }
            },
            confirmButton = {
                Button(
                    enabled = snapshot != null &&
                        snapshot.inputDigest == decisionDigest &&
                        decisionEnabled(snapshot, dialogDecision.approve, decisionReason),
                    onClick = {
                        val fresh = reviewPendingAction(
                            controller.state,
                            networkState.value,
                            cancelRequested,
                            nowMillis(),
                        )
                        val digestMatches = fresh != null && fresh.inputDigest == decisionDigest
                        val stillAllowed = fresh != null &&
                            if (dialogDecision.approve) fresh.approveAllowed else fresh.rejectAllowed
                        if (!digestMatches || !stillAllowed) {
                            // The 15s clock may lag the real expiry; never submit a stale snapshot.
                            clock = nowMillis()
                            decision = null
                            decisionReason = ""
                            decisionDigest = null
                            return@Button
                        }
                        val reason = decisionReason
                        decision = null
                        decisionReason = ""
                        decisionDigest = null
                        controller.confirm(dialogDecision.approve, reason)
                    },
                ) {
                    Text(if (dialogDecision.approve) "确认执行" else "确认拒绝")
                }
            },
            dismissButton = {
                TextButton(
                    onClick = {
                        decision = null
                        decisionReason = ""
                        decisionDigest = null
                    },
                ) { Text("取消") }
            },
        )
    }

    if (abandonDialog) {
        AlertDialog(
            onDismissRequest = { abandonDialog = false },
            title = { Text("人工放弃本地定位") },
            text = {
                Column(
                    modifier = Modifier.verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(5.dp),
                ) {
                    Text("仅在重新读取原运行得到 404 之后可用。")
                    Text("此操作只清除本机保存的运行定位，不会取消、删除或重放服务端运行；服务端运行、审计与幂等记录仍保留。")
                    Text("请确认已人工核对：该编号不存在、不属于当前门店/账号，或已无权访问。")
                }
            },
            confirmButton = {
                Button(
                    enabled = actions.canDiscardLocator,
                    onClick = {
                        val live = agentActions(
                            controller.state,
                            networkState.value,
                            cancelRequestedRunId == currentRunId,
                            get404RunId,
                            nowMillis(),
                        )
                        val liveRunId = controller.state.runId
                        if (live.canDiscardLocator && liveRunId != null && get404RunId == liveRunId) {
                            abandonDialog = false
                            controller.discardAfterReview()
                        } else {
                            // Gate changed after the dialog opened: never call discard on stale state.
                            abandonDialog = false
                            clock = nowMillis()
                        }
                    },
                ) { Text("确认放弃本地定位") }
            },
            dismissButton = {
                TextButton(onClick = { abandonDialog = false }) { Text("取消") }
            },
        )
    }

    if (state.phase == ChatPhase.SIGNED_OUT) {
        LaunchedEffect(state.phase) { onLogout() }
    }
}

@Composable
private fun ReceiptCard(
    receipt: AgentRunReceipt,
    review: PendingReview?,
    actions: AgentActions,
    cancelRequested: Boolean,
    onRefresh: () -> Unit,
    onCancel: () -> Unit,
    onApprove: () -> Unit,
    onReject: () -> Unit,
) {
    val notice = agentAnswerNotice(receipt)
    Card(
        modifier = Modifier.fillMaxWidth(),
        border = BorderStroke(1.dp, Color(0xFFB8C4CE)),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            Text("本轮结果", fontWeight = FontWeight.Bold)
            Text("运行状态：${runStateLabel(receipt.state)}")
            agentExecutionNotice(receipt)?.let { Text(it, color = Color(0xFF345A72)) }
            receipt.errorCode?.let {
                ErrorPanel(
                    userMessage = "运行错误：$it",
                    code = it,
                )
            }
            pendingUnavailableNotice(receipt)?.let { Text(it, color = Color(0xFF8A2600)) }

            if (notice != null) {
                Text(notice, color = Color(0xFF8A2600))
            } else if (receipt.answer != null) {
                Text("回答（需人工核实）", fontWeight = FontWeight.SemiBold)
                Text(receipt.answer)
                if (receipt.citations.isEmpty()) {
                    Text("暂无结构化引用，不能据此确认业务事实。")
                }
                receipt.citations.forEach { citation ->
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = Color(0xFFF7F9FB)),
                    ) {
                        Column(
                            modifier = Modifier.padding(8.dp),
                            verticalArrangement = Arrangement.spacedBy(2.dp),
                        ) {
                            agentCitationLines(citation).forEach { line -> Text(line) }
                        }
                    }
                }
            }

            Text("步骤", fontWeight = FontWeight.SemiBold)
            if (receipt.steps.isEmpty()) {
                Text("尚未产生步骤。")
            } else {
                receipt.steps.forEach { step -> Text(agentStepText(step)) }
            }

            if (receipt.pendingAction != null && review != null) {
                PendingActionCard(
                    review = review,
                    actions = actions,
                    onApprove = onApprove,
                    onReject = onReject,
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onRefresh, enabled = actions.canRefresh) {
                    Text("重新读取（仅GET）")
                }
                if (!receipt.state.terminal) {
                    OutlinedButton(onClick = onCancel, enabled = actions.canCancel) {
                        Text(if (cancelRequested) "取消已请求" else "请求取消运行")
                    }
                }
            }

            if (cancelRequested && !receipt.state.terminal) {
                Text(
                    "已请求取消：服务端收据返回终态前禁止再次确认。当前显示服务端收据，不以本机状态冒充终态。",
                    color = Color(0xFF8A2600),
                )
            }
        }
    }
}

@Composable
private fun PendingActionCard(
    review: PendingReview,
    actions: AgentActions,
    onApprove: () -> Unit,
    onReject: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = Color(0xFFFFF8E1)),
        border = BorderStroke(2.dp, Color(0xFFB26A00)),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text("等待人工确认", fontWeight = FontWeight.Bold)
            Text("工具：${review.toolKey} @ ${review.toolVersion}")
            if (!review.supportedTool) {
                Text(
                    "该工具未在本版本登记：只能拒绝或重新读取，不能批准。",
                    color = Color(0xFFB3261E),
                )
            }
            Text("完整参数", fontWeight = FontWeight.SemiBold)
            review.argumentRows.forEach { row ->
                val suffix = buildString {
                    if (row.required) append("（必填）")
                    if (!row.registered) append("  [未登记字段]")
                }
                Text("${row.key}$suffix：${row.value}")
            }
            review.reviewerId?.let { Text("负责人（reviewer_id）：$it") }
            Text("影响：${review.impact}")
            Text("有效期至：${review.expiresAt}${if (review.expired) "（已过期）" else ""}")
            Text("摘要（input_digest）：${review.inputDigest}")
            Text("版本：run_version=${review.runVersion} · step_version=${review.stepVersion}")
            review.approvalBlockedReason?.let {
                Text("不能批准：$it", color = Color(0xFFB3261E))
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onApprove, enabled = actions.canApprove) {
                    Text("确认执行（二次确认）")
                }
                OutlinedButton(onClick = onReject, enabled = actions.canReject) {
                    Text("拒绝…")
                }
            }
        }
    }
}

@Composable
private fun RecoveryCard(
    actions: AgentActions,
    readAttempted: Boolean,
    failureStatus: Int?,
    onRefresh: () -> Unit,
    onAbandon: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = Color(0xFFF2F4F7)),
        border = BorderStroke(1.dp, Color(0xFF8A94A0)),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(5.dp),
        ) {
            Text("待核对（结果未知）", fontWeight = FontWeight.Bold)
            Text("不会自动重发问题、确认或取消；请先重新读取原运行。")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onRefresh, enabled = actions.canRefresh) {
                    Text("重新读取原运行（仅GET）")
                }
                if (actions.canDiscardLocator) {
                    OutlinedButton(onClick = onAbandon) {
                        Text("人工放弃本地定位…")
                    }
                }
            }
            if (readAttempted && failureStatus == 404) {
                Text("重新读取得到 404。只有人工核对后，才可放弃本地定位；不会删除服务端运行。")
            }
            if (!actions.canDiscardLocator && actions.offline) {
                Text("当前离线：不能重新读取或放弃定位。")
            }
        }
    }
}

@Composable
private fun rememberNetworkState(
    context: Context,
    mainHandler: Handler,
): MutableState<Boolean> {
    val state = remember { mutableStateOf(hasUsableNetwork(context)) }
    DisposableEffect(context) {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        if (manager == null) {
            onDispose { }
        } else {
            val callback = object : ConnectivityManager.NetworkCallback() {
                private fun refresh() {
                    mainHandler.post { state.value = hasUsableNetwork(context) }
                }

                override fun onAvailable(network: android.net.Network) = refresh()
                override fun onLost(network: android.net.Network) = refresh()
                override fun onCapabilitiesChanged(
                    network: android.net.Network,
                    networkCapabilities: NetworkCapabilities,
                ) = refresh()
            }
            runCatching { manager.registerDefaultNetworkCallback(callback) }
            onDispose { runCatching { manager.unregisterNetworkCallback(callback) } }
        }
    }
    return state
}

/**
 * Any usable local transport counts as online: the backend may be reached over a LAN, so a
 * validated internet probe is deliberately not required.
 */
private fun hasUsableNetwork(context: Context): Boolean = runCatching {
    val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
    if (manager == null) {
        false
    } else {
        val network = manager.activeNetwork
        if (network == null) {
            false
        } else {
            val capabilities = manager.getNetworkCapabilities(network)
            capabilities != null && (
                capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) ||
                    capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                    capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) ||
                    capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) ||
                    capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                )
        }
    }
}.getOrDefault(false)
