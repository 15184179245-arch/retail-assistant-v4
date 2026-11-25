package com.retail.smartops.features.shell

import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.navigation.AppDestination
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.ConnectionStatus
import com.retail.smartops.core.network.L2ConfirmationResult
import com.retail.smartops.core.network.L2ConfirmationStatus
import com.retail.smartops.core.network.ThreadWorkspace
import com.retail.smartops.core.network.WorkObjectRef
import com.retail.smartops.core.network.WorkspaceApiClient
import com.retail.smartops.core.network.WorkspaceArtifact
import com.retail.smartops.core.network.WorkspaceEventStreamController
import com.retail.smartops.core.network.WorkspaceSseEvent
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.features.blocks.ArtifactCoeditEngine
import com.retail.smartops.features.blocks.ArtifactCoeditUiState
import com.retail.smartops.features.blocks.CommandConfirmModel
import com.retail.smartops.features.blocks.ParsedUiBlock
import java.io.Closeable
import java.util.UUID

enum class ChipKind {
    BARCODE_SCAN,
    WORK_OBJECT,
    SELECTION_COUNT,
}

data class ContextChip(
    val id: String,
    val kind: ChipKind,
    val label: String,
    val value: String,
    val objectKind: String? = null,
    val objectId: String? = null,
)

data class FieldThreadMessage(
    val id: String,
    val role: String, // "human" | "agent" | "system"
    val text: String,
    val contextChips: List<ContextChip> = emptyList(),
    val blocks: List<ParsedUiBlock> = emptyList(),
    val timestamp: String? = null,
)

data class FieldWorkObjectSummary(
    val id: String,
    val kind: String,
    val title: String,
    val statusLabel: String,
    val statusKind: StatusKind,
    val sourceRoute: String? = null,
    val artifactId: String? = null,
    val version: Int = 1,
    val isComplexBatchReview: Boolean = false,
    val batchItemCount: Int = 0,
    val desktopHint: String? = null,
)

data class FieldShellState(
    val session: Session? = null,
    val environmentKind: StatusKind = StatusKind.DEVELOPMENT,
    val connectionStatus: ConnectionStatus = ConnectionStatus.ONLINE,
    val unsyncedLocalDraftCount: Int = 0,
    val activeZone: AppDestination = AppDestination.Today,
    val activeThreadId: String? = null,
    val openObjects: List<FieldWorkObjectSummary> = emptyList(),
    val activeObject: FieldWorkObjectSummary? = null,
    val activeArtifactCoedit: ArtifactCoeditUiState? = null,
    val contextChips: List<ContextChip> = emptyList(),
    val threadMessages: List<FieldThreadMessage> = emptyList(),
    val chatDrawerExpanded: Boolean = false,
    val scanSheetOpen: Boolean = false,
    val voiceFallbackMode: Boolean = false,
    val voiceStatusText: String? = null,
    val crossDeviceToast: String? = null,
    val sseConnected: Boolean = false,
    val sseCursor: Long = 0L,
    val l2ButtonLabel: String = "确认执行",
    val l2OfflineBlocked: Boolean = false,
    val errorBanner: String? = null,
    val scopeInvalidationCount: Int = 0,
) {
    val isNetworkReachable: Boolean
        get() = connectionStatus == ConnectionStatus.ONLINE || connectionStatus == ConnectionStatus.MODEL_OFFLINE

    val isModelAvailable: Boolean
        get() = connectionStatus == ConnectionStatus.ONLINE

    val isManualOperationsPermitted: Boolean
        get() = true // Scan, cached search, and local draft remain 100% usable even offline.
}

/**
 * Coordinates C2C-140 §2.5 Android Field Shell state:
 * - Persistent status bar metadata (env badge, employee identity, network/model status, unsynced drafts)
 * - 5-zone navigation (`Today`, `Objects`, `ScanSpeak`, `Threads`, `Profile`)
 * - Cross-device Web <-> Android ThreadWorkspace & Artifact resumption
 * - Barcode / Work Object context chip injection into active thread
 * - Offline degradation (`待网络` on AI & L2 while keeping scan & local drafts usable)
 * - Scope invalidation cleanup on user switch / logout (backstack, thread, chips, SSE stream)
 */
