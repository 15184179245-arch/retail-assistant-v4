package com.retail.smartops.core.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.network.ConnectionStatus

/**
 * Visual banner reflecting network reachability and model availability.
 * Automatically disappears when online.
 */
@Composable
fun ConnectionBar(
    status: ConnectionStatus,
    modifier: Modifier = Modifier,
) {
    if (status == ConnectionStatus.ONLINE) {
        return
    }

    val (bg, text, message) = when (status) {
        ConnectionStatus.OFFLINE -> Triple(
            Color(0xFF8A3324), // Danger
            Color.White,
            "网络未连接 · 本地草稿与离线扫码可用，暂无法提交同步",
        )
        ConnectionStatus.BACKEND_UNREACHABLE -> Triple(
            Color(0xFF8A5A00), // Warning
            Color.White,
            "服务端连接中断 · 正在尝试重新连接，请勿重复提交",
        )
        ConnectionStatus.MODEL_OFFLINE -> Triple(
            Color(0xFF246A9A), // Info
            Color.White,
            "AI 助手服务离线 · 手工开单、商品搜索与扫码功能正常可用",
        )
        ConnectionStatus.ONLINE -> Triple(Color.Transparent, Color.Transparent, "")
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .background(bg)
            .padding(horizontal = 14.dp, vertical = 6.dp),
        contentAlignment = Alignment.CenterStart,
    ) {
        Text(
            text = message,
            color = text,
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
        )
    }
}
