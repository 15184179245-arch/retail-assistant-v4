package com.retail.smartops.features.blocks

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
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
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.network.L2ConfirmationResult
import com.retail.smartops.core.network.L2ConfirmationStatus
import com.retail.smartops.core.ui.ErrorPanel
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius
import com.retail.smartops.core.ui.theme.RetailSpacing

/**
 * History-block interactions. L1 editing is owned only by the current work object.
 */
data class BlockInteractionCallbacks(
    val onPinToWorkbench: (ParsedUiBlock.Valid) -> Unit = {},
    val onOpenArtifact: (String) -> Unit = {},
    val onOpenActionConfirm: (ParsedUiBlock.Valid) -> Unit = {},
    val onSubmitFormRequest: (ParsedUiBlock.Valid, Map<String, String>) -> Unit = { _, _ -> },
    val onSelectProduct: (Map<String, Any?>) -> Unit = {},
)

@Composable
fun UiBlockView(
    block: ParsedUiBlock,
    online: Boolean,
    callbacks: BlockInteractionCallbacks = BlockInteractionCallbacks(),
    modifier: Modifier = Modifier,
) {
    when (block) {
        is ParsedUiBlock.StreamingSkeleton -> StreamingSkeletonBlockView(block, modifier)
        is ParsedUiBlock.DegradedText -> DegradedTextBlockView(block, modifier)
        is ParsedUiBlock.Valid -> ValidUiBlockCard(
            block = block,
            online = online,
            callbacks = callbacks,
            modifier = modifier,
        )
    }
}

@Composable
fun StreamingSkeletonBlockView(
    block: ParsedUiBlock.StreamingSkeleton,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(RetailRadius.md),
        colors = CardDefaults.cardColors(containerColor = RetailColors.neutralBg),
        border = BorderStroke(1.dp, RetailColors.neutralBorder),
    ) {
        Column(
            modifier = Modifier.padding(RetailSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = "生成中 (${block.rawType})",
                    fontWeight = FontWeight.SemiBold,
                    color = RetailColors.neutral,
                    fontSize = 13.sp,
                )
                StatusBadge(status = StatusKind.WAITING)
            }
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(14.dp)
                    .background(RetailColors.border, RoundedCornerShape(RetailRadius.sm)),
            )
            Box(
                modifier = Modifier
                    .fillMaxWidth(0.72f)
                    .height(14.dp)
                    .background(RetailColors.border, RoundedCornerShape(RetailRadius.sm)),
            )
            Text(
                text = block.hint,
                color = RetailColors.muted,
                fontSize = 12.sp,
            )
        }
    }
}

@Composable
fun DegradedTextBlockView(
    block: ParsedUiBlock.DegradedText,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(RetailRadius.md),
        colors = CardDefaults.cardColors(containerColor = RetailColors.warningBg),
        border = BorderStroke(1.dp, RetailColors.warningBorder),
    ) {
        Column(
            modifier = Modifier.padding(RetailSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = "已安全降级为文本 (${block.rawType})",
                    fontWeight = FontWeight.Bold,
                    color = RetailColors.warning,
                    fontSize = 13.sp,
                )
                StatusBadge(
                    status = StatusKind.UNVERIFIED,
                    source = "客户端安全校验",
                )
            }
            Text(
                text = block.fallbackText,
                color = RetailColors.ink,
                fontSize = 14.sp,
            )
            Text(
                text = "降级原因：${block.reason}",
                color = RetailColors.warningInk,
                fontSize = 12.sp,
            )
        }
    }
}

