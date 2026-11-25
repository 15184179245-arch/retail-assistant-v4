package com.retail.smartops.features.shell

import android.app.Activity
import android.content.Intent
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.auth.Capabilities
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.navigation.AppDestination
import com.retail.smartops.core.network.ConnectionStatus
import com.retail.smartops.core.scanner.BarcodeScanEntry
import com.retail.smartops.core.scanner.ScanOutcome
import com.retail.smartops.core.ui.ConnectionBar
import com.retail.smartops.core.ui.ErrorPanel
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius
import com.retail.smartops.core.ui.theme.RetailSpacing
import com.retail.smartops.features.blocks.ArtifactCoeditUiState
import com.retail.smartops.features.blocks.BlockInteractionCallbacks
import com.retail.smartops.features.blocks.CommandConfirmDialog
import com.retail.smartops.features.blocks.L1CoeditFieldRow
import com.retail.smartops.features.blocks.ParsedUiBlock
import com.retail.smartops.features.blocks.UiBlockType
import com.retail.smartops.features.blocks.BlockProvenance

/**
 * C2C-140 §2.5 Android AI-First Field Workbench Shell:
 * - Persistent top status bar (env badge, employee identity, network/model status, unsynced local draft count)
 * - Full-screen current Work Object view (with mobile summaries + `去电脑处理` for batch reviews)
 * - Pull-up half-screen AI chat drawer with scanned barcode/object context chips
 * - 5-zone bottom bar (`今日` | `对象` | `◉ 扫码/说话` | `线程` | `我的`, >= 48dp touch targets)
 */