class FieldShellController(
    private val sessions: SessionRepository,
    private val workspaceApi: WorkspaceApiClient? = null,
    private val onStateChanged: (FieldShellState) -> Unit = {},
) : Closeable {

    private var current = buildInitialState(sessions.currentSession())
    private var eventStream: WorkspaceEventStreamController? = null
    private var closed = false

    private val removeScopeListener = sessions.onScopeInvalidated {
        invalidateScope()
    }

    val state: FieldShellState
        get() {
            val latest = sessions.currentSession()
            if (latest != current.session) {
                invalidateScope()
            }
            return current
        }

    private fun buildInitialState(session: Session?): FieldShellState = FieldShellState(
        session = session,
        l2OfflineBlocked = false,
        l2ButtonLabel = "确认执行",
    )

    private fun publish(next: FieldShellState) {
        current = next
        if (!closed) {
            onStateChanged(next)
        }
    }

    fun selectZone(zone: AppDestination) {
        if (zone == AppDestination.ScanSpeak) {
            publish(current.copy(scanSheetOpen = true))
            return
        }
        publish(current.copy(activeZone = zone))
    }

    fun setChatDrawerExpanded(expanded: Boolean) {
        publish(current.copy(chatDrawerExpanded = expanded))
    }

    fun setScanSheetOpen(open: Boolean) {
        publish(current.copy(scanSheetOpen = open))
    }

    fun updateUnsyncedLocalDraftCount(count: Int) {
        publish(current.copy(unsyncedLocalDraftCount = count.coerceAtLeast(0)))
    }

    fun updateConnectionStatus(status: ConnectionStatus) {
        val offlineOrUnreachable = status == ConnectionStatus.OFFLINE || status == ConnectionStatus.BACKEND_UNREACHABLE
        publish(
            current.copy(
                connectionStatus = status,
                l2OfflineBlocked = offlineOrUnreachable,
                l2ButtonLabel = if (offlineOrUnreachable) "待网络" else "确认执行",
            ),
        )
    }

    /**
     * Triggered when user long-presses central `◉ 扫码/按住说话`.
     * When offline or SpeechRecognizer unavailable, degrades gracefully to text/barcode input.
     */
    fun onVoiceInputRequested(speechRecognizerAvailable: Boolean): Boolean {
        if (!current.isNetworkReachable || !speechRecognizerAvailable) {
            publish(
                current.copy(
                    voiceFallbackMode = true,
                    chatDrawerExpanded = true,
                    voiceStatusText = if (!current.isNetworkReachable) {
                        "当前离线：系统语音不可用，已降级为条码/文字输入"
                    } else {
                        "设备未配置系统语音识别，已降级为条码/文字输入"
                    },
                ),
            )
            return false
        }
        publish(
            current.copy(
                voiceFallbackMode = false,
                voiceStatusText = "正在使用系统语音输入…",
            ),
        )
        return true
    }

    fun onVoiceTranscriptReady(transcript: String) {
        val cleaned = transcript.trim()
        if (cleaned.isEmpty()) return
        publish(current.copy(voiceStatusText = null, chatDrawerExpanded = true))
        submitThreadPrompt(cleaned)
    }

    /**
     * Injects a scanned barcode as a context chip into the current thread (works 100% offline).
     */
    fun injectBarcodeChip(rawBarcode: String) {
        val barcode = rawBarcode.trim()
        if (barcode.isEmpty()) return
        val chip = ContextChip(
            id = "scan:$barcode",
            kind = ChipKind.BARCODE_SCAN,
            label = "扫码: $barcode",
            value = barcode,
        )
        val updatedChips = (current.contextChips.filterNot { it.id == chip.id } + chip)
        val threadMsg = FieldThreadMessage(
            id = "msg-${UUID.randomUUID()}",
            role = "system",
            text = "已扫码加入上下文芯片：$barcode（本地找货/草稿可用）",
            contextChips = listOf(chip),
        )
        publish(
            current.copy(
                contextChips = updatedChips,
                threadMessages = current.threadMessages + threadMsg,
                scanSheetOpen = false,
            ),
        )
    }

    fun removeContextChip(chipId: String) {
        publish(current.copy(contextChips = current.contextChips.filterNot { it.id == chipId }))
    }

    fun focusWorkObject(summary: FieldWorkObjectSummary, remoteArtifact: WorkspaceArtifact? = null) {
        val normalized = normalizeBatchSummary(summary)
        val objectChip = ContextChip(
            id = "obj:${normalized.kind}:${normalized.id}",
            kind = ChipKind.WORK_OBJECT,
            label = "当前对象: ${normalized.title}",
            value = normalized.id,
            objectKind = normalized.kind,
            objectId = normalized.id,
        )
        val nextObjects = (current.openObjects.filterNot { it.id == normalized.id } + normalized)
        val nextChips = (current.contextChips.filterNot { it.kind == ChipKind.WORK_OBJECT } + objectChip)
        val nextCoedit = remoteArtifact?.let { ArtifactCoeditEngine.fromArtifact(it) }
            ?: current.activeArtifactCoedit?.takeIf { it.artifact.id == normalized.artifactId }

        publish(
            current.copy(
                openObjects = nextObjects,
                activeObject = normalized,
                activeArtifactCoedit = nextCoedit,
                contextChips = nextChips,
                activeZone = AppDestination.Objects,
            ),
        )
    }

    /**
     * Resumes a Web-created ThreadWorkspace & Artifact directly onto Android (C2C-140 §2.6).
     */
    fun resumeFromThreadWorkspace(
        workspace: ThreadWorkspace,
        activeArtifact: WorkspaceArtifact? = null,
    ) {
        val session = sessions.currentSession()
        if (session != null && (workspace.organizationId.isNotBlank() && workspace.organizationId != session.organizationId ||
                workspace.storeId.isNotBlank() && workspace.storeId != session.storeId)
        ) {
            publish(current.copy(errorBanner = "跨组织或跨门店线程已隔离，拒绝跨范围恢复。"))
            return
        }

        val summaries = workspace.openObjects.map { ref -> toSummary(ref) }
        val activeSummary = workspace.activeObjectRef?.let { toSummary(it) }
            ?: activeArtifact?.let { artifact ->
                normalizeBatchSummary(
                    FieldWorkObjectSummary(
                        id = artifact.id,
                        kind = artifact.kind,
                        title = artifact.title,
                        statusLabel = if (artifact.status == "submitted") "已提交" else "草稿 v${artifact.version}",
                        statusKind = if (artifact.status == "submitted") StatusKind.SETTLED else StatusKind.FACT,
                        artifactId = artifact.id,
                        version = artifact.version,
                    ),
                )
            }
            ?: summaries.firstOrNull()

        val chip = activeSummary?.let {
            ContextChip(
                id = "obj:${it.kind}:${it.id}",
                kind = ChipKind.WORK_OBJECT,
                label = "接续对象: ${it.title}",
                value = it.id,
                objectKind = it.kind,
                objectId = it.id,
            )
        }
        val nextChips = buildList {
            addAll(current.contextChips.filterNot { it.kind == ChipKind.WORK_OBJECT })
            if (chip != null) add(chip)
        }
        val coedit = activeArtifact?.let { ArtifactCoeditEngine.fromArtifact(it) }

        publish(
            current.copy(
                activeThreadId = workspace.threadId,
                openObjects = summaries.ifEmpty { listOfNotNull(activeSummary) },
                activeObject = activeSummary,
                activeArtifactCoedit = coedit,
                contextChips = nextChips,
                crossDeviceToast = workspace.activeEditorHint ?: activeArtifact?.activeEditorNotice,
                activeZone = if (activeSummary != null) AppDestination.Objects else current.activeZone,
                errorBanner = null,
            ),
        )
    }

    /**
     * Fetches ThreadWorkspace (and its active Artifact) from backend for cross-device Web -> Android resume.
     */
    fun loadThreadWorkspaceFromBackend(threadId: String): ApiResult<ThreadWorkspace> {
        val session = sessions.currentSession()
            ?: return ApiResult.Failure.Unauthorized(userMessage = "请先登录后再接续跨端线程。")
        val client = workspaceApi
            ?: return ApiResult.Failure.NetworkError(userMessage = "未配置工作区客户端。")
        if (!current.isNetworkReachable) {
            return ApiResult.Failure.NetworkError(userMessage = "待网络：当前离线，无法拉取远端线程。")
        }

        val wsResult = client.getThreadWorkspace(session, threadId)
        if (wsResult is ApiResult.Success) {
            val ws = wsResult.data
            val artifactId = ws.activeObjectRef?.artifactId
            val artifact = if (!artifactId.isNullOrBlank()) {
                when (val artRes = client.getArtifact(session, artifactId)) {
                    is ApiResult.Success -> artRes.data
                    else -> null
                }
            } else null
            resumeFromThreadWorkspace(ws, artifact)
            attachSseStream(session, threadId)
        }
        return wsResult
    }

    fun attachSseStream(session: Session, threadId: String) {
        eventStream?.close()
        val client = workspaceApi ?: return
        val controller = client.createEventStreamController(
            threadId = threadId,
            initialCursor = current.sseCursor,
            onEvent = ::onSseEvent,
            onConnectionChanged = { connected ->
                publish(current.copy(sseConnected = connected))
            },
        )
        eventStream = controller
    }

    fun pumpActiveSseStreamOnce(expected: Session, maxReconnects: Int = 2): Boolean =
        eventStream?.pumpOnce(expected, maxReconnects) ?: false

    fun onSseEvent(event: WorkspaceSseEvent) {
        val session = sessions.currentSession() ?: return
        if (event.threadId != current.activeThreadId && current.activeThreadId != null) return
        val isOtherEndpoint = (event.deviceType != null && event.deviceType != "android") ||
            (event.actorSubjectId != null && event.actorSubjectId != session.subjectId)
        val toast = if (isOtherEndpoint && event.eventType in setOf("artifact.updated", "workspace.editing", "workspace.updated")) {
            "此对象正在另一端编辑"
        } else {
            current.crossDeviceToast
        }
        publish(
            current.copy(
                sseCursor = maxOf(current.sseCursor, event.cursor),
                crossDeviceToast = toast,
            ),
        )
    }

    fun submitThreadPrompt(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        val userMsg = FieldThreadMessage(
            id = "msg-${UUID.randomUUID()}",
            role = "human",
            text = trimmed,
            contextChips = current.contextChips,
        )
        val replyMsg = when {
            !current.isNetworkReachable -> FieldThreadMessage(
                id = "msg-${UUID.randomUUID()}",
                role = "system",
                text = "待网络：当前离线，AI 对话与 L2 提交暂不可用；扫码、本地查价与草稿可继续使用。",
            )
            !current.isModelAvailable -> FieldThreadMessage(
                id = "msg-${UUID.randomUUID()}",
                role = "system",
                text = "模型服务离线：请改用本地查货/手工编辑草稿，工作区确定性数据仍可用。",
            )
            else -> null
        }
        val added = listOfNotNull(userMsg, replyMsg)
        publish(current.copy(threadMessages = current.threadMessages + added))
    }

    fun appendAgentBlocks(summaryText: String, blocks: List<ParsedUiBlock>) {
        val agentMsg = FieldThreadMessage(
            id = "msg-${UUID.randomUUID()}",
            role = "agent",
            text = summaryText,
            blocks = blocks,
        )
        publish(current.copy(threadMessages = current.threadMessages + agentMsg))
    }

    fun openCommandConfirm(actionBlock: ParsedUiBlock.Valid) {
        val artifactId = actionBlock.artifactId
            ?: (actionBlock.props["artifact_id"] as? String)
            ?: current.activeArtifactCoedit?.artifact?.id
            ?: current.activeObject?.artifactId
            ?: ""
        val baseVersion = current.activeArtifactCoedit?.artifact?.version
            ?: (actionBlock.props["base_version"] as? Number)?.toInt()
            ?: current.activeObject?.version
            ?: 1
        val model = CommandConfirmModel(
            actionKey = (actionBlock.props["action_key"] as? String) ?: "artifact.submit",
            title = (actionBlock.props["title"] as? String) ?: "确认执行正式操作",
            whatHappens = (actionBlock.props["what_happens"] as? String) ?: "提交草稿并生成正式单据交接请求",
            impactScope = (actionBlock.props["impact_scope"] as? String) ?: "仅影响当前选定对象",
            willNotDo = (actionBlock.props["will_not_do"] as? String) ?: "不会自动改写管家婆正式库存或售价",
            approver = (actionBlock.props["approver"] as? String) ?: (current.session?.username ?: "门店审批人"),
            artifactId = artifactId,
            baseVersion = baseVersion,
            idempotencyKey = (actionBlock.props["idempotency_key"] as? String)?.takeIf { it.isNotBlank() }
                ?: UUID.randomUUID().toString(),
        )
        val baseCoedit = current.activeArtifactCoedit ?: WorkspaceArtifact(
            id = artifactId.ifBlank { "art-local" },
            organizationId = current.session?.organizationId.orEmpty(),
            storeId = current.session?.storeId.orEmpty(),
            ownerSubjectId = current.session?.subjectId.orEmpty(),
            threadId = current.activeThreadId,
            kind = current.activeObject?.kind ?: "quote_draft",
            title = current.activeObject?.title ?: "当前草稿",
            status = "draft",
            version = baseVersion,
        ).let { ArtifactCoeditEngine.fromArtifact(it) }

        publish(
            current.copy(
                activeArtifactCoedit = baseCoedit.copy(confirmDialog = model),
            ),
        )
    }

    fun dismissCommandConfirm() {
        val coedit = current.activeArtifactCoedit ?: return
        publish(current.copy(activeArtifactCoedit = coedit.copy(confirmDialog = null)))
    }

    fun submitActiveL2Confirmation(reason: String): L2ConfirmationResult {
        val coedit = current.activeArtifactCoedit
        val dialog = coedit?.confirmDialog
        val idempotencyKey = dialog?.idempotencyKey ?: UUID.randomUUID().toString()

        if (!current.isNetworkReachable) {
            val offlineResult = L2ConfirmationResult(
                status = L2ConfirmationStatus.OFFLINE,
                statusKind = StatusKind.UNVERIFIED,
                idempotencyKey = idempotencyKey,
                message = "待网络：当前离线，严禁执行 L2 正式确认",
                hint = "请恢复网络后再点击确认；本地草稿已保存。",
            )
            if (coedit != null) {
                publish(
                    current.copy(
                        activeArtifactCoedit = ArtifactCoeditEngine.recordL2Outcome(coedit, offlineResult),
                        l2OfflineBlocked = true,
                        l2ButtonLabel = "待网络",
                    ),
                )
            }
            return offlineResult
        }

        val session = sessions.currentSession()
            ?: return L2ConfirmationResult(
                status = L2ConfirmationStatus.FORBIDDEN,
                statusKind = StatusKind.ERROR,
                idempotencyKey = idempotencyKey,
                message = "会话未登录，禁止 L2 提交",
            )

        val client = workspaceApi
            ?: return L2ConfirmationResult(
                status = L2ConfirmationStatus.ERROR,
                statusKind = StatusKind.ERROR,
                idempotencyKey = idempotencyKey,
                message = "未连接工作区服务",
            )

        if (coedit != null && dialog != null) {
            publish(current.copy(activeArtifactCoedit = coedit.copy(confirmDialog = dialog.copy(inFlight = true))))
        }

        val outcome = client.submitL2Confirmation(
            expected = session,
            artifactId = dialog?.artifactId ?: coedit?.artifact?.id.orEmpty(),
            baseVersion = dialog?.baseVersion ?: coedit?.artifact?.version ?: 1,
            idempotencyKey = idempotencyKey,
            reason = reason,
            online = current.isNetworkReachable,
        )

        val latestCoedit = current.activeArtifactCoedit
        if (latestCoedit != null) {
            publish(current.copy(activeArtifactCoedit = ArtifactCoeditEngine.recordL2Outcome(latestCoedit, outcome)))
        }
        return outcome
    }

    fun checkL2ActualStatus(idempotencyKey: String): L2ConfirmationResult? {
        val session = sessions.currentSession() ?: return null
        val client = workspaceApi ?: return null
        val coedit = current.activeArtifactCoedit ?: return null
        val artifactId = coedit.confirmDialog?.artifactId ?: coedit.artifact.id
        val outcome = client.checkL2ActualStatus(session, artifactId, idempotencyKey)
        publish(current.copy(activeArtifactCoedit = ArtifactCoeditEngine.recordL2Outcome(coedit, outcome)))
        return outcome
    }

    // --- L1 Co-Editing delegators (backed by ArtifactCoeditEngine + optional remote sync) ---

    fun applyHumanFieldEdit(field: String, value: Any?) {
        val coedit = current.activeArtifactCoedit ?: return
        val session = sessions.currentSession()
        val updated = if (workspaceApi != null && session != null && current.isNetworkReachable) {
            when (val res = workspaceApi.patchArtifact(session, coedit.artifact.id, coedit.artifact.version, mapOf(field to value), "human")) {
                is ApiResult.Success -> ArtifactCoeditEngine.fromArtifact(res.data, coedit.opsHistory + listOfNotNull(res.data.lastOp))
                else -> ArtifactCoeditEngine.applyHumanEdit(coedit, field, value, session.subjectId)
            }
        } else {
            ArtifactCoeditEngine.applyHumanEdit(coedit, field, value, session?.subjectId ?: "local")
        }
        publish(current.copy(activeArtifactCoedit = updated))
    }

    fun unlockField(field: String) {
        val coedit = current.activeArtifactCoedit ?: return
        val session = sessions.currentSession()
        val updated = if (workspaceApi != null && session != null && current.isNetworkReachable) {
            when (val res = workspaceApi.unlockField(session, coedit.artifact.id, field, coedit.artifact.version)) {
                is ApiResult.Success -> ArtifactCoeditEngine.fromArtifact(res.data, coedit.opsHistory + listOfNotNull(res.data.lastOp))
                else -> ArtifactCoeditEngine.unlockField(coedit, field, session.subjectId)
            }
        } else {
            ArtifactCoeditEngine.unlockField(coedit, field, session?.subjectId ?: "local")
        }
        publish(current.copy(activeArtifactCoedit = updated))
    }

    fun acceptSuggestion(field: String, opId: String) {
        val coedit = current.activeArtifactCoedit ?: return
        val session = sessions.currentSession()
        val updated = if (workspaceApi != null && session != null && current.isNetworkReachable) {
            when (val res = workspaceApi.acceptSuggestion(session, coedit.artifact.id, opId, field, coedit.artifact.version)) {
                is ApiResult.Success -> ArtifactCoeditEngine.fromArtifact(res.data, coedit.opsHistory + listOfNotNull(res.data.lastOp))
                else -> ArtifactCoeditEngine.acceptSuggestion(coedit, field, opId, session.subjectId)
            }
        } else {
            ArtifactCoeditEngine.acceptSuggestion(coedit, field, opId, session?.subjectId ?: "local")
        }
        publish(current.copy(activeArtifactCoedit = updated))
    }

    fun ignoreSuggestion(field: String, opId: String) {
        val coedit = current.activeArtifactCoedit ?: return
        val session = sessions.currentSession()
        val updated = if (workspaceApi != null && session != null && current.isNetworkReachable) {
            when (val res = workspaceApi.ignoreSuggestion(session, coedit.artifact.id, opId, field, coedit.artifact.version)) {
                is ApiResult.Success -> ArtifactCoeditEngine.fromArtifact(res.data, coedit.opsHistory + listOfNotNull(res.data.lastOp))
                else -> ArtifactCoeditEngine.ignoreSuggestion(coedit, field, opId, session.subjectId)
            }
        } else {
            ArtifactCoeditEngine.ignoreSuggestion(coedit, field, opId, session?.subjectId ?: "local")
        }
        publish(current.copy(activeArtifactCoedit = updated))
    }

    fun undoOperation(opId: String) {
        val coedit = current.activeArtifactCoedit ?: return
        val session = sessions.currentSession()
        val updated = if (workspaceApi != null && session != null && current.isNetworkReachable) {
            when (val res = workspaceApi.undoArtifactOp(session, coedit.artifact.id, opId, coedit.artifact.version)) {
                is ApiResult.Success -> ArtifactCoeditEngine.fromArtifact(res.data, coedit.opsHistory + listOfNotNull(res.data.lastOp))
                else -> ArtifactCoeditEngine.undoOperation(coedit, opId, session.subjectId)
            }
        } else {
            ArtifactCoeditEngine.undoOperation(coedit, opId, session?.subjectId ?: "local")
        }
        publish(current.copy(activeArtifactCoedit = updated))
    }

    /**
     * Scope Invalidation (C2C-140 §5.3 & design-system §7):
     * Immediately wipes thread, open objects, context chips, chat drawer, and SSE connection.
     */
    fun invalidateScope() {
        eventStream?.close()
        eventStream = null
        val nextSession = sessions.currentSession()
        publish(
            FieldShellState(
                session = nextSession,
                connectionStatus = current.connectionStatus,
                l2OfflineBlocked = !current.isNetworkReachable,
                l2ButtonLabel = if (!current.isNetworkReachable) "待网络" else "确认执行",
                scopeInvalidationCount = current.scopeInvalidationCount + 1,
            ),
        )
    }

    private fun toSummary(ref: WorkObjectRef): FieldWorkObjectSummary {
        val isBatch = ref.kind in setOf("stocktake_review", "receiving_batch", "approval_batch", "exception_reconcile")
        return normalizeBatchSummary(
            FieldWorkObjectSummary(
                id = ref.objectId,
                kind = ref.kind,
                title = ref.title ?: "${ref.kind}#${ref.objectId.take(8)}",
                statusLabel = "进行中",
                statusKind = StatusKind.FACT,
                sourceRoute = ref.sourceRoute,
                artifactId = ref.artifactId,
                version = ref.version ?: 1,
                isComplexBatchReview = isBatch,
            ),
        )
    }

    private fun normalizeBatchSummary(summary: FieldWorkObjectSummary): FieldWorkObjectSummary {
        val complex = summary.isComplexBatchReview ||
            summary.batchItemCount > 5 ||
            summary.kind in setOf("stocktake_review", "receiving_batch", "approval_batch", "exception_reconcile")
        return if (complex) {
            summary.copy(
                isComplexBatchReview = true,
                desktopHint = summary.desktopHint ?: "去电脑处理",
            )
        } else {
            summary
        }
    }

    override fun close() {
        closed = true
        removeScopeListener()
        eventStream?.close()
        eventStream = null
    }
}
