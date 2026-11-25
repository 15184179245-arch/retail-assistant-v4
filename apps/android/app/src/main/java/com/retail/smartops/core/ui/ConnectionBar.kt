package com.retail.smartops.core.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.retail.smartops.core.network.ConnectionStatus

internal enum class ConnectionNotice(val message: String) {
    NOT_CHECKED("网络状态未检测"),
    OFFLINE("网络未连接 · 本地草稿与离线扫码可用，暂无法提交同步"),
    BACKEND_UNREACHABLE("服务端连接中断 · 正在尝试重新连接，请勿重复提交"),
    MODEL_OFFLINE("AI 助手服务离线 · 手工开单、商品搜索与扫码功能正常可用"),
}

/** A missing observation is unknown, never equivalent to observed online. */
internal fun connectionNoticeFor(status: ConnectionStatus?): ConnectionNotice? = when (status) {
    null -> ConnectionNotice.NOT_CHECKED
    ConnectionStatus.OFFLINE -> ConnectionNotice.OFFLINE
    ConnectionStatus.BACKEND_UNREACHABLE -> ConnectionNotice.BACKEND_UNREACHABLE
    ConnectionStatus.MODEL_OFFLINE -> ConnectionNotice.MODEL_OFFLINE
    ConnectionStatus.ONLINE -> null
}

/** Shows an unknown or negative observation; only an explicit online status hides it. */
@Composable
fun ConnectionBar(
    status: ConnectionStatus?,
    modifier: Modifier = Modifier,
) {
    val notice = connectionNoticeFor(status) ?: return
    val (bg, text) = when (notice) {
        ConnectionNotice.NOT_CHECKED ->
            MaterialTheme.colorScheme.surfaceVariant to MaterialTheme.colorScheme.onSurfaceVariant
        ConnectionNotice.OFFLINE -> Color(0xFF8A3324) to Color.White
        ConnectionNotice.BACKEND_UNREACHABLE -> Color(0xFF8A5A00) to Color.White
        ConnectionNotice.MODEL_OFFLINE -> Color(0xFF246A9A) to Color.White
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .background(bg)
            .padding(horizontal = 14.dp, vertical = 6.dp),
        contentAlignment = Alignment.CenterStart,
    ) {
        Text(
            text = notice.message,
            color = text,
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
        )
    }
}