@Composable
fun FieldWorkbenchShell(
    session: Session,
    state: FieldShellState,
    controller: FieldShellController,
    currentNavRoute: String?,
    hasCapability: (String?) -> Boolean,
    capabilities: Set<String>,
    onNavigateObjectRoute: (AppDestination) -> Unit,
    onResumeRemoteThread: (String) -> Unit,
    onLogout: () -> Unit,
    onSwitchLegacyShell: (() -> Unit)? = null,
    workObjectNavContent: @Composable () -> Unit,
) {
    val context = LocalContext.current

    val speechLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val matches = result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            val recognized = matches?.firstOrNull()?.trim().orEmpty()
            if (recognized.isNotEmpty()) {
                controller.onVoiceTranscriptReady(recognized)
            } else {
                controller.onVoiceInputRequested(speechRecognizerAvailable = false)
            }
        } else {
            controller.onVoiceInputRequested(speechRecognizerAvailable = false)
        }
    }

    val requestSystemSpeech: () -> Unit = {
        val available = runCatching { SpeechRecognizer.isRecognitionAvailable(context) }.getOrDefault(false)
        val canLaunch = controller.onVoiceInputRequested(available)
        if (canLaunch) {
            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                putExtra(RecognizerIntent.EXTRA_PROMPT, "请对当前工作对象说话…")
            }
            val launched = runCatching { speechLauncher.launch(intent) }.isSuccess
            if (!launched) {
                controller.onVoiceInputRequested(speechRecognizerAvailable = false)
            }
        }
    }

    val blockCallbacks = remember(controller) {
        BlockInteractionCallbacks(
            onPinToWorkbench = { validBlock ->
                val obj = FieldWorkObjectSummary(
                    id = validBlock.artifactId ?: validBlock.blockId,
                    kind = validBlock.type.wire,
                    title = (validBlock.props["title"] as? String) ?: validBlock.type.displayName,
                    statusLabel = "已固定草稿",
                    statusKind = validBlock.provenance.statusKind,
                    artifactId = validBlock.artifactId,
                )
                controller.focusWorkObject(obj)
            },
            onOpenArtifact = { artifactId ->
                controller.focusWorkObject(
                    FieldWorkObjectSummary(
                        id = artifactId,
                        kind = "quote_draft",
                        title = "工作成果 #$artifactId",
                        statusLabel = "进行中",
                        statusKind = StatusKind.FACT,
                        artifactId = artifactId,
                    ),
                )
            },
            onHumanFieldEdit = { _, field, value -> controller.applyHumanFieldEdit(field, value) },
            onUnlockField = { _, field -> controller.unlockField(field) },
            onAcceptSuggestion = { _, field, opId -> controller.acceptSuggestion(field, opId) },
            onIgnoreSuggestion = { _, field, opId -> controller.ignoreSuggestion(field, opId) },
            onUndoOp = { _, opId -> controller.undoOperation(opId) },
            onOpenActionConfirm = { actionBlock -> controller.openCommandConfirm(actionBlock) },
            onSelectProduct = { props ->
                val code = (props["external_code"] as? String)
                    ?: (props["barcode"] as? String)
                    ?: (props["product_id"] as? String)
                if (!code.isNullOrBlank()) {
                    controller.injectBarcodeChip(code)
                }
            },
        )
    }

    Scaffold(
        topBar = {
            FieldTopStatusBar(
                session = session,
                state = state,
                onLogout = onLogout,
            )
        },
        bottomBar = {
            Column(modifier = Modifier.fillMaxWidth()) {
                PullUpChatDrawer(
                    state = state,
                    blockCallbacks = blockCallbacks,
                    onToggleExpanded = { controller.setChatDrawerExpanded(it) },
                    onRemoveChip = { controller.removeContextChip(it) },
                    onSendPrompt = { controller.submitThreadPrompt(it) },
                    onRequestVoice = requestSystemSpeech,
                    onOpenScan = { controller.setScanSheetOpen(true) },
                )
                FiveZoneBottomBar(
                    activeZone = state.activeZone,
                    online = state.isNetworkReachable,
                    onSelectZone = { zone ->
                        controller.selectZone(zone)
                        if (zone == AppDestination.Objects) {
                            onNavigateObjectRoute(AppDestination.SalesDraft)
                        } else if (zone == AppDestination.Threads && hasCapability(Capabilities.AGENT_RUN)) {
                            // Keep user in Threads overview unless explicitly entering AgentChatScreen
                        }
                    },
                    onTapScan = { controller.setScanSheetOpen(true) },
                    onHoldSpeak = requestSystemSpeech,
                )
            }
        },
    ) { paddingValues ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(RetailColors.background)
                .padding(paddingValues),
        ) {
            when (state.activeZone) {
                AppDestination.Today -> TodayFieldZone(
                    state = state,
                    hasCapability = hasCapability,
                    onOpenObject = { dest ->
                        controller.selectZone(AppDestination.Objects)
                        onNavigateObjectRoute(dest)
                    },
                    onResumeRemoteThread = onResumeRemoteThread,
                )
                AppDestination.Threads -> ThreadsFieldZone(
                    state = state,
                    hasCapability = hasCapability,
                    onResumeRemoteThread = onResumeRemoteThread,
                    onFocusObject = { summary ->
                        controller.focusWorkObject(summary)
                    },
                    onOpenLegacyAgent = {
                        controller.selectZone(AppDestination.Objects)
                        onNavigateObjectRoute(AppDestination.AgentChat)
                    },
                )
                AppDestination.Profile -> ProfileFieldZone(
                    session = session,
                    state = state,
                    capabilities = capabilities,
                    onLogout = onLogout,
                    onSwitchLegacyShell = onSwitchLegacyShell,
                )
                else -> ObjectsFieldZone(
                    state = state,
                    currentNavRoute = currentNavRoute,
                    hasCapability = hasCapability,
                    onNavigateObjectRoute = onNavigateObjectRoute,
                    onHumanFieldEdit = { field, value -> controller.applyHumanFieldEdit(field, value) },
                    onUnlockField = { field -> controller.unlockField(field) },
                    onAcceptSuggestion = { field, opId -> controller.acceptSuggestion(field, opId) },
                    onIgnoreSuggestion = { field, opId -> controller.ignoreSuggestion(field, opId) },
                    onUndoOp = { opId -> controller.undoOperation(opId) },
                    onOpenL2Confirm = {
                        val artifact = state.activeArtifactCoedit?.artifact
                        controller.openCommandConfirm(
                            ParsedUiBlock.Valid(
                                blockId = "l2-${artifact?.id ?: "draft"}",
                                type = UiBlockType.ACTION_CARD,
                                version = "1",
                                props = mapOf(
                                    "action_key" to "artifact.submit",
                                    "title" to "正式提交：${artifact?.title ?: "当前工作成果"}",
                                    "what_happens" to "将当前确认草稿提交至门店正式业务流程并生成回执",
                                    "impact_scope" to "仅作用于单据 ${artifact?.id ?: "当前草稿"}",
                                    "will_not_do" to "不自动改写管家婆正式库存或商品价格",
                                    "approver" to session.username,
                                    "artifact_id" to (artifact?.id ?: ""),
                                    "base_version" to (artifact?.version ?: 1),
                                ),
                                refs = emptyList(),
                                resolvedRefs = emptyMap(),
                                provenance = BlockProvenance("V4 工作台", null, null, StatusKind.FACT),
                                artifactId = artifact?.id,
                            ),
                        )
                    },
                    workObjectNavContent = workObjectNavContent,
                )
            }

            if (state.scanSheetOpen) {
                BarcodeScanModal(
                    onBarcodeConfirmed = { raw -> controller.injectBarcodeChip(raw) },
                    onDismiss = { controller.setScanSheetOpen(false) },
                )
            }

            state.activeArtifactCoedit?.confirmDialog?.let { confirmModel ->
                CommandConfirmDialog(
                    model = confirmModel,
                    online = state.isNetworkReachable,
                    onConfirm = { reason -> controller.submitActiveL2Confirmation(reason) },
                    onCheckActualStatus = { key -> controller.checkL2ActualStatus(key) },
                    onDismiss = { controller.dismissCommandConfirm() },
                )
            }
        }
    }
}

