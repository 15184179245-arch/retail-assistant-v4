package com.retail.smartops.features.stocktake

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
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
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.scanner.BarcodeScanEntry
import com.retail.smartops.core.scanner.ScanOutcome
import kotlinx.coroutines.launch

@Composable
fun StocktakeScreen(
    viewModel: StocktakeViewModel,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsState()
    val scope = rememberCoroutineScope()

    var startKind by remember { mutableStateOf("partial_area") }
    var startNote by remember { mutableStateOf("A区标准五金配件架") }

    var isKnownLocation by remember { mutableStateOf(true) }
    var locationIdInput by remember { mutableStateOf("loc-shelf-a1") }
    var unknownLocInput by remember { mutableStateOf("") }

    var isKnownProduct by remember { mutableStateOf(true) }
    var productIdInput by remember { mutableStateOf("") }
    var productNameInput by remember { mutableStateOf("") }

    var quantityInput by remember { mutableStateOf("1") }
    var selectedUnit by remember { mutableStateOf("piece") }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // Header
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                text = "现场连续盘点 · 实物观察录入",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = "连续现场盘点工具：支持多货位分布、整盒/散件混盘、拍照取证与断点续盘。观察记录不直接修改正式库存。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        // Session creation card if no active session
        val currentSession = state.session
        if (currentSession == null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Column(
                    modifier = Modifier.padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = "开启盘点区域会话",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    OutlinedTextField(
                        value = startNote,
                        onValueChange = { startNote = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("盘点区域 / 货架备注说明") },
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Button(
                            onClick = {
                                scope.launch {
                                    viewModel.startSession(startKind, null, startNote)
                                }
                            },
                            enabled = startNote.isNotBlank(),
                        ) {
                            Text("开启连续盘点")
                        }
                    }
                }
            }
        } else {
            // Active session card
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = if (currentSession.isOffline) Color(0xFFFFF3E0) else Color(0xFFE8F5E9)
                ),
                border = BorderStroke(
                    1.dp,
                    if (currentSession.isOffline) Color(0xFFEF6C00) else Color(0xFF2E7D32)
                ),
            ) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = if (currentSession.isOffline) "📡 本地离线盘点草稿" else "🌐 在线盘点会话",
                            fontWeight = FontWeight.Bold,
                            color = if (currentSession.isOffline) Color(0xFFE65100) else Color(0xFF1B5E20),
                        )
                        if (state.observations.any { it.syncState != "synced" }) {
                            OutlinedButton(
                                onClick = { scope.launch { viewModel.syncPendingObservations() } },
                                enabled = !state.isSyncing,
                            ) {
                                Text(if (state.isSyncing) "同步中..." else "立即同步")
                            }
                        }
                    }
                    Text("区域: ${currentSession.coverageNote}")
                    Text(
                        text = "会话ID: ${currentSession.sessionId.take(16)}... · 已记录: ${state.observations.size} 项",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        // Warnings / Feedback
        if (state.debounceWarning != null) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFFEBEE)),
                border = BorderStroke(1.dp, Color(0xFFC62828)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = state.debounceWarning!!,
                    color = Color(0xFFC62828),
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(10.dp),
                )
            }
        }

        if (state.message.isNotBlank()) {
            Text(
                text = state.message,
                color = Color(0xFF00796B),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
            )
        }

        if (currentSession != null) {
            // Location Selection Row
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Text("库位选择", fontWeight = FontWeight.Bold)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(selected = isKnownLocation, onClick = { isKnownLocation = true })
                        Text("已知货位编号")
                        Spacer(modifier = Modifier.width(16.dp))
                        RadioButton(selected = !isKnownLocation, onClick = { isKnownLocation = false })
                        Text("临时/未知位置")
                    }
                    if (isKnownLocation) {
                        OutlinedTextField(
                            value = locationIdInput,
                            onValueChange = { locationIdInput = it },
                            label = { Text("货位编号 (如 A-01-02)") },
                            modifier = Modifier.fillMaxWidth(),
                        )
                    } else {
                        OutlinedTextField(
                            value = unknownLocInput,
                            onValueChange = { unknownLocInput = it },
                            label = { Text("未知位置描述 (如 货架顶层临时放置)") },
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }

            // Product & Scanner Card
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text("商品与扫码", fontWeight = FontWeight.Bold)
                    OutlinedTextField(
                        value = productNameInput,
                        onValueChange = { productNameInput = it },
                        label = { Text("商品名称或规格") },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    BarcodeScanEntry { outcome ->
                        when (outcome) {
                            is ScanOutcome.Found -> {
                                productIdInput = outcome.rawValue
                                if (productNameInput.isBlank()) {
                                    productNameInput = "商品_${outcome.rawValue}"
                                }
                            }
                            is ScanOutcome.Multiple -> {
                                if (outcome.rawValues.isNotEmpty()) {
                                    productIdInput = outcome.rawValues.first()
                                }
                            }
                            else -> {}
                        }
                    }

                    // Quantity and Packaging Unit Row
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        OutlinedTextField(
                            value = quantityInput,
                            onValueChange = { quantityInput = it },
                            label = { Text("盘点数量") },
                            modifier = Modifier.weight(1f),
                        )
                        Column(modifier = Modifier.weight(1f)) {
                            Text("单位", style = MaterialTheme.typography.labelSmall)
                            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                listOf("piece" to "支/件", "box" to "整盒", "kg" to "公斤").forEach { (u, label) ->
                                    OutlinedButton(
                                        onClick = { selectedUnit = u },
                                        colors = if (selectedUnit == u)
                                            ButtonDefaults.outlinedButtonColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
                                        else ButtonDefaults.outlinedButtonColors(),
                                    ) {
                                        Text(label, style = MaterialTheme.typography.labelSmall)
                                    }
                                }
                            }
                        }
                    }

                    // Photo Evidence
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        if (state.pendingPhoto != null) {
                            Text(
                                text = "📷 照片已就绪 (${state.pendingPhoto!!.sha256.take(6)}...)",
                                color = Color(0xFF00796B),
                                style = MaterialTheme.typography.bodySmall,
                            )
                            OutlinedButton(onClick = { viewModel.clearPendingPhoto() }) {
                                Text("清除照片")
                            }
                        } else {
                            OutlinedButton(onClick = {
                                viewModel.attachPhoto("synthetic-evidence-image-data".toByteArray())
                            }) {
                                Text("📷 拍摄现场照片取证")
                            }
                        }
                    }

                    // Record Observation Button
                    Button(
                        onClick = {
                            scope.launch {
                                val pId = productIdInput.ifBlank { null }
                                val pLabel = if (pId == null) (productNameInput.ifBlank { "未知商品" }) else null
                                val lId = if (isKnownLocation) locationIdInput.ifBlank { "default-loc" } else null
                                val lLabel = if (!isKnownLocation) (unknownLocInput.ifBlank { "未知位置" }) else null

                                val success = viewModel.recordObservation(
                                    productId = pId,
                                    unknownProductLabel = pLabel,
                                    productName = productNameInput.ifBlank { pId ?: "未知商品" },
                                    locationId = lId,
                                    unknownLocationLabel = lLabel,
                                    locationName = lId ?: lLabel ?: "默认货位",
                                    quantity = quantityInput.ifBlank { "1" },
                                    unit = selectedUnit,
                                )
                                if (success) {
                                    productNameInput = ""
                                    productIdInput = ""
                                    quantityInput = "1"
                                }
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        enabled = productNameInput.isNotBlank() || productIdInput.isNotBlank(),
                    ) {
                        Text("录入此项实物观察")
                    }
                }
            }

            // Observations History
            if (state.observations.isNotEmpty()) {
                Text(
                    text = "现场已录入观察列表 (${state.observations.size} 项)",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                )
                state.observations.asReversed().forEachIndexed { index, obs ->
                    val lineNum = state.observations.size - index
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                    ) {
                        Column(
                            modifier = Modifier.padding(10.dp),
                            verticalArrangement = Arrangement.spacedBy(2.dp),
                        ) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                            ) {
                                Text(
                                    text = "#$lineNum ${obs.productName}",
                                    fontWeight = FontWeight.SemiBold,
                                    style = MaterialTheme.typography.bodyMedium,
                                )
                                Text(
                                    text = "${obs.quantity} ${obs.unit}",
                                    fontWeight = FontWeight.Bold,
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = MaterialTheme.colorScheme.primary,
                                )
                            }
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                            ) {
                                Text(
                                    text = "库位: ${obs.locationName}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                                Text(
                                    text = when (obs.syncState) {
                                        "synced" -> "已同步"
                                        "failed" -> "同步异常"
                                        else -> "待同步"
                                    },
                                    style = MaterialTheme.typography.labelSmall,
                                    fontWeight = FontWeight.Bold,
                                    color = when (obs.syncState) {
                                        "synced" -> Color(0xFF2E7D32)
                                        "failed" -> Color(0xFFC62828)
                                        else -> Color(0xFFEF6C00)
                                    },
                                )
                            }
                            if (obs.photoEvidence != null) {
                                Text(
                                    text = "📷 证据照片已关联 (${obs.photoEvidence.sha256.take(6)}... · ${obs.photoEvidence.uploadState})",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Color(0xFF00796B),
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}
