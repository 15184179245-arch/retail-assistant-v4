package com.retail.smartops.features.search

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
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.scanner.BarcodeScanEntry
import com.retail.smartops.core.scanner.ScanOutcome
import kotlinx.coroutines.launch

@Composable
fun SearchScreen(
    viewModel: SearchViewModel,
    modifier: Modifier = Modifier,
    onUseConfirmedItem: (() -> Boolean)? = null,
) {
    val state by viewModel.state.collectAsState()
    val scope = rememberCoroutineScope()
    var selectionError by remember { mutableStateOf<String?>(null) }

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
                text = "真实商品查询 · 人工找货确认",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = "现场实物查找核对工具：支持在线精确/模糊检索，断网自动优雅降级本地缓存。不操作正式库存，不创建销售账单。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        // Query input row
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = state.query,
                onValueChange = { viewModel.onQueryChange(it) },
                modifier = Modifier.weight(1f),
                label = { Text("商品编码 / 条码 / 名称") },
                singleLine = true,
            )
            Button(
                onClick = {
                    scope.launch { viewModel.search() }
                },
                enabled = !state.isLoading && state.query.isNotBlank(),
            ) {
                Text("查询")
            }
        }

        // Camera Barcode Scanner Entry
        BarcodeScanEntry { outcome ->
            when (outcome) {
                is ScanOutcome.Found -> {
                    viewModel.onQueryChange(outcome.rawValue)
                    scope.launch { viewModel.search(outcome.rawValue) }
                }
                is ScanOutcome.Multiple -> {
                    if (outcome.rawValues.isNotEmpty()) {
                        val first = outcome.rawValues.first()
                        viewModel.onQueryChange(first)
                        scope.launch { viewModel.search(first) }
                    }
                }
                else -> { /* ignore other outcomes */ }
            }
        }

        // Loading Indicator
        if (state.isLoading) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.Center,
            ) {
                CircularProgressIndicator()
            }
        }

        // Ambiguity Alert Banner (CRITICAL: R-002, R-006)
        if (state.isAmbiguous) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFFEBEE)),
                border = BorderStroke(1.dp, Color(0xFFC62828)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        text = "⚠️ 存在同码或多件精确匹配商品（歧义）",
                        color = Color(0xFFC62828),
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "当前输入对应多个商品。系统禁止自动选品，请务必核对商品规格/原码，并由店员人工点击确认目标商品！",
                        color = Color(0xFFB71C1C),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }

        // Offline / Stale Cache Banner
        if (state.isOffline) {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = if (state.isStaleCache) Color(0xFFFFF3E0) else Color(0xFFE8F5E9)
                ),
                border = BorderStroke(
                    1.dp,
                    if (state.isStaleCache) Color(0xFFEF6C00) else Color(0xFF2E7D32)
                ),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        text = if (state.isStaleCache) "📡 离线模式 · 本地缓存已过期" else "📡 离线模式 · 使用本地快照",
                        color = if (state.isStaleCache) Color(0xFFE65100) else Color(0xFF1B5E20),
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = "当前网络不可用，正在使用本地 Room 缓存（快照版本：${state.cacheSnapshotVersion ?: "未知"}）。" +
                                if (state.isStaleCache) " 请注意：本地缓存已超过有效期，仅供实物参考！" else "",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                }
            }
        }

        // Error message banner
        if (state.error != null) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFFFBE9E7)),
                border = BorderStroke(1.dp, Color(0xFFD84315)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(
                    text = state.error!!,
                    color = Color(0xFFBF360C),
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(12.dp),
                )
            }
        }

        // Confirmed Item Card
        val confirmed = state.confirmedItem
        if (confirmed != null) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Color(0xFFE8F8F5)),
                border = BorderStroke(2.dp, Color(0xFF00796B)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(
                    modifier = Modifier.padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = "✅ 店员已人工确认此商品",
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF004D40),
                        )
                        OutlinedButton(onClick = { viewModel.clearConfirmedItem() }) {
                            Text("重新选择")
                        }
                    }
                    Text(
                        text = confirmed.name,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Text("编码: ${confirmed.externalCode} · 条码: ${confirmed.barcode ?: "无"}")
                    if (confirmed.spec != null || confirmed.unit != null) {
                        Text("规格: ${confirmed.spec ?: "-"} · 单位: ${confirmed.unit ?: "-"}")
                    }
                    Text(
                        text = "来源: ${confirmed.sourceSystem} (${confirmed.sourceVersion}) · 匹配: ${confirmed.matchLabel}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    onUseConfirmedItem?.let { useConfirmedItem ->
                        Button(
                            onClick = {
                                selectionError = if (useConfirmedItem()) null
                                else "选择或销售入口已变化，请重新确认商品和账号权限"
                            },
                            enabled = !state.isLoading,
                        ) {
                            Text("用于销售草稿")
                        }
                        selectionError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                    }
                }
            }
        }

        // Exact Matches List
        if (state.exactMatches.isNotEmpty()) {
            Text(
                text = "精确匹配 (${state.exactMatches.size})",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            state.exactMatches.forEach { item ->
                ProductItemCard(
                    item = item,
                    isConfirmed = (state.confirmedItem?.productId == item.productId),
                    onConfirm = { viewModel.confirmItem(item) },
                )
            }
        }

        // Candidate Matches List
        if (state.candidateMatches.isNotEmpty()) {
            Text(
                text = "候选匹配 (${state.candidateMatches.size})",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            state.candidateMatches.forEach { item ->
                ProductItemCard(
                    item = item,
                    isConfirmed = (state.confirmedItem?.productId == item.productId),
                    onConfirm = { viewModel.confirmItem(item) },
                )
            }
        }

        // Empty state
        if (!state.isLoading && state.query.isNotBlank() && state.exactMatches.isEmpty() && state.candidateMatches.isEmpty() && state.error == null) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text(
                        text = if (state.message.isNotBlank()) state.message else "未找到与 \"${state.query}\" 匹配的商品",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "建议尝试输入部分名称、规格或使用扫码枪重新扫描。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

@Composable
private fun ProductItemCard(
    item: SearchMatchItem,
    isConfirmed: Boolean,
    onConfirm: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        border = BorderStroke(
            1.dp,
            if (isConfirmed) Color(0xFF00796B) else MaterialTheme.colorScheme.outlineVariant
        ),
        colors = CardDefaults.cardColors(
            containerColor = if (isConfirmed) Color(0xFFF0FDF4) else MaterialTheme.colorScheme.surface
        ),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Top,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = item.name,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = "编码: ${item.externalCode} · 条码: ${item.barcode ?: "无"}",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
                Text(
                    text = item.matchLabel,
                    style = MaterialTheme.typography.labelSmall,
                    fontWeight = FontWeight.Bold,
                    color = when (item.matchType) {
                        "exact_code", "exact_barcode" -> Color(0xFF1B5E20)
                        "alias_exact" -> Color(0xFF0277BD)
                        "offline_cache" -> Color(0xFFE65100)
                        else -> Color(0xFF558B2F)
                    },
                    modifier = Modifier
                        .background(
                            color = when (item.matchType) {
                                "exact_code", "exact_barcode" -> Color(0xFFE8F5E9)
                                "alias_exact" -> Color(0xFFE1F5FE)
                                "offline_cache" -> Color(0xFFFFF3E0)
                                else -> Color(0xFFF1F8E9)
                            },
                            shape = MaterialTheme.shapes.small,
                        )
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }

            if (item.spec != null || item.unit != null) {
                Text(
                    text = "规格: ${item.spec ?: "-"} · 单位: ${item.unit ?: "-"}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Text(
                text = "来源: ${item.sourceSystem} · 版本: ${item.sourceVersion}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
            ) {
                if (isConfirmed) {
                    Text(
                        text = "已确认选择",
                        color = Color(0xFF00796B),
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(vertical = 8.dp),
                    )
                } else {
                    Button(
                        onClick = onConfirm,
                        colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary),
                    ) {
                        Text("确认此商品")
                    }
                }
            }
        }
    }
}