@Composable
fun FieldTopStatusBar(
    session: Session,
    state: FieldShellState,
    onLogout: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(RetailColors.surface)
            .border(1.dp, RetailColors.border)
            .padding(horizontal = RetailSpacing.sm, vertical = RetailSpacing.xs),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            StatusBadge(status = state.environmentKind)
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "员工 ${session.username}",
                    fontWeight = FontWeight.Bold,
                    fontSize = 14.sp,
                    color = RetailColors.ink,
                )
                Text(
                    text = "${session.organizationId} / ${session.storeId}",
                    fontSize = 11.sp,
                    color = RetailColors.muted,
                )
            }
            OutlinedButton(
                onClick = onLogout,
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("退出")
            }
        }

        // Network ● Model ● Unsynced count strip
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            val netLabel = if (state.isNetworkReachable) "网络●" else "网络○"
            val modelLabel = if (state.isModelAvailable) "模型●" else "模型○"
            Text(
                text = "$netLabel $modelLabel · 未同步 ${state.unsyncedLocalDraftCount}",
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                color = if (state.isNetworkReachable) RetailColors.inkSecondary else RetailColors.warning,
                modifier = Modifier.semantics {
                    contentDescription = "$netLabel $modelLabel 未同步本地草稿 ${state.unsyncedLocalDraftCount} 笔"
                },
            )
            if (state.sseConnected) {
                Text(
                    text = "SSE 实时已连 #${state.sseCursor}",
                    fontSize = 11.sp,
                    color = RetailColors.info,
                )
            }
        }

        ConnectionBar(status = state.connectionStatus)

        state.crossDeviceToast?.takeIf { it.isNotBlank() }?.let { toast ->
            Text(
                text = "跨端提示：$toast",
                color = RetailColors.infoInk,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                modifier = Modifier
                    .fillMaxWidth()
                    .background(RetailColors.infoBg, RoundedCornerShape(RetailRadius.sm))
                    .padding(horizontal = 8.dp, vertical = 4.dp),
            )
        }

        state.errorBanner?.takeIf { it.isNotBlank() }?.let { err ->
            ErrorPanel(userMessage = err)
        }
    }
}

