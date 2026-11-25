package com.retail.smartops.features.shell

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.navigation.AppDestination
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius
import com.retail.smartops.core.ui.theme.RetailSpacing

/**
 * C2C-140 §2.5 Android 5-zone bottom navigation bar:
 * `今日` | `对象` | `◉ 扫码/按住说话` | `线程` | `我的`
 * Every touch target strictly enforces >= 48dp (`RetailSpacing.minTouchTarget`).
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun FiveZoneBottomBar(
    activeZone: AppDestination,
    online: Boolean,
    onSelectZone: (AppDestination) -> Unit,
    onTapScan: () -> Unit,
    onHoldSpeak: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .border(1.dp, RetailColors.border),
        color = RetailColors.surface,
        shadowElevation = 4.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 6.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            ZoneTabButton(
                label = "今日",
                icon = "◷",
                selected = activeZone == AppDestination.Today,
                onClick = { onSelectZone(AppDestination.Today) },
                modifier = Modifier.weight(1f),
            )
            ZoneTabButton(
                label = "对象",
                icon = "◫",
                selected = activeZone == AppDestination.Objects ||
                    activeZone == AppDestination.SalesDraft ||
                    activeZone == AppDestination.CatalogSearch ||
                    activeZone == AppDestination.Stocktake ||
                    activeZone == AppDestination.Receiving,
                onClick = { onSelectZone(AppDestination.Objects) },
                modifier = Modifier.weight(1f),
            )

            // Central hero button: Tap = Scan barcode, Long-press = System SpeechRecognizer / fallback
            Box(
                modifier = Modifier
                    .weight(1.45f)
                    .defaultMinSize(
                        minWidth = RetailSpacing.minTouchTarget,
                        minHeight = RetailSpacing.minTouchTarget,
                    )
                    .heightIn(min = 52.dp)
                    .clip(RoundedCornerShape(RetailRadius.pill))
                    .background(RetailColors.brandDark)
                    .combinedClickable(
                        onClick = onTapScan,
                        onLongClick = onHoldSpeak,
                    )
                    .semantics {
                        contentDescription = if (online) {
                            "扫码或按住说话，点击打开扫码，长按启动系统语音"
                        } else {
                            "扫码或条码输入，当前离线扫码可用，语音降级为条码文字输入"
                        }
                    }
                    .padding(horizontal = 10.dp, vertical = 8.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "◉ 扫码/说话",
                    color = Color.White,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }

            ZoneTabButton(
                label = "线程",
                icon = "☰",
                selected = activeZone == AppDestination.Threads || activeZone == AppDestination.AgentChat,
                onClick = { onSelectZone(AppDestination.Threads) },
                modifier = Modifier.weight(1f),
            )
            ZoneTabButton(
                label = "我的",
                icon = "☺",
                selected = activeZone == AppDestination.Profile,
                onClick = { onSelectZone(AppDestination.Profile) },
                modifier = Modifier.weight(1f),
            )
        }
    }
}

@Composable
private fun ZoneTabButton(
    label: String,
    icon: String,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val bg = if (selected) RetailColors.surfaceSubtle else Color.Transparent
    val fg = if (selected) RetailColors.brandDark else RetailColors.muted
    Column(
        modifier = modifier
            .defaultMinSize(
                minWidth = RetailSpacing.minTouchTarget,
                minHeight = RetailSpacing.minTouchTarget,
            )
            .heightIn(min = RetailSpacing.minTouchTarget)
            .clip(RoundedCornerShape(RetailRadius.sm))
            .background(bg)
            .clickable(role = Role.Tab, onClick = onClick)
            .semantics { contentDescription = label }
            .padding(horizontal = 4.dp, vertical = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = icon,
            color = fg,
            fontSize = 14.sp,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
        )
        Text(
            text = label,
            color = fg,
            fontSize = 12.sp,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
            maxLines = 1,
        )
    }
}
