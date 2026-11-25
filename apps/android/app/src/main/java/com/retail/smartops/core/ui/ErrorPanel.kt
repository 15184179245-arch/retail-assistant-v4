package com.retail.smartops.core.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.network.ApiError
import com.retail.smartops.core.ui.theme.RetailColors
import com.retail.smartops.core.ui.theme.RetailRadius
import com.retail.smartops.core.ui.theme.RetailSpacing

/**
 * Standard six-field error display panel.
 * Presents user_message prominently in danger tone and provides troubleshooting hint.
 */
@Composable
fun ErrorPanel(
    userMessage: String,
    hint: String? = null,
    modifier: Modifier = Modifier,
    code: String? = null,
    onRetry: (() -> Unit)? = null,
    retryLabel: String = "重试",
) {
    Column(
        modifier = modifier
            .semantics { liveRegion = LiveRegionMode.Assertive }
            .fillMaxWidth()
            .background(RetailColors.dangerBg, RoundedCornerShape(RetailRadius.md))
            .border(1.dp, RetailColors.dangerBorder, RoundedCornerShape(RetailRadius.md))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(RetailSpacing.xs),
        ) {
            Text(
                text = "!",
                color = RetailColors.danger,
                fontWeight = FontWeight.Bold,
                fontSize = 16.sp,
            )
            Text(
                text = userMessage,
                color = RetailColors.danger,
                fontWeight = FontWeight.Bold,
                fontSize = 14.sp,
                modifier = Modifier.weight(1f),
            )
        }

        if (!hint.isNullOrBlank()) {
            Text(
                text = "建议排查：$hint",
                color = RetailColors.ink,
                fontSize = 13.sp,
            )
        }

        if (!code.isNullOrBlank()) {
            Text(
                text = "错误码: $code",
                color = RetailColors.muted,
                fontSize = 11.sp,
            )
        }

        if (onRetry != null) {
            OutlinedButton(
                onClick = onRetry,
                modifier = Modifier.padding(top = 4.dp),
            ) {
                Text(retryLabel)
            }
        }
    }
}

@Composable
fun ErrorPanel(
    error: ApiError,
    modifier: Modifier = Modifier,
    onRetry: (() -> Unit)? = null,
    retryLabel: String = "重试",
) {
    ErrorPanel(
        userMessage = error.userMessage,
        hint = error.hint,
        code = error.code,
        modifier = modifier,
        onRetry = onRetry,
        retryLabel = retryLabel,
    )
}