@Composable
private fun TodayFieldZone(
    state: FieldShellState,
    hasCapability: (String?) -> Boolean,
    onOpenObject: (AppDestination) -> Unit,
    onResumeRemoteThread: (String) -> Unit,
) {
    var threadIdInput by remember { mutableStateOf(state.activeThreadId ?: "thread-web-001") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(RetailSpacing.md),
        verticalArrangement = Arrangement.spacedBy(RetailSpacing.sm),
    ) {
        Text("今日现场工作台", fontWeight = FontWeight.Bold, fontSize = 18.sp, color = RetailColors.ink)

        Card(
            colors = CardDefaults.cardColors(containerColor = RetailColors.surface),
            border = BorderStroke(1.dp, RetailColors.border),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(
                modifier = Modifier.padding(RetailSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("当前活跃状态", fontWeight = FontWeight.Bold, color = RetailColors.ink)
                Text(
                    text = "已打开工作对象 ${state.openObjects.size} 项 · 未同步本地草稿 ${state.unsyncedLocalDraftCount} 笔",
                    fontSize = 13.sp,
                    color = RetailColors.inkSecondary,
                )
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (hasCapability(Capabilities.SALES_DRAFT)) {
                        Button(
                            onClick = { onOpenObject(AppDestination.SalesDraft) },
                            colors = ButtonDefaults.buttonColors(containerColor = RetailColors.brandDark),
                            modifier = Modifier
                                .weight(1f)
                                .heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("现场开单")
                        }
                    }
                    if (hasCapability(Capabilities.CATALOG_READ)) {
                        OutlinedButton(
                            onClick = { onOpenObject(AppDestination.CatalogSearch) },
                            modifier = Modifier
                                .weight(1f)
                                .heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("扫码找货")
                        }
                    }
                }
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (hasCapability(Capabilities.INVENTORY_STOCKTAKE)) {
                        OutlinedButton(
                            onClick = { onOpenObject(AppDestination.Stocktake) },
                            modifier = Modifier
                                .weight(1f)
                                .heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("现场盘点")
                        }
                    }
                    if (hasCapability(Capabilities.PURCHASING_READ)) {
                        OutlinedButton(
                            onClick = { onOpenObject(AppDestination.Receiving) },
                            modifier = Modifier
                                .weight(1f)
                                .heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("到货清点")
                        }
                    }
                }
            }
        }

        Card(
            colors = CardDefaults.cardColors(containerColor = RetailColors.surface),
            border = BorderStroke(1.dp, RetailColors.border),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(
                modifier = Modifier.padding(RetailSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("跨端线程接续（Web ↔ Android）", fontWeight = FontWeight.Bold, color = RetailColors.ink)
                OutlinedTextField(
                    value = threadIdInput,
                    onValueChange = { threadIdInput = it },
                    label = { Text("线程 ID") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Button(
                    onClick = { if (threadIdInput.isNotBlank()) onResumeRemoteThread(threadIdInput.trim()) },
                    enabled = state.isNetworkReachable && threadIdInput.isNotBlank(),
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text(if (state.isNetworkReachable) "拉取并继续 Web 草稿/线程" else "待网络（离线暂无法拉取远端线程）")
                }
            }
        }
    }
}

@Composable
private fun ObjectsFieldZone(
    state: FieldShellState,
    currentNavRoute: String?,
    hasCapability: (String?) -> Boolean,
    onNavigateObjectRoute: (AppDestination) -> Unit,
    onHumanFieldEdit: (String, String) -> Unit,
    onUnlockField: (String) -> Unit,
    onAcceptSuggestion: (String, String) -> Unit,
    onIgnoreSuggestion: (String, String) -> Unit,
    onUndoOp: (String) -> Unit,
    onOpenL2Confirm: () -> Unit,
    workObjectNavContent: @Composable () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize()) {
        // Quick object switcher strip (>= 48dp touch targets)
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(RetailColors.surface)
                .padding(horizontal = RetailSpacing.xs, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            listOf(
                AppDestination.SalesDraft to "开单",
                AppDestination.CatalogSearch to "找货",
                AppDestination.Stocktake to "盘点",
                AppDestination.Receiving to "到货",
            ).filter { hasCapability(it.first.requiredCapability) }.forEach { (dest, label) ->
                val selected = currentNavRoute == dest.route
                OutlinedButton(
                    onClick = { onNavigateObjectRoute(dest) },
                    colors = ButtonDefaults.outlinedButtonColors(
                        containerColor = if (selected) RetailColors.surfaceSubtle else RetailColors.surface,
                    ),
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text(label, fontSize = 12.sp, fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium)
                }
            }
        }

        HorizontalDivider(color = RetailColors.border)

        // Complex batch review mobile summary banner (C2C-140 §2.5)
        state.activeObject?.takeIf { it.isComplexBatchReview }?.let { batch ->
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(RetailSpacing.sm),
                colors = CardDefaults.cardColors(containerColor = RetailColors.infoBg),
                border = BorderStroke(1.dp, RetailColors.infoBorder),
            ) {
                Column(
                    modifier = Modifier.padding(RetailSpacing.sm),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("${batch.title}（手机端摘要）", fontWeight = FontWeight.Bold, color = RetailColors.infoInk)
                        StatusBadge(status = batch.statusKind)
                    }
                    Text(
                        text = "包含 ${batch.batchItemCount} 项批量明细，手机端仅展示摘要；${batch.desktopHint ?: "去电脑处理"}。",
                        fontSize = 13.sp,
                        color = RetailColors.ink,
                    )
                }
            }
        }

        // Active Workspace Artifact L1/L2 Co-Editing Card (when resumed or pinned)
        state.activeArtifactCoedit?.let { coedit ->
            ActiveArtifactCoeditBanner(
                coedit = coedit,
                online = state.isNetworkReachable,
                l2ButtonLabel = state.l2ButtonLabel,
                onHumanFieldEdit = onHumanFieldEdit,
                onUnlockField = onUnlockField,
                onAcceptSuggestion = onAcceptSuggestion,
                onIgnoreSuggestion = onIgnoreSuggestion,
                onUndoOp = onUndoOp,
                onOpenL2Confirm = onOpenL2Confirm,
            )
        }

        // Full-screen Work Object view (hosts Sales / Search / Stocktake / Receiving NavHost)
        Box(modifier = Modifier.weight(1f)) {
            workObjectNavContent()
        }
    }
}