@Composable
private fun ValidUiBlockCard(
    block: ParsedUiBlock.Valid,
    online: Boolean,
    callbacks: BlockInteractionCallbacks,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(RetailRadius.md),
        colors = CardDefaults.cardColors(containerColor = RetailColors.surface),
        border = BorderStroke(1.dp, RetailColors.border),
    ) {
        Column(
            modifier = Modifier.padding(RetailSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
        ) {
            // Header + StatusBadge + visible provenance
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = (block.props["title"] as? String)?.takeIf { it.isNotBlank() }
                        ?: block.type.displayName,
                    fontWeight = FontWeight.Bold,
                    color = RetailColors.ink,
                    fontSize = 15.sp,
                    modifier = Modifier.weight(1f),
                )
                StatusBadge(
                    status = block.provenance.statusKind,
                    source = block.provenance.source,
                    timestamp = block.provenance.syncedAt,
                )
            }

            // Explicit provenance line right beside badge (never hover-only)
            Text(
                text = buildString {
                    append("来源: ").append(block.provenance.source)
                    block.provenance.syncedAt?.let { append(" · 时点: ").append(it) }
                    block.provenance.verifiedBy?.let { append(" · 核验人: ").append(it) }
                    if (block.provenance.stale) append(" · 引用版本已过期")
                },
                color = RetailColors.muted,
                fontSize = 12.sp,
            )

            HorizontalDivider(color = RetailColors.border)

            when (block.type) {
                UiBlockType.PRODUCT_CARD -> RenderProductCard(block, callbacks)
                UiBlockType.PRODUCT_LIST -> RenderProductList(block, callbacks)
                UiBlockType.COMPARE_TABLE -> RenderCompareTable(block)
                UiBlockType.QUOTE_DRAFT, UiBlockType.PURCHASE_DRAFT -> RenderHistoricalDraft(block)
                UiBlockType.STOCK_LOCATION -> RenderStockLocation(block)
                UiBlockType.WORK_ITEM_LIST -> RenderWorkItemList(block)
                UiBlockType.EVIDENCE_PANEL -> RenderEvidencePanel(block)
                UiBlockType.ACTION_CARD -> RenderActionCard(block, online, callbacks)
                UiBlockType.FORM_REQUEST -> RenderFormRequest(block, callbacks)
                UiBlockType.ARTIFACT_LINK -> RenderArtifactLink(block, callbacks)
            }

            // Pin to Workbench button (unless already an artifact link)
            if (block.type != UiBlockType.ARTIFACT_LINK) {
                OutlinedButton(
                    onClick = { callbacks.onPinToWorkbench(block) },
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text("固定到工作台")
                }
            }
        }
    }
}

@Composable
private fun RenderProductCard(
    block: ParsedUiBlock.Valid,
    callbacks: BlockInteractionCallbacks,
) {
    val name = (block.props["name"] as? String) ?: (block.props["title"] as? String) ?: "未命名商品"
    val sku = (block.props["sku"] as? String) ?: (block.props["external_code"] as? String) ?: "-"
    val spec = block.props["spec"] as? String
    val unit = block.props["unit"] as? String
    val resolvedPrice = resolveCommercialDisplay(block, "price", "¥")
    val resolvedStock = resolveCommercialDisplay(block, "stock", "")

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(text = name, fontWeight = FontWeight.SemiBold, color = RetailColors.ink)
        Text(text = "编码: $sku · 规格: ${spec ?: "-"} · 单位: ${unit ?: "-"}", fontSize = 13.sp, color = RetailColors.inkSecondary)
        Text(
            text = "核验单价: ${resolvedPrice ?: "待服务端核验"} · 库存: ${resolvedStock ?: "待服务端核验"}",
            fontSize = 13.sp,
            color = RetailColors.brandDark,
            fontWeight = FontWeight.Medium,
        )
        Button(
            onClick = { callbacks.onSelectProduct(block.props) },
            colors = ButtonDefaults.buttonColors(containerColor = RetailColors.brandDark),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = RetailSpacing.minTouchTarget),
        ) {
            Text("选定并加入当前草稿")
        }
    }
}

