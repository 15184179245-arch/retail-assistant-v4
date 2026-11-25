package com.retail.smartops.features.sales

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Divider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind

@Composable
fun SalesScreen(
    viewModel: SalesViewModel,
    onNavigateToSearch: () -> Unit = {},
) {
    val state by viewModel.state.collectAsState()
    val scrollState = rememberScrollState()
    var archiveOpen by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(scrollState)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // 1. Header Card: Draft ID & Status Badges
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = "销售草稿工作台",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        if (state.stage == "ready") Text("本机已确认 · 本次未发送")
                        else StatusBadge(status = StatusKind.WAITING)
                        StatusBadge(
                            status = if (state.handoff?.isSettled == true) StatusKind.SETTLED else StatusKind.UNVERIFIED,
                        )
                    }
                }
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "草稿单号: ${state.draftId.take(16)}...",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (state.syncState == "pending_sync") {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "● 本机暂存；不会自动发起交接",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(0xFFD97706),
                        fontWeight = FontWeight.Medium,
                    )
                }
            }
        }

        // 2. Ambiguity Alert Warning (R-003 / R-004 boundary)
        if (state.ambiguityWarning != null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFEF3C7)),
                border = BorderStroke(1.dp, Color(0xFFF59E0B)),
            ) {
                Row(
                    modifier = Modifier.padding(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = "⚠️ 歧义拦截：${state.ambiguityWarning}",
                        color = Color(0xFF92400E),
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium,
                    )
                }
            }
        }

        // 3. Error Banner
        if (state.error != null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFEE2E2)),
                border = BorderStroke(1.dp, Color(0xFFEF4444)),
            ) {
                Text(
                    text = state.error ?: "",
                    modifier = Modifier.padding(12.dp),
                    color = Color(0xFFB91C1C),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }

        // 4. Success / Info Message
        if (state.message != null && state.error == null && state.ambiguityWarning == null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFDCFCE7)),
                border = BorderStroke(1.dp, Color(0xFF22C55E)),
            ) {
                Text(
                    text = state.message ?: "",
                    modifier = Modifier.padding(12.dp),
                    color = Color(0xFF15803D),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }

        // 5. Handoff Status Card (R-004 & R-018: Never display "已结算" until confirmed receipt)
        state.handoff?.let { handoff ->
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = if (handoff.isSettled) Color(0xFFF0FDF4) else Color(0xFFF8FAFC),
                ),
                border = BorderStroke(
                    1.dp,
                    if (handoff.isSettled) Color(0xFF22C55E) else Color(0xFF64748B),
                ),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = "收银交接信息",
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold,
                        )
                        StatusBadge(
                            status = if (handoff.isSettled) StatusKind.SETTLED else StatusKind.UNVERIFIED,
                        )
                    }
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "交接单号: ${handoff.handoffId}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        text = "交接状态: ${handoff.state}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (handoff.deliveryRef != null) {
                        Text(
                            text = "投递凭据: ${handoff.deliveryRef}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    if (handoff.externalRef != null) {
                        Text(
                            text = "收银回执: ${handoff.externalRef}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }

        when (state.saveStatus) {
            DraftSaveStatus.SAVING -> Text("正在保存本机草稿…")
            DraftSaveStatus.SAVED -> Text("已保存本机草稿")
            DraftSaveStatus.FAILED -> OutlinedButton(
                onClick = viewModel::retryLocalSave,
                enabled = !state.isOpeningDraft,
            ) { Text("重试保存") }
            DraftSaveStatus.READ_ONLY -> Text("原草稿只读，保留原件及原意图。")
            DraftSaveStatus.EMPTY -> Unit
        }
        OutlinedButton(
            onClick = { archiveOpen = !archiveOpen; if (archiveOpen) viewModel.refreshSavedDrafts() },
            enabled = !state.isOpeningDraft,
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (archiveOpen) "收起本机草稿" else "打开本机已存草稿") }
        if (archiveOpen) {
            val archive = state.archive
            val feedback = when (archive) {
                SalesDraftArchive.Idle -> null
                SalesDraftArchive.Loading -> "正在读取本机草稿…"
                SalesDraftArchive.Failed -> "无法读取本机草稿。"
                is SalesDraftArchive.Loaded -> if (archive.drafts.isEmpty()) {
                    "此员工在本门店暂无已存草稿。"
                } else "已读取 ${archive.drafts.size} 份本机草稿。"
            }
            feedback?.let {
                Text(it, modifier = Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite })
            }
            if (archive == SalesDraftArchive.Failed) {
                OutlinedButton(onClick = viewModel::refreshSavedDrafts, modifier = Modifier.fillMaxWidth()) {
                    Text("重试读取本机草稿")
                }
            }
            if (archive is SalesDraftArchive.Loaded && archive.drafts.isNotEmpty()) {
                LazyColumn(Modifier.heightIn(max = 220.dp)) {
                    items(archive.drafts, key = { it.id }) { saved ->
                        OutlinedButton(
                            onClick = { viewModel.reopenDraft(saved.id) },
                            enabled = saved.id != state.draftId && !state.isOpeningDraft && !state.isLoading,
                            modifier = Modifier.fillMaxWidth(),
                        ) { Text("打开草稿 ${saved.id.take(16)}") }
                    }
                }
            }
        }

        // 6. Cart Line Items Section
        Text(
            text = "已确认明细 (${state.lines.size} 项)",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
        )

        if (state.lines.isEmpty()) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(24.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = "当前购物车为空，请先找货并人工确认商品",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        } else {
            state.lines.forEachIndexed { index, line ->
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                ) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                text = "${index + 1}. ${line.productName}",
                                style = MaterialTheme.typography.bodyLarge,
                                fontWeight = FontWeight.Bold,
                            )
                            Text(
                                text = "¥${formatDraftMoney(line.exactSubtotal)}",
                                style = MaterialTheme.typography.titleSmall,
                                fontWeight = FontWeight.Bold,
                                color = MaterialTheme.colorScheme.primary,
                            )
                        }
                        Spacer(modifier = Modifier.height(2.dp))
                        Text(
                            text = "商品 ID: ${line.productId} · 单价: ¥${line.unitPriceText} / ${line.packagingUnit}（${when (line.unitSource) {
                                DraftUnitSource.CATALOG -> "目录来源"
                                DraftUnitSource.OPERATOR -> "人工填报"
                                null -> "单位待核"
                            }}）",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                            ) {
                                OutlinedButton(
                                    onClick = { viewModel.adjustLineQuantity(line.lineKey, -1) },
                                ) {
                                    Text("-")
                                }
                                Text(
                                    text = formatDraftQuantity(line.quantity),
                                    style = MaterialTheme.typography.bodyLarge,
                                    fontWeight = FontWeight.SemiBold,
                                )
                                OutlinedButton(
                                    onClick = { viewModel.adjustLineQuantity(line.lineKey, 1) },
                                ) {
                                    Text("+")
                                }
                            }
                            OutlinedButton(
                                onClick = { viewModel.removeLine(line.lineKey) },
                                colors = ButtonDefaults.outlinedButtonColors(contentColor = MaterialTheme.colorScheme.error),
                            ) {
                                Text("删除")
                            }
                        }
                    }
                }
            }
        }

        // 7. Summary Card
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer),
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(
                        text = "数量及单位以各商品行为准，不合并不同口径。",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Text(
                        text = "本机估算合计: ¥${formatDraftMoney(state.totalAmount)}",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onPrimaryContainer,
                    )
                }
            }
        }

        // 8. Explicitly selected product; no sample ID, price or automatic cart insertion.
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        ) {
            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("确认并添加商品", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
                OutlinedButton(onClick = onNavigateToSearch) {
                    Text(if (state.pendingProduct == null) "去找货并人工确认商品" else "重新找货选择商品")
                }
                val selected = state.pendingProduct
                if (selected == null) {
                    Text("请先在找货页核对并确认商品；选择本身不会加入草稿。")
                } else {
                    Text(selected.name, style = MaterialTheme.typography.titleMedium)
                    Text("商品 ID: ${selected.productId} · 原码: ${selected.externalCode}")
                    Text("来源声明: ${selected.sourceSystem} · 版本: ${selected.sourceVersion}")
                    Text("仅人工选品；此处未核验报价，数量和单价需明确填写。", style = MaterialTheme.typography.bodySmall)
                    val sourceUnit = state.catalogSalesUnit
                    if (sourceUnit != null) {
                        Text("来源单位: $sourceUnit")
                    } else {
                        Text("来源未提供可用单位，请人工填写并在加入时确认。", color = MaterialTheme.colorScheme.error)
                        OutlinedTextField(
                            value = state.manualUnitInput,
                            onValueChange = viewModel::updatePendingUnit,
                            label = { Text("人工确认的销售单位（必填）") },
                            modifier = Modifier.fillMaxWidth(),
                            singleLine = true,
                        )
                    }
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(
                            value = state.quantityInput,
                            onValueChange = viewModel::updatePendingQuantity,
                            label = { Text("数量（必填）") },
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                            modifier = Modifier.weight(1f),
                            singleLine = true,
                        )
                        OutlinedTextField(
                            value = state.priceInput,
                            onValueChange = viewModel::updatePendingPrice,
                            label = { Text("人工单价 (¥，必填)") },
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                            modifier = Modifier.weight(1f),
                            singleLine = true,
                        )
                    }
                    Button(onClick = { viewModel.confirmPendingProduct() }, modifier = Modifier.fillMaxWidth()) {
                        Text("确认商品、数量、单价与单位后加入草稿")
                    }
                    OutlinedButton(onClick = viewModel::clearPendingProduct) { Text("取消本次选品") }
                }
            }
        }

        // 9. Actions Row
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            OutlinedButton(
                onClick = { viewModel.newDraft() },
                enabled = !state.isOpeningDraft && !state.isLoading,
                modifier = Modifier.weight(1f),
            ) {
                Text("新建草稿")
            }
            Button(
                onClick = { viewModel.markReady() },
                enabled = state.stage == "draft" && state.lines.isNotEmpty() &&
                    !state.isOpeningDraft && !state.submissionLocked,
                modifier = Modifier.weight(1f),
            ) {
                Text("确认本机草稿")
            }
            Button(
                onClick = { viewModel.submitHandoff() },
                enabled = state.lines.isNotEmpty() && !state.isOpeningDraft && !state.isLoading &&
                    state.saveStatus in setOf(DraftSaveStatus.SAVED, DraftSaveStatus.READ_ONLY),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary),
                modifier = Modifier.weight(1f),
            ) {
                Text("查看交接限制")
            }
        }

        Spacer(modifier = Modifier.height(16.dp))
    }
}