@Composable
private fun ActiveArtifactCoeditBanner(
    coedit: ArtifactCoeditUiState,
    online: Boolean,
    l2ButtonLabel: String,
    onHumanFieldEdit: (String, String) -> Unit,
    onUnlockField: (String) -> Unit,
    onAcceptSuggestion: (String, String) -> Unit,
    onIgnoreSuggestion: (String, String) -> Unit,
    onUndoOp: (String) -> Unit,
    onOpenL2Confirm: () -> Unit,
) {
    val artifact = coedit.artifact
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = RetailSpacing.sm, vertical = 6.dp),
        colors = CardDefaults.cardColors(containerColor = RetailColors.surface),
        border = BorderStroke(1.dp, RetailColors.borderDark),
    ) {
        Column(
            modifier = Modifier.padding(RetailSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = "共编成果：${artifact.title} (v${artifact.version})",
                    fontWeight = FontWeight.Bold,
                    color = RetailColors.ink,
                    modifier = Modifier.weight(1f),
                )
                StatusBadge(
                    status = if (artifact.status == "submitted") StatusKind.SETTLED else StatusKind.FACT,
                )
            }

            coedit.partialUndoNotice?.let { notice ->
                Text(
                    text = notice,
                    color = RetailColors.warning,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                )
            }

            artifact.payload.entries.take(4).forEach { (field, value) ->
                L1CoeditFieldRow(
                    artifactId = artifact.id,
                    field = field,
                    valueText = value?.toString().orEmpty(),
                    visual = coedit.fieldState(field),
                    onSaveHumanEdit = { newVal -> onHumanFieldEdit(field, newVal) },
                    onUnlock = { onUnlockField(field) },
                    onLock = {},
                    onAcceptSuggestion = { opId -> onAcceptSuggestion(field, opId) },
                    onIgnoreSuggestion = { opId -> onIgnoreSuggestion(field, opId) },
                )
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val lastAiOp = coedit.opsHistory.lastOrNull { it.actorType == "agent" && it.opType == "patch" }
                if (lastAiOp != null) {
                    OutlinedButton(
                        onClick = { onUndoOp(lastAiOp.id) },
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = RetailSpacing.minTouchTarget),
                    ) {
                        Text("撤销 AI 修改")
                    }
                }
                Button(
                    onClick = onOpenL2Confirm,
                    enabled = online,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (online) RetailColors.brandDark else RetailColors.neutral,
                    ),
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text(if (online) l2ButtonLabel else "待网络")
                }
            }
        }
    }
}