@Composable
private fun RenderProductList(
    block: ParsedUiBlock.Valid,
    callbacks: BlockInteractionCallbacks,
) {
    val items = (block.props["items"] as? List<*>)?.mapNotNull { it as? Map<*, *> }.orEmpty()
    if (items.isEmpty()) {
        Text("暂无候选商品", color = RetailColors.muted, fontSize = 13.sp)
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
        items.take(6).forEach { item ->
            val name = (item["name"] as? String) ?: (item["title"] as? String) ?: "候选商品"
            val spec = item["spec"] as? String ?: "-"
            val code = item["external_code"] as? String ?: (item["product_id"] as? String) ?: "-"
            @Suppress("UNCHECKED_CAST")
            OutlinedButton(
                onClick = { callbacks.onSelectProduct(item as Map<String, Any?>) },
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text(text = "$name ($code · $spec)", modifier = Modifier.weight(1f))
            }
        }
        if (items.size > 6) {
            Text(
                text = "共 ${items.size} 条匹配，复杂多行筛选建议去电脑处理",
                fontSize = 12.sp,
                color = RetailColors.muted,
            )
        }
    }
}

@Composable
private fun RenderCompareTable(block: ParsedUiBlock.Valid) {
    // Mobile rule (C2C-140 §1 & §2.5): do NOT mimic cramped wide desktop tables; render stacked cards + desktop hint.
    val rows = ((block.props["rows"] ?: block.props["items"]) as? List<*>)?.mapNotNull { it as? Map<*, *> }.orEmpty()
    Column(verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
        rows.take(4).forEachIndexed { idx, row ->
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(RetailColors.background, RoundedCornerShape(RetailRadius.sm))
                    .padding(RetailSpacing.xs),
            ) {
                val label = (row["name"] as? String) ?: (row["label"] as? String) ?: "方案 ${idx + 1}"
                Text(label, fontWeight = FontWeight.SemiBold, color = RetailColors.ink, fontSize = 13.sp)
                row.entries.filter { it.key != "name" && it.key != "label" }.take(4).forEach { (k, v) ->
                    Text("$k: $v", fontSize = 12.sp, color = RetailColors.inkSecondary)
                }
            }
        }
        if (rows.size > 4) {
            Text(
                text = "对比条目较多（${rows.size} 项），完整多列对比请去电脑处理",
                fontSize = 12.sp,
                color = RetailColors.info,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

internal data class HistoricalDraftPreview(
    val stage: String?,
    val status: String?,
    val fields: List<Pair<String, String>>,
)

internal fun historicalDraftPreview(block: ParsedUiBlock.Valid): HistoricalDraftPreview =
    HistoricalDraftPreview(
        stage = (block.props["stage"] as? String)?.takeIf { it.isNotBlank() },
        status = (block.props["status"] as? String)?.takeIf { it.isNotBlank() },
        fields = block.props.entries.asSequence()
            .filter { it.key !in setOf("title", "stage", "status", "artifact_id", "draft_id") }
            .take(8)
            .map { (field, value) -> field to value?.toString().orEmpty() }
            .toList(),
    )

@Composable
private fun RenderHistoricalDraft(block: ParsedUiBlock.Valid) {
    val preview = historicalDraftPreview(block)
    Column(verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
        Text("历史草稿预览（只读）· 在工作台编辑", fontSize = 12.sp, color = RetailColors.inkSecondary)
        val state = listOfNotNull(preview.stage?.let { "阶段 stage=$it" }, preview.status?.let { "状态 status=$it" })
        if (state.isNotEmpty()) Text(state.joinToString(" · "), fontSize = 12.sp, color = RetailColors.inkSecondary)
        preview.fields.forEach { (field, value) ->
            Text("$field: ${value.ifBlank { "（空）" }}", fontSize = 13.sp, color = RetailColors.ink)
        }
    }
}

/**
 * Mobile L1 co-editing field row implementing:
 * - Triple-encoded AI edit ("✦" + "AI 刚改" + infoBg/infoBorder background)
 * - Automatic human lock ("🔒 人工锁定" + "解锁" button)
 * - Pending AI suggestion diff card ("接受建议" preserves lock / "忽略建议" keeps lock)
 */
@Composable
fun L1CoeditFieldRow(
    artifactId: String,
    field: String,
    valueText: String,
    visual: FieldCoeditVisualState,
    onSaveHumanEdit: (String) -> Unit,
    onUnlock: () -> Unit,
    onLock: () -> Unit,
    onAcceptSuggestion: (opId: String) -> Unit,
    onIgnoreSuggestion: (opId: String) -> Unit,
) {
    var editing by remember(field, valueText) { mutableStateOf(false) }
    var draftInput by remember(field, valueText) { mutableStateOf(valueText) }

    val rowBg = when {
        visual.isAiJustModified -> RetailColors.aiEditBg
        visual.isHumanLocked -> RetailColors.humanLockBg
        else -> RetailColors.background
    }
    val rowBorder = when {
        visual.isAiJustModified -> RetailColors.aiEditBorder
        visual.isHumanLocked -> RetailColors.humanLockBorder
        else -> RetailColors.border
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(rowBg, RoundedCornerShape(RetailRadius.sm))
            .border(1.dp, rowBorder, RoundedCornerShape(RetailRadius.sm))
            .padding(RetailSpacing.xs)
            .semantics {
                contentDescription = buildString {
                    append(field).append(": ").append(valueText)
                    if (visual.isAiJustModified) append(" · ").append(visual.aiBadgeLabel)
                    if (visual.isHumanLocked) append(" · ").append(visual.lockBadgeLabel)
                }
            },
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = field,
                fontWeight = FontWeight.SemiBold,
                fontSize = 13.sp,
                color = RetailColors.ink,
                modifier = Modifier.weight(1f),
            )
            if (visual.isAiJustModified) {
                Text(
                    text = "${visual.aiBadgeIcon} ${visual.aiBadgeLabel}",
                    color = RetailColors.aiEditInk,
                    fontWeight = FontWeight.Bold,
                    fontSize = 12.sp,
                    modifier = Modifier
                        .background(RetailColors.aiEditBg, RoundedCornerShape(RetailRadius.pill))
                        .border(1.dp, RetailColors.aiEditBorder, RoundedCornerShape(RetailRadius.pill))
                        .padding(horizontal = 8.dp, vertical = 2.dp),
                )
            }
            if (visual.isHumanLocked) {
                Text(
                    text = visual.lockBadgeLabel,
                    color = RetailColors.humanLockInk,
                    fontWeight = FontWeight.Bold,
                    fontSize = 12.sp,
                    modifier = Modifier
                        .background(RetailColors.humanLockBg, RoundedCornerShape(RetailRadius.pill))
                        .border(1.dp, RetailColors.humanLockBorder, RoundedCornerShape(RetailRadius.pill))
                        .padding(horizontal = 8.dp, vertical = 2.dp),
                )
            }
        }

        if (editing && artifactId.isNotBlank()) {
            OutlinedTextField(
                value = draftInput,
                onValueChange = { draftInput = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("编辑 $field") },
                singleLine = true,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
                Button(
                    onClick = {
                        editing = false
                        onSaveHumanEdit(draftInput)
                    },
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text("保存并锁定")
                }
                OutlinedButton(
                    onClick = {
                        editing = false
                        draftInput = valueText
                    },
                    modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text("取消")
                }
            }
        } else {
            Text(
                text = valueText.ifBlank { "（空）" },
                fontSize = 14.sp,
                color = RetailColors.ink,
            )
            if (artifactId.isNotBlank()) {
                Row(horizontalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
                    OutlinedButton(
                        onClick = { editing = true },
                        modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                    ) {
                        Text("修改")
                    }
                    if (visual.isHumanLocked) {
                        OutlinedButton(
                            onClick = onUnlock,
                            modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("解锁")
                        }
                    } else {
                        OutlinedButton(
                            onClick = onLock,
                            modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("锁定")
                        }
                    }
                }
            }
        }

        visual.pendingSuggestion?.let { suggestion ->
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(RetailColors.warningBg, RoundedCornerShape(RetailRadius.sm))
                    .border(1.dp, RetailColors.warningBorder, RoundedCornerShape(RetailRadius.sm))
                    .padding(RetailSpacing.xs),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    text = "AI 修改建议（字段已人工锁定，未覆盖）",
                    fontWeight = FontWeight.Bold,
                    color = RetailColors.warning,
                    fontSize = 12.sp,
                )
                Text(
                    text = "建议值: ${suggestion.suggestedValue} · 原因: ${suggestion.reason ?: "AI 优化建议"}",
                    color = RetailColors.ink,
                    fontSize = 13.sp,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
                    Button(
                        onClick = { onAcceptSuggestion(suggestion.opId) },
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = RetailSpacing.minTouchTarget),
                    ) {
                        Text("接受建议")
                    }
                    OutlinedButton(
                        onClick = { onIgnoreSuggestion(suggestion.opId) },
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = RetailSpacing.minTouchTarget),
                    ) {
                        Text("忽略建议")
                    }
                }
            }
        }
    }
}

