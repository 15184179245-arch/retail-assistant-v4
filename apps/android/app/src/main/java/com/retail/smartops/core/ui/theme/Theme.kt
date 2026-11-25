package com.retail.smartops.core.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val RetailLightColorScheme = lightColorScheme(
    primary = RetailColors.brandDark,
    onPrimary = Color.White,
    primaryContainer = RetailColors.surfaceSubtle,
    onPrimaryContainer = RetailColors.brandDark,
    secondary = RetailColors.info,
    onSecondary = Color.White,
    background = RetailColors.background,
    onBackground = RetailColors.ink,
    surface = RetailColors.surface,
    onSurface = RetailColors.ink,
    surfaceVariant = RetailColors.background,
    onSurfaceVariant = RetailColors.muted,
    outline = RetailColors.border,
    error = RetailColors.danger,
    onError = Color.White,
)

/**
 * Retail smart operations design system theme wrapper.
 * Conforms to Material 3 and respects system dynamic font scale.
 */
@Composable
fun RetailTheme(
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = RetailLightColorScheme,
        content = content,
    )
}
