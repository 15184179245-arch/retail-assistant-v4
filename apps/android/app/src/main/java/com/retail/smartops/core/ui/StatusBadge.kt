package com.retail.smartops.core.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius

/**
 * Visual tone for status rendering.
 */
enum class StatusTone {
    NEUTRAL,
    INFO,
    SUCCESS,
    WARNING,
    DANGER,
}

/**
 * Canonical statuses matching docs/ui/status-vocabulary.json.
 * Single source of truth across Android and Web.
 */
enum class StatusKind(
    val kind: String,
    val label: String,
    val tone: StatusTone,
    val icon: String,
    val allowSuccessTone: Boolean,
) {
    DEVELOPMENT("development", "开发版", StatusTone.WARNING, "⚙", false),
    PRODUCTION("production", "营业环境", StatusTone.NEUTRAL, "●", false),
    SUGGESTION("suggestion", "建议", StatusTone.NEUTRAL, "◇", false),
    FACT("fact", "已确认事实", StatusTone.INFO, "◆", false),
    PENDING_SYNC("pending_sync", "待同步", StatusTone.INFO, "↻", false),
    SETTLED("settled", "已结算（外部）", StatusTone.INFO, "¤", false),
    VERIFIED("verified", "已核验", StatusTone.SUCCESS, "✓", true),
    UNVERIFIED("unverified", "待核实", StatusTone.WARNING, "?", false),
    WAITING("waiting", "等待中", StatusTone.INFO, "…", false),
    ERROR("error", "失败", StatusTone.DANGER, "!", false);

    init {
        // Enforce success tone restriction: only verified may use success tone
        if (tone == StatusTone.SUCCESS) {
            require(allowSuccessTone) {
                "StatusKind '$kind' cannot use StatusTone.SUCCESS because allowSuccessTone is false"
            }
        }
        if (kind == "unverified" || kind == "suggestion") {
            require(!allowSuccessTone) {
                "StatusKind '$kind' must never allow success tone"
            }
            require(icon != "✓") {
                "StatusKind '$kind' must never use completion checkmark icon ✓"
            }
        }
    }

    companion object {
        fun fromKind(raw: String): StatusKind? =
            entries.firstOrNull { it.kind.equals(raw.trim(), ignoreCase = true) }
    }
}

data class BadgeColors(
    val text: Color,
    val bg: Color,
    val border: Color,
)

fun getBadgeColors(tone: StatusTone): BadgeColors = when (tone) {
    StatusTone.NEUTRAL -> BadgeColors(RetailColors.neutral, RetailColors.neutralBg, RetailColors.neutralBorder)
    StatusTone.INFO -> BadgeColors(RetailColors.info, RetailColors.infoBg, RetailColors.infoBorder)
    StatusTone.SUCCESS -> BadgeColors(RetailColors.success, RetailColors.successBg, RetailColors.successBorder)
    StatusTone.WARNING -> BadgeColors(RetailColors.warning, RetailColors.warningBg, RetailColors.warningBorder)
    StatusTone.DANGER -> BadgeColors(RetailColors.danger, RetailColors.dangerBg, RetailColors.dangerBorder)
}

/**
 * Text + Icon dual-encoding status badge.
 * Provides accessible contentDescription including optional source and timestamp.
 */
@Composable
fun StatusBadge(
    status: StatusKind,
    modifier: Modifier = Modifier,
    source: String? = null,
    timestamp: String? = null,
) {
    val colors = getBadgeColors(status.tone)
    val description = buildString {
        append(status.label)
        if (!source.isNullOrBlank()) {
            append(" · 来源: ").append(source)
        }
        if (!timestamp.isNullOrBlank()) {
            append(" · 时点: ").append(timestamp)
        }
    }

    Row(
        modifier = modifier
            .semantics { contentDescription = description }
            .background(colors.bg, RoundedCornerShape(RetailRadius.pill))
            .border(1.dp, colors.border, RoundedCornerShape(RetailRadius.pill))
            .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "${status.icon} ${status.label}",
            color = colors.text,
            fontSize = 12.sp,
            fontWeight = FontWeight.SemiBold,
        )
    }
}
