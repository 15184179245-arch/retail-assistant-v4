package com.retail.smartops.features.receiving

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.ui.ErrorPanel
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind

internal data class ReceivingFieldInput(
    val label: String,
    val singleLine: Boolean,
    val keyboard: KeyboardOptions,
)

private fun structuredInput(
    label: String, keyboardType: KeyboardType = KeyboardType.Text, imeAction: ImeAction = ImeAction.Next,
) = ReceivingFieldInput(label, true,
    KeyboardOptions(keyboardType = keyboardType, autoCorrectEnabled = false, imeAction = imeAction))

internal val receivingFields = listOf(
    ReceivingField.NOTE_HASH to structuredInput("本批送货单证据 SHA-256（64位，未核验）", KeyboardType.Ascii),
    ReceivingField.LINE_KEY to structuredInput("本批独立行标识"),
    ReceivingField.LABEL to ReceivingFieldInput("商品规格 / 未确认商品原文", false, KeyboardOptions.Default),
    ReceivingField.QUANTITY to structuredInput("实物观察数量（小数点用 .）", KeyboardType.Decimal),
    ReceivingField.UNIT to structuredInput("单位 piece / box / kg / meter", KeyboardType.Ascii),
    ReceivingField.EVIDENCE_HASH to structuredInput("本行现场证据 SHA-256（未核验）", KeyboardType.Ascii, ImeAction.Done),
)

internal fun receivingObservationSummary(lineCount: Int): String =
    if (lineCount == 0) "本页暂无观察，未提交。" else "本页已加入 $lineCount 条观察，未提交。"

@Composable
internal fun ReceivingScreen(form: ReceivingObservationForm) {
    val state by form.state.collectAsState()
    Column(
        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("分批到货 · 人工观察", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            StatusBadge(status = StatusKind.UNVERIFIED)
        }
        Text(RECEIVING_NOT_SUBMITTED, style = MaterialTheme.typography.titleSmall)
        Text(
            "观察仅留本次登录的应用内存，切页或旋转后可返回。退出登录、关闭此工作界面、会话或权限失效、换账号或应用进程结束都会清空。不写库存或账面数据。",
            style = MaterialTheme.typography.bodySmall,
        )

        state.error?.let { error ->
            ErrorPanel(error, hint = "请核对输入；已加入的观察不会被覆盖。", modifier = Modifier.fillMaxWidth())
        }

        for ((field, options) in receivingFields) {
            OutlinedTextField(
                value = state.value(field),
                onValueChange = { form.edit(field, it) },
                label = { Text(options.label) },
                singleLine = options.singleLine,
                keyboardOptions = options.keyboard,
                modifier = Modifier.fillMaxWidth(),
            )
        }

        Button(onClick = form::addLine, modifier = Modifier.fillMaxWidth()) {
            Text("加入本页观察（不提交）")
        }

        // Announce only the local count; do not automatically speak evidence hashes or item data.
        Text(
            receivingObservationSummary(state.lines.size),
            modifier = Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite },
        )
        for (line in state.lines) {
            Column {
                Text(line.lineKey, fontWeight = FontWeight.SemiBold)
                Text("${line.productLabel}: ${line.quantity} ${line.unit}")
                Text("现场证据（未核验）：${line.evidenceSha256}", style = MaterialTheme.typography.bodySmall)
            }
        }

        OutlinedButton(onClick = form::requestDiscard, enabled = state.hasInput) {
            Text("清空本页观察")
        }
    }

    if (state.confirmDiscard) {
        AlertDialog(
            onDismissRequest = form::dismissDiscard,
            title = { Text("清空本页观察？") },
            text = { Text("已加入的观察和未完成输入都会清空。它们未提交，清空后无法恢复。") },
            confirmButton = { TextButton(onClick = form::confirmDiscard) { Text("确认清空") } },
            dismissButton = { TextButton(onClick = form::dismissDiscard) { Text("继续保留") } },
        )
    }
}
