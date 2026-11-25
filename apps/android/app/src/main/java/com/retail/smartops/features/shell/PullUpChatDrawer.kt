package com.retail.smartops.features.shell

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.network.ConnectionStatus
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius
import com.retail.smartops.core.ui.theme.RetailSpacing
import com.retail.smartops.features.blocks.BlockInteractionCallbacks
import com.retail.smartops.features.blocks.UiBlockView

/**
 * C2C-140 §2.5 Pull-up half-screen AI chat drawer anchored above the 5-zone bottom bar.
 * Carries scanned barcode / active Work Object context chips and renders validated UI blocks.
 */
@Composable
fun PullUpChatDrawer(
    state: FieldShellState,
    blockCallbacks: BlockInteractionCallbacks,
    onToggleExpanded: (Boolean) -> Unit,
    onRemoveChip: (String) -> Unit,
    onSendPrompt: (String) -> Unit,
    onRequestVoice: () -> Unit,
    onOpenScan: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var inputText by remember(state.session?.subjectId) { mutableStateOf("") }

    Surface(
        modifier = modifier
            .fillMaxWidth()
            .border(1.dp, RetailColors.border, RoundedCornerShape(topStart = RetailRadius.card, topEnd = RetailRadius.card)),
        shape = RoundedCornerShape(topStart = RetailRadius.card, topEnd = RetailRadius.card),
        color = RetailColors.surface,
        shadowElevation = 8.dp,
    ) {
        Column(modifier = Modifier.fillMaxWidth()) {
            // Pull handle / collapsed summary bar (>= 48dp touch target)
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = RetailSpacing.minTouchTarget)
                    .clickable { onToggleExpanded(!state.chatDrawerExpanded) }
                    .padding(horizontal = RetailSpacing.sm, vertical = 8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    Box(
                        modifier = Modifier
                            .width(36.dp)
                            .heightIn(min = 4.dp)
                            .background(RetailColors.borderDark, RoundedCornerShape(RetailRadius.pill)),
                    )
                    Text(
                        text = if (state.chatDrawerExpanded) {
                            "对话抽屉（收起）"
                        } else {
                            "上拉对话抽屉 · 芯片 ${state.contextChips.size} 项"
                        },
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 13.sp,
                        color = RetailColors.ink,
                    )
                }
                if (!state.isNetworkReachable) {
                    StatusBadge(status = StatusKind.UNVERIFIED, source = "待网络")
                } else if (!state.isModelAvailable) {
                    StatusBadge(status = StatusKind.WARNING_MODEL_OFFLINE_FALLBACK(), source = "模型离线")
                } else {
                    Text(
                        text = if (state.chatDrawerExpanded) "▾ 收起" else "▴ 展开对对象说话",
                        color = RetailColors.brandDark,
                        fontWeight = FontWeight.Bold,
                        fontSize = 12.sp,
                    )
                }
            }

            if (state.chatDrawerExpanded) {
                HorizontalDivider(color = RetailColors.border)

                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 360.dp)
                        .padding(RetailSpacing.sm),
                    verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
                ) {
                    // Context chips strip (scanned barcodes & current work object)
                    if (state.contextChips.isNotEmpty()) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            state.contextChips.forEach { chip ->
                                Surface(
                                    shape = RoundedCornerShape(RetailRadius.pill),
                                    color = RetailColors.surfaceSubtle,
                                    border = BorderStroke(1.dp, RetailColors.borderFocus),
                                ) {
                                    Row(
                                        modifier = Modifier
                                            .heightIn(min = 36.dp)
                                            .padding(horizontal = 10.dp, vertical = 4.dp),
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                                    ) {
                                        Text(
                                            text = chip.label,
                                            fontSize = 12.sp,
                                            color = RetailColors.brandDark,
                                            fontWeight = FontWeight.SemiBold,
                                        )
                                        Text(
                                            text = "✕",
                                            fontSize = 12.sp,
                                            color = RetailColors.muted,
                                            modifier = Modifier
                                                .clip(RoundedCornerShape(RetailRadius.pill))
                                                .clickable { onRemoveChip(chip.id) }
                                                .padding(horizontal = 4.dp, vertical = 2.dp),
                                        )
                                    }
                                }
                            }
                        }
                    }

                    // Offline / voice fallback notice
                    if (state.connectionStatus == ConnectionStatus.OFFLINE ||
                        state.connectionStatus == ConnectionStatus.BACKEND_UNREACHABLE
                    ) {
                        Text(
                            text = "待网络：AI 助理与 L2 确认暂不可用；扫码、本地查价与草稿可继续操作。",
                            color = RetailColors.warning,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Medium,
                            modifier = Modifier
                                .fillMaxWidth()
                                .background(RetailColors.warningBg, RoundedCornerShape(RetailRadius.sm))
                                .padding(RetailSpacing.xs),
                        )
                    } else if (state.connectionStatus == ConnectionStatus.MODEL_OFFLINE) {
                        Text(
                            text = "模型服务离线：请改用扫码或工作对象手工编辑。",
                            color = RetailColors.info,
                            fontSize = 12.sp,
                            modifier = Modifier
                                .fillMaxWidth()
                                .background(RetailColors.infoBg, RoundedCornerShape(RetailRadius.sm))
                                .padding(RetailSpacing.xs),
                        )
                    }

                    state.voiceStatusText?.let { voiceMsg ->
                        Text(
                            text = voiceMsg,
                            color = RetailColors.infoInk,
                            fontSize = 12.sp,
                        )
                    }

                    // Messages + Generative UI Blocks
                    Column(
                        modifier = Modifier
                            .weight(1f, fill = false)
                            .fillMaxWidth()
                            .verticalScroll(rememberScrollState()),
                        verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
                    ) {
                        if (state.threadMessages.isEmpty()) {
                            Text(
                                text = "对着当前对象说话、扫码加入芯片，或输入指令（例：帮我核对这条防爆管件报价）。",
                                color = RetailColors.muted,
                                fontSize = 13.sp,
                            )
                        } else {
                            state.threadMessages.forEach { msg ->
                                Column(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .background(
                                            color = when (msg.role) {
                                                "human" -> RetailColors.surfaceSubtle
                                                "agent" -> RetailColors.background
                                                else -> RetailColors.warningBg
                                            },
                                            shape = RoundedCornerShape(RetailRadius.sm),
                                        )
                                        .padding(RetailSpacing.xs),
                                    verticalArrangement = Arrangement.spacedBy(6.dp),
                                ) {
                                    Text(
                                        text = when (msg.role) {
                                            "human" -> "我：${msg.text}"
                                            "agent" -> "AI 助理：${msg.text}"
                                            else -> msg.text
                                        },
                                        color = RetailColors.ink,
                                        fontSize = 13.sp,
                                    )
                                    msg.blocks.forEach { block ->
                                        UiBlockView(
                                            block = block,
                                            online = state.isNetworkReachable,
                                            coeditState = state.activeArtifactCoedit,
                                            callbacks = blockCallbacks,
                                        )
                                    }
                                }
                            }
                        }
                    }

                    // Input bar + Scan / Voice / Send (>= 48dp touch targets)
                    OutlinedTextField(
                        value = inputText,
                        onValueChange = { inputText = it },
                        label = { Text(if (state.voiceFallbackMode) "条码/文字输入（语音已降级）" else "对当前对象提问或输入条码") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        OutlinedButton(
                            onClick = onOpenScan,
                            modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("扫码芯片")
                        }
                        OutlinedButton(
                            onClick = onRequestVoice,
                            modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text("语音")
                        }
                        Button(
                            onClick = {
                                val text = inputText.trim()
                                if (text.isNotEmpty()) {
                                    inputText = ""
                                    onSendPrompt(text)
                                }
                            },
                            enabled = inputText.isNotBlank() && state.isNetworkReachable && state.isModelAvailable,
                            colors = ButtonDefaults.buttonColors(containerColor = RetailColors.brandDark),
                            modifier = Modifier
                                .weight(1f)
                                .heightIn(min = RetailSpacing.minTouchTarget),
                        ) {
                            Text(if (state.isNetworkReachable) "发送" else "待网络")
                        }
                    }
                }
            }
        }
    }
}

private fun StatusKind.Companion.WARNING_MODEL_OFFLINE_FALLBACK(): StatusKind = StatusKind.UNVERIFIED