@Composable
private fun RenderStockLocation(block: ParsedUiBlock.Valid) {
    val locationCode = (block.props["location_code"] as? String)
        ?: (block.props["location_id"] as? String)
        ?: "未知库位"
    val resolvedStock = resolveCommercialDisplay(block, "stock", "")
    Text("货位编号: $locationCode", fontWeight = FontWeight.SemiBold, color = RetailColors.ink)
    Text(
        text = "核验数量: ${resolvedStock ?: "待核验"}",
        fontSize = 13.sp,
        color = RetailColors.inkSecondary,
    )
}

@Composable
private fun RenderWorkItemList(block: ParsedUiBlock.Valid) {
    val items = (block.props["items"] as? List<*>)?.mapNotNull { it as? Map<*, *> }.orEmpty()
    val batchReview = block.props["is_batch_review"] == true || items.size > 5
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        items.take(4).forEach { item ->
            val title = (item["title"] as? String) ?: (item["summary"] as? String) ?: "工作项"
            val state = (item["state"] as? String) ?: "待处理"
            Text("• $title（$state）", fontSize = 13.sp, color = RetailColors.ink)
        }
        if (batchReview) {
            Text(
                text = "共 ${items.size} 项批量复核 · 移动端仅展示摘要，完整处理请去电脑处理",
                color = RetailColors.info,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
            )
        }
    }
}

