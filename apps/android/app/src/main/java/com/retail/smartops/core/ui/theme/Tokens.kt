package com.retail.smartops.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Design system tokens corresponding to DESIGN.md, docs/ui/design-tokens.json,
 * and docs/ui/design-system.md (synced with apps/web/src/styles/tokens.css).
 */
object RetailColors {
    // Surface & background
    val surface = Color(0xFFFFFFFF)
    val background = Color(0xFFF4F6F8)
    val surfaceSubtle = Color(0xFFEFF7F3)
    val brandDark = Color(0xFF173F35)
    val brandHover = Color(0xFF1F4F43)
    val brandActive = Color(0xFF102A24)

    // Ink (typography)
    val ink = Color(0xFF17232B)
    val inkSecondary = Color(0xFF4C5A56)
    val muted = Color(0xFF65736F)
    val subtle = Color(0xFF6B7B78)

    // Borders & dividers
    val border = Color(0xFFE4E9E8)
    val borderDark = Color(0xFFD7DEDB)
    val borderFocus = Color(0xFF8FC7B5)

    // Semantic status tones (synced with DESIGN.md / tokens.css)
    val info = Color(0xFF246A9A)
    val infoInk = Color(0xFF1F5F86)
    val infoBg = Color(0xFFEAF4FB)
    val infoBorder = Color(0xFFB9D8EA)

    val success = Color(0xFF26725E)
    val successInk = Color(0xFF1F5E4B)
    val successBg = Color(0xFFEAF6F0)
    val successBorder = Color(0xFFB7DFCD)

    val warning = Color(0xFF8A5A00)
    val warningInk = Color(0xFF7A4D00)
    val warningBg = Color(0xFFFFF5DD)
    val warningBorder = Color(0xFFE7C57C)

    val danger = Color(0xFF8A3324)
    val dangerInk = Color(0xFF7D2C1F)
    val dangerBg = Color(0xFFFDF0ED)
    val dangerBorder = Color(0xFFE8B9AD)

    val neutral = Color(0xFF3C4A46)
    val neutralBg = Color(0xFFF1F4F3)
    val neutralBorder = Color(0xFFD7DEDB)

    // L1 Co-Editing triple-encoding tokens (DESIGN.md §2 coedit)
    val aiEditBg = Color(0xFFFEF3C7)
    val aiEditBorder = Color(0xFFD97706)
    val aiEditInk = Color(0xFF78350F)

    val humanLockBg = Color(0xFFEAF4FB)
    val humanLockBorder = Color(0xFF7FB4D9)
    val humanLockInk = Color(0xFF1F5F86)
}

object RetailSpacing {
    val xxs: Dp = 4.dp
    val xs: Dp = 8.dp
    val sm: Dp = 12.dp
    val md: Dp = 16.dp
    val lg: Dp = 24.dp
    val xl: Dp = 32.dp

    // Android touch target requirement (C2C-140 §2.5 / DESIGN.md): minimum 48dp
    val minTouchTarget: Dp = 48.dp
}

object RetailTypography {
    val h1: TextUnit = 28.sp
    val h2: TextUnit = 22.sp
    val subtitle: TextUnit = 16.sp
    val body: TextUnit = 14.sp
    val caption: TextUnit = 12.sp
    val eyebrow: TextUnit = 12.sp
}

object RetailRadius {
    val sm: Dp = 8.dp
    val md: Dp = 10.dp
    val lg: Dp = 14.dp
    val card: Dp = 16.dp
    val pill: Dp = 999.dp
}