@Composable
private fun ThreadsFieldZone(
    state: FieldShellState,
    hasCapability: (String?) -> Boolean,
    onResumeRemoteThread: (String) -> Unit,
    onFocusObject: (FieldWorkObjectSummary) -> Unit,
    onOpenLegacyAgent: () -> Unit,
) {
    var threadInput by remember { mutableStateOf(state.activeThreadId ?: "") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(RetailSpacing.md),
        verticalArrangement = Arrangement.spacedBy(RetailSpacing.sm),
    ) {
        Text("对话线程与跨端接续", fontWeight = FontWeight.Bold, fontSize = 18.sp, color = RetailColors.ink)
        Text(
            text = "当前线程：${state.activeThreadId ?: "未绑定"} · SSE 游标：${state.sseCursor}",
            fontSize = 13.sp,
            color = RetailColors.inkSecondary,
        )

        OutlinedTextField(
            value = threadInput,
            onValueChange = { threadInput = it },
            label = { Text("输入线程 ID 接续 Web 工作台") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = { if (threadInput.isNotBlank()) onResumeRemoteThread(threadInput.trim()) },
            enabled = state.isNetworkReachable && threadInput.isNotBlank(),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = RetailSpacing.minTouchTarget),
        ) {
            Text(if (state.isNetworkReachable) "接续线程与工作对象" else "待网络")
        }

        if (state.openObjects.isNotEmpty()) {
            Text("线程关联工作对象", fontWeight = FontWeight.SemiBold, color = RetailColors.ink)
            state.openObjects.forEach { obj ->
                OutlinedButton(
                    onClick = { onFocusObject(obj) },
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text("${obj.title} (${obj.kind}) · ${obj.statusLabel}")
                }
            }
        }

        if (hasCapability(Capabilities.AGENT_RUN)) {
            OutlinedButton(
                onClick = onOpenLegacyAgent,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("进入完整 Agent 运行定位页")
            }
        }
    }
}

@Composable
private fun ProfileFieldZone(
    session: Session,
    state: FieldShellState,
    capabilities: Set<String>,
    onLogout: () -> Unit,
    onSwitchLegacyShell: (() -> Unit)?,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(RetailSpacing.md),
        verticalArrangement = Arrangement.spacedBy(RetailSpacing.sm),
    ) {
        Text("我的 · 账号与设备隔离", fontWeight = FontWeight.Bold, fontSize = 18.sp, color = RetailColors.ink)
        Card(
            colors = CardDefaults.cardColors(containerColor = RetailColors.surface),
            border = BorderStroke(1.dp, RetailColors.border),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(
                modifier = Modifier.padding(RetailSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("员工：${session.username}", fontWeight = FontWeight.Bold)
                    StatusBadge(status = state.environmentKind)
                }
                Text("组织 / 门店：${session.organizationId} / ${session.storeId}", fontSize = 13.sp)
                Text("权限能力数：${capabilities.size} 项（界面显隐不等于授权）", fontSize = 12.sp, color = RetailColors.muted)
                Text("未同步本地草稿：${state.unsyncedLocalDraftCount} 笔", fontSize = 12.sp, color = RetailColors.inkSecondary)
            }
        }

        onSwitchLegacyShell?.let { switchBack ->
            OutlinedButton(
                onClick = switchBack,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("切换为旧版 3 标签壳（回滚调试）")
            }
        }

        Button(
            onClick = onLogout,
            colors = ButtonDefaults.buttonColors(containerColor = RetailColors.danger),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = RetailSpacing.minTouchTarget),
        ) {
            Text("退出登录并清空当前线程/草稿缓存")
        }
    }
}

@Composable
private fun BarcodeScanModal(
    onBarcodeConfirmed: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var manualBarcode by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("扫码 / 条码录入（离线可用）", fontWeight = FontWeight.Bold) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(
                    text = "扫描或输入条码后，将以「上下文芯片」注入当前线程；商品身份仍须员工人工确认。",
                    fontSize = 13.sp,
                    color = RetailColors.inkSecondary,
                )
                BarcodeScanEntry { outcome ->
                    when (outcome) {
                        is ScanOutcome.Found -> onBarcodeConfirmed(outcome.rawValue)
                        is ScanOutcome.Multiple -> outcome.rawValues.firstOrNull()?.let(onBarcodeConfirmed)
                        else -> Unit
                    }
                }
                OutlinedTextField(
                    value = manualBarcode,
                    onValueChange = { manualBarcode = it },
                    label = { Text("手工输入条码（无相机/离线兜底）") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            Button(
                onClick = { if (manualBarcode.isNotBlank()) onBarcodeConfirmed(manualBarcode.trim()) },
                enabled = manualBarcode.isNotBlank(),
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("加入线程芯片")
            }
        },
        dismissButton = {
            TextButton(
                onClick = onDismiss,
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("取消")
            }
        },
    )
}