@Composable
private fun RenderEvidencePanel(block: ParsedUiBlock.Valid) {
    val summary = block.props["summary"] as? String
    val evidences = (block.props["evidences"] ?: block.props["items"]) as? List<*>
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        summary?.let { Text(it, fontSize = 13.sp, color = RetailColors.ink) }
        evidences?.take(4)?.forEach { ev ->
            val text = when (ev) {
                is Map<*, *> -> "${ev["label"] ?: ev["kind"] ?: "证据"}: ${ev["value"] ?: ev["ref"] ?: ""}"
                else -> ev.toString()
            }
            Text("• $text", fontSize = 12.sp, color = RetailColors.inkSecondary)
        }
    }
}

@Composable
private fun RenderActionCard(
    block: ParsedUiBlock.Valid,
    online: Boolean,
    callbacks: BlockInteractionCallbacks,
) {
    val whatHappens = block.props["what_happens"] as? String ?: "执行正式业务命令"
    val impactScope = block.props["impact_scope"] as? String ?: "仅作用于当前指定单据"
    val willNotDo = block.props["will_not_do"] as? String ?: "不自动修改无关库存与账务"
    val approver = block.props["approver"] as? String ?: "当前登录店员 / 门店审批人"

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(RetailColors.warningBg, RoundedCornerShape(RetailRadius.sm))
            .border(1.dp, RetailColors.warningBorder, RoundedCornerShape(RetailRadius.sm))
            .padding(RetailSpacing.xs),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text("L2 待确认动作（需人工二次确认）", fontWeight = FontWeight.Bold, color = RetailColors.warning, fontSize = 13.sp)
        Text("将发生什么：$whatHappens", fontSize = 13.sp, color = RetailColors.ink)
        Text("影响范围：$impactScope", fontSize = 12.sp, color = RetailColors.inkSecondary)
        Text("不会做什么：$willNotDo", fontSize = 12.sp, color = RetailColors.inkSecondary)
        Text("审批人：$approver", fontSize = 12.sp, color = RetailColors.inkSecondary)

        Button(
            onClick = { if (online) callbacks.onOpenActionConfirm(block) },
            enabled = online,
            colors = ButtonDefaults.buttonColors(
                containerColor = if (online) RetailColors.brandDark else RetailColors.neutral,
            ),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = RetailSpacing.minTouchTarget),
        ) {
            Text(if (online) "审阅并确认执行" else "待网络（离线禁正式确认）")
        }
    }
}

