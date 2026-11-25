package com.retail.smartops.features.receiving

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.ui.ErrorPanel
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import java.math.BigDecimal

/**
 * PUR-002 scoped intake FORM ONLY. The caller owns login/scope, encrypted
 * MOB-004 persistence, idempotency, network and reconciliation. This screen
 * cannot submit stock or purchase orders. It never shows ledger balances.
 * Local entries are discarded on process death or store-scope change.
 */
data class ReceivingLocalLine(
    val lineKey: String,
    val productLabel: String,
    val quantity: String,
    val unit: String,
    val evidenceSha256: String,
)

data class ReceivingLocalDraft(
    val deliveryNoteSha256: String,
    val lines: List<ReceivingLocalLine>,
    val verifiedExternalReceipt: Boolean = false,
)

@Composable
fun ReceivingScreen(
    scopeKey: String,
    onReviewDraft: (ReceivingLocalDraft) -> Unit,
) {
    var noteSha by remember(scopeKey) { mutableStateOf("") }
    var lineKey by remember(scopeKey) { mutableStateOf("") }
    var label by remember(scopeKey) { mutableStateOf("") }
    var quantity by remember(scopeKey) { mutableStateOf("") }
    var unit by remember(scopeKey) { mutableStateOf("piece") }
    var evidenceSha by remember(scopeKey) { mutableStateOf("") }
    var rows by remember(scopeKey) { mutableStateOf(emptyList<ReceivingLocalLine>()) }
    var notice by remember(scopeKey) { mutableStateOf("") }
    var errorMessage by remember(scopeKey) { mutableStateOf<String?>(null) }

    fun validSha(value: String): Boolean =
        value.length == 64 && value.all { it in '0'..'9' || it in 'a'..'f' }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = "分批到货 · 人工观察",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            StatusBadge(status = StatusKind.DEVELOPMENT)
        }

        Text(
            text = "只记录本批现场事实；不自动写管家婆、不修改账面库存。不显示账面差异，未知包装换算请交给人工复核。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Text(
            text = "本界面尚未接入正式 API 和离线持久化。离开或切换门店会清空草稿。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        errorMessage?.let { error ->
            ErrorPanel(
                userMessage = error,
                hint = "请核对必填项与输入格式是否有效后再次尝试。",
                modifier = Modifier.fillMaxWidth(),
            )
        }

        OutlinedTextField(
            value = noteSha,
            onValueChange = {
                noteSha = it.trim().lowercase()
                errorMessage = null
            },
            label = { Text("本批送货单证据 SHA-256（64位）") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = lineKey,
            onValueChange = {
                lineKey = it
                errorMessage = null
            },
            label = { Text("本批独立行标识") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = label,
            onValueChange = {
                label = it
                errorMessage = null
            },
            label = { Text("商品规格 / 未确认商品原文") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = quantity,
            onValueChange = {
                quantity = it
                errorMessage = null
            },
            label = { Text("实物观察数量（十进制）") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = unit,
            onValueChange = {
                unit = it
                errorMessage = null
            },
            label = { Text("单位 piece / box / kg / meter") },
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = evidenceSha,
            onValueChange = {
                evidenceSha = it.trim().lowercase()
                errorMessage = null
            },
            label = { Text("本行现场证据 SHA-256") },
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 44.dp),
            onClick = {
                val amount = quantity.toBigDecimalOrNull()
                val validAmount = amount != null && amount >= BigDecimal.ZERO &&
                    amount.scale() <= 3
                val safeUnit = unit in setOf("piece", "box", "kg", "meter")
                if (!validAmount || !safeUnit || label.isBlank() || lineKey.isBlank()
                    || rows.any { it.lineKey == lineKey } || !validSha(evidenceSha)
                ) {
                    errorMessage = "必填项、单位、行标识或证据无效；不得自动换算。"
                    notice = ""
                } else {
                    errorMessage = null
                    rows = rows + ReceivingLocalLine(
                        lineKey.trim(),
                        label.trim(),
                        amount!!.toPlainString(),
                        unit,
                        evidenceSha,
                    )
                    lineKey = ""
                    label = ""
                    quantity = ""
                    evidenceSha = ""
                    notice = "已加入本地观察草稿，尚未提交或确认收货。"
                }
            },
        ) {
            Text("加入本批草稿")
        }

        rows.forEach { item ->
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(text = item.lineKey, fontWeight = FontWeight.SemiBold)
                    Text(text = "${item.productLabel}: ${item.quantity} ${item.unit}", style = MaterialTheme.typography.bodySmall)
                }
                StatusBadge(status = StatusKind.UNVERIFIED)
            }
        }

        Spacer(Modifier.height(4.dp))

        Button(
            enabled = validSha(noteSha) && rows.isNotEmpty(),
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 44.dp),
            onClick = {
                onReviewDraft(ReceivingLocalDraft(noteSha, rows.toList()))
                notice = "已交给宿主人工复核流程；不是已入库或正式收货。"
                errorMessage = null
            },
        ) {
            Text("交人工复核（非正式入库）")
        }

        if (notice.isNotBlank()) {
            Text(
                text = notice,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.primary,
            )
        }
    }
}
