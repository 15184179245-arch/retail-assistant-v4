package com.retail.smartops.features.sales

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.retail.smartops.DraftRules
import com.retail.smartops.LocalDraftStore
import com.retail.smartops.SyntheticCatalog
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.scanner.BarcodeScanEntry
import com.retail.smartops.core.scanner.ScanOutcome

@Composable
fun SalesPreviewScreen(
    store: LocalDraftStore,
    session: Session? = null,
    onLogout: () -> Unit = {},
    showAccountHeader: Boolean = false,
) {
    var query by rememberSaveable { mutableStateOf("") }
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    var quantityInput by rememberSaveable { mutableStateOf("1") }
    var statusMessage by rememberSaveable { mutableStateOf("") }
    var lines by remember { mutableStateOf(store.load()) }
    val candidates = SyntheticCatalog.search(query)
    val selected = SyntheticCatalog.products.firstOrNull { it.id == selectedId }
    val quantity = quantityInput.toIntOrNull()?.takeIf { it > 0 }
    val selectedIsCandidate = selected != null && candidates.any { it.id == selected.id }
    val quantityIsAllowed = quantity != null && quantity in 1..DraftRules.MAX_QUANTITY

    Column(modifier = Modifier.fillMaxSize()) {
        if (showAccountHeader && session != null) {
            Column(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "员工 ${session.username} · ${session.organizationId}/${session.storeId}",
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.weight(1f),
                    )
                    OutlinedButton(onClick = onLogout) { Text("退出登录") }
                }
                Text("合成数据", color = Color(0xFF8A2600), fontWeight = FontWeight.Bold)
                Text("仅本机草稿 · 未提交营业", color = Color(0xFF8A2600), fontWeight = FontWeight.Bold)
            }
        }
        Column(
            modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("商品搜索与草稿预览", style = MaterialTheme.typography.headlineSmall)
            Text("以下商品、编码、条码、名称和单价均为合成示例，不代表正式商品、库存或售价。")
            OutlinedTextField(
                value = query,
                onValueChange = {
                    query = it
                    selectedId = null
                    quantityInput = "1"
                },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("输入商品 code、barcode 或名称") },
                singleLine = true,
            )
            BarcodeScanEntry { outcome ->
                when (outcome) {
                    is ScanOutcome.Found -> {
                        query = outcome.rawValue
                        selectedId = null
                        quantityInput = "1"
                        statusMessage = "条码已识别，仅作为搜索文本；请人工核对商品、规格与数量。"
                    }
                    is ScanOutcome.Multiple ->
                        statusMessage = "画面存在多个不同条码；请单独对准一个再扫或手工输入。"
                    ScanOutcome.NoCode ->
                        statusMessage = "未识别条码，请调焦重扫或手工输入。"
                    ScanOutcome.Cancelled ->
                        statusMessage = "已取消扫码，可以手工输入。"
                    ScanOutcome.PermissionDenied ->
                        statusMessage = "没有相机权限，仍可手工输入搜索。"
                    ScanOutcome.Failed ->
                        statusMessage = "相机识别失败，请手工输入。"
                }
            }
            Text("候选 ${candidates.size} 项 · 请人工选择", fontWeight = FontWeight.SemiBold)
            if (candidates.isEmpty()) Text("没有匹配的合成候选")
            candidates.forEach { product ->
                val isSelected = product.id == selectedId
                Card(
                    modifier = Modifier.fillMaxWidth().clickable { selectedId = product.id },
                    colors = CardDefaults.cardColors(containerColor = if (isSelected) Color(0xFFE4F2FF) else MaterialTheme.colorScheme.surface),
                    border = BorderStroke(1.dp, if (isSelected) Color(0xFF246A9A) else Color.LightGray),
                ) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Text(product.name, fontWeight = FontWeight.Bold)
                        Text("合成 code：${product.code}")
                        Text("合成 barcode：${product.barcode}")
                        Text("合成示例单价：¥${formatMoney(product.unitPriceFen)}")
                        Text(if (isSelected) "已由你选择" else "点按人工选择", color = Color(0xFF246A9A))
                    }
                }
            }
            if (selected != null) {
                Card(modifier = Modifier.fillMaxWidth(), border = BorderStroke(1.dp, Color(0xFF246A9A))) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("当前人工选择：${selected.name}", fontWeight = FontWeight.Bold)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            OutlinedButton(
                                enabled = quantityIsAllowed && quantity!! > 1,
                                onClick = {
                                    val updated = DraftRules.changeQuantity(quantity ?: 1, -1)
                                    quantityInput = updated.toString()
                                },
                            ) { Text("−") }
                            Spacer(Modifier.width(8.dp))
                            OutlinedTextField(
                                value = quantityInput,
                                onValueChange = { value ->
                                    val digits = value.filter(Char::isDigit)
                                    if (digits.length <= DraftRules.MAX_QUANTITY.toString().length &&
                                        (digits.isEmpty() || digits.toLongOrNull()?.let { it <= DraftRules.MAX_QUANTITY } == true)
                                    ) {
                                        quantityInput = digits
                                    }
                                },
                                modifier = Modifier.width(130.dp),
                                label = { Text("数量") },
                                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                                singleLine = true,
                            )
                            Spacer(Modifier.width(8.dp))
                            OutlinedButton(
                                enabled = quantityIsAllowed && quantity!! < DraftRules.MAX_QUANTITY,
                                onClick = {
                                    val updated = DraftRules.changeQuantity(quantity ?: 1, 1)
                                    quantityInput = updated.toString()
                                },
                            ) { Text("＋") }
                        }
                        Button(
                            onClick = {
                                val chosen = selected?.takeIf { candidate -> candidates.any { it.id == candidate.id } }
                                if (chosen != null && quantityIsAllowed) {
                                    runCatching { DraftRules.addLine(lines, chosen.id, quantity!!) }
                                        .onSuccess {
                                            lines = it
                                            selectedId = null
                                            quantityInput = "1"
                                            statusMessage = "已加入本机草稿；尚未保存。"
                                        }
                                        .onFailure { statusMessage = it.message ?: "无法加入本机草稿。" }
                                } else {
                                    selectedId = null
                                    statusMessage = "请重新搜索并人工选择当前候选。"
                                }
                            },
                            enabled = selectedIsCandidate && quantityIsAllowed,
                            modifier = Modifier.fillMaxWidth(),
                        ) { Text("加入本机草稿") }
                    }
                }
            }
            Spacer(Modifier.height(2.dp))
            Text("本机草稿（${lines.size} 种合成商品）", style = MaterialTheme.typography.titleLarge)
            if (lines.isEmpty()) Text("草稿为空。选择合成候选并加入后，可保存到本机。")
            lines.forEach { line ->
                val product = SyntheticCatalog.products.firstOrNull { it.id == line.productId }
                if (product != null) {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(product.name, fontWeight = FontWeight.SemiBold)
                                Text("合成 code ${product.code} · 数量 ${line.quantity}")
                            }
                            OutlinedButton(onClick = {
                                DraftRules.changeLineQuantity(lines, line.productId, -1).let { mutation ->
                                    lines = mutation.lines
                                    statusMessage = mutation.statusMessage
                                }
                            }) { Text("−") }
                            Spacer(Modifier.width(4.dp))
                            OutlinedButton(
                                enabled = line.quantity < DraftRules.MAX_QUANTITY,
                                onClick = {
                                    DraftRules.changeLineQuantity(lines, line.productId, 1).let { mutation ->
                                        lines = mutation.lines
                                        statusMessage = mutation.statusMessage
                                    }
                                },
                            ) { Text("＋") }
                        }
                    }
                }
            }
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = {
                        statusMessage = if (runCatching { store.save(lines) }.getOrDefault(false)) {
                            "本机草稿已保存。"
                        } else {
                            "保存失败；草稿尚未确认写入本机存储。"
                        }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("保存本机草稿") }
                OutlinedButton(
                    onClick = {
                        if (runCatching { store.clear() }.getOrDefault(false)) {
                            val mutation = DraftRules.clearDraft()
                            lines = mutation.lines
                            selectedId = null
                            statusMessage = mutation.statusMessage
                        } else {
                            statusMessage = "清空失败；原草稿仍保留。"
                        }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("清空草稿") }
            }
            if (statusMessage.isNotBlank()) Text(statusMessage, fontWeight = FontWeight.SemiBold)
            Text("仅保存在此应用的私有存储。没有联网、同步或提交营业。", style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.height(24.dp))
        }
    }
}

private fun formatMoney(fen: Int): String = "${fen / 100}.${(fen % 100).toString().padStart(2, '0')}"