@Composable
private fun RenderFormRequest(
    block: ParsedUiBlock.Valid,
    callbacks: BlockInteractionCallbacks,
) {
    val fields = (block.props["fields"] as? List<*>)?.mapNotNull { it as? Map<*, *> }.orEmpty()
    val values = remember(block.blockId) { mutableStateMapOf<String, String>() }

    Column(verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs)) {
        fields.take(5).forEach { f ->
            val key = (f["key"] as? String) ?: (f["name"] as? String) ?: return@forEach
            val label = (f["label"] as? String) ?: key
            OutlinedTextField(
                value = values[key].orEmpty(),
                onValueChange = { values[key] = it },
                label = { Text(label) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Button(
            onClick = { callbacks.onSubmitFormRequest(block, values.toMap()) },
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = RetailSpacing.minTouchTarget),
        ) {
            Text("提交补充参数")
        }
    }
}

@Composable
private fun RenderArtifactLink(
    block: ParsedUiBlock.Valid,
    callbacks: BlockInteractionCallbacks,
) {
    val artifactId = block.props["artifact_id"] as? String ?: ""
    val label = (block.props["label"] as? String) ?: (block.props["title"] as? String) ?: "打开工作成果"
    OutlinedButton(
        onClick = { if (artifactId.isNotBlank()) callbacks.onOpenArtifact(artifactId) },
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = RetailSpacing.minTouchTarget),
    ) {
        Text("打开工作成果：$label")
    }
}

private fun resolveCommercialDisplay(
    block: ParsedUiBlock.Valid,
    key: String,
    prefix: String,
): String? {
    val resolvedVal = block.resolvedRefs[key] ?: block.props[key] ?: return null
    return "$prefix$resolvedVal"
}

/**
 * Mobile L2 `CommandConfirm` dialog (C2C-140 §2.2 & §5.1).
 * - Displays What Happens / Impact Scope / Will Not Do / Approver
 * - Default-folded Technical Details (`idempotencyKey` & `baseVersion`)
 * - Blocks offline execution (`待网络`), missing `idempotencyKey`, and double-tap while `inFlight`
 * - Timeout/unknown outcome transitions to `UNVERIFIED` (`? 待核实`) with `"先查实际状态"` entry and ZERO auto-resend.
 */
@Composable
fun CommandConfirmDialog(
    model: CommandConfirmModel,
    online: Boolean,
    onConfirm: (reason: String) -> Unit,
    onCheckActualStatus: (idempotencyKey: String) -> Unit,
    onDismiss: () -> Unit,
) {
    var reason by remember(model.idempotencyKey) { mutableStateOf("门店现场确认") }
    var techDetailsExpanded by remember(model.idempotencyKey) { mutableStateOf(false) }

    val outcome = model.outcome
    val canSubmit = online &&
        !model.inFlight &&
        model.idempotencyKey.isNotBlank() &&
        outcome?.status != L2ConfirmationStatus.UNVERIFIED &&
        outcome?.status != L2ConfirmationStatus.VERIFIED

    AlertDialog(
        onDismissRequest = { if (!model.inFlight) onDismiss() },
        title = {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(model.title, fontWeight = FontWeight.Bold, fontSize = 17.sp)
                if (!online) {
                    StatusBadge(status = StatusKind.UNVERIFIED, source = "待网络")
                }
            }
        },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("将发生什么：${model.whatHappens}", fontSize = 13.sp, color = RetailColors.ink)
                Text("影响范围：${model.impactScope}", fontSize = 13.sp, color = RetailColors.inkSecondary)
                Text("不会做什么：${model.willNotDo}", fontSize = 13.sp, color = RetailColors.inkSecondary)
                Text("审批人：${model.approver}", fontSize = 13.sp, color = RetailColors.inkSecondary)

                OutlinedTextField(
                    value = reason,
                    onValueChange = { reason = it },
                    label = { Text("确认说明") },
                    enabled = !model.inFlight && outcome?.status != L2ConfirmationStatus.UNVERIFIED,
                    modifier = Modifier.fillMaxWidth(),
                )

                // Default-folded technical details
                OutlinedButton(
                    onClick = { techDetailsExpanded = !techDetailsExpanded },
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = RetailSpacing.minTouchTarget),
                ) {
                    Text(if (techDetailsExpanded) "收起技术详情" else "技术详情（默认折叠）")
                }
                if (techDetailsExpanded) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(RetailColors.neutralBg, RoundedCornerShape(RetailRadius.sm))
                            .padding(RetailSpacing.xs),
                    ) {
                        Text("idempotencyKey: ${model.idempotencyKey}", fontSize = 12.sp, color = RetailColors.neutral)
                        Text("base_version: ${model.baseVersion}", fontSize = 12.sp, color = RetailColors.neutral)
                        Text("action_key: ${model.actionKey}", fontSize = 12.sp, color = RetailColors.neutral)
                    }
                }

                // Outcome feedback
                if (outcome != null) {
                    when (outcome.status) {
                        L2ConfirmationStatus.UNVERIFIED -> {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .background(RetailColors.warningBg, RoundedCornerShape(RetailRadius.sm))
                                    .border(1.dp, RetailColors.warningBorder, RoundedCornerShape(RetailRadius.sm))
                                    .padding(RetailSpacing.xs),
                                verticalArrangement = Arrangement.spacedBy(6.dp),
                            ) {
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Text("结果未知，严禁自动重发", fontWeight = FontWeight.Bold, color = RetailColors.warning)
                                    StatusBadge(status = StatusKind.UNVERIFIED, source = "L2 超时")
                                }
                                Text(outcome.message, fontSize = 13.sp, color = RetailColors.ink)
                                outcome.hint?.let { Text(it, fontSize = 12.sp, color = RetailColors.warningInk) }
                                Button(
                                    onClick = { onCheckActualStatus(model.idempotencyKey) },
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .heightIn(min = RetailSpacing.minTouchTarget),
                                ) {
                                    Text("先查实际状态")
                                }
                            }
                        }
                        L2ConfirmationStatus.VERIFIED, L2ConfirmationStatus.SETTLED -> {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Text(outcome.message, fontSize = 13.sp, color = RetailColors.ink, modifier = Modifier.weight(1f))
                                StatusBadge(status = outcome.statusKind, source = outcome.receiptRef)
                            }
                        }
                        else -> {
                            ErrorPanel(
                                userMessage = outcome.message,
                                hint = outcome.hint,
                            )
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(reason.trim()) },
                enabled = canSubmit && reason.isNotBlank(),
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text(
                    when {
                        !online -> "待网络"
                        model.inFlight -> "提交中…"
                        else -> "确认执行"
                    },
                )
            }
        },
        dismissButton = {
            TextButton(
                onClick = onDismiss,
                enabled = !model.inFlight,
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("关闭")
            }
        },
    )
}
