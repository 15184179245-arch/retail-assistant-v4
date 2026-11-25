package com.retail.smartops.features.auth

import android.os.Handler
import android.os.Looper
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.ui.ErrorPanel
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

@Composable
fun LoginScreen(
    repository: SessionRepository,
    onLoggedIn: (Session) -> Unit,
) {
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var hint by remember { mutableStateOf<String?>(null) }
    var pending by remember { mutableStateOf(false) }
    val executor: ExecutorService = remember { Executors.newSingleThreadExecutor() }
    val mainHandler = remember { Handler(Looper.getMainLooper()) }

    DisposableEffect(Unit) {
        onDispose { executor.shutdownNow() }
    }

    MaterialTheme {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "员工登录",
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold,
                )
                StatusBadge(status = StatusKind.DEVELOPMENT)
            }

            Text(
                "使用真实员工账号登录。登录后按人员、组织、门店隔离会话与草稿。",
                style = MaterialTheme.typography.bodyMedium,
            )

            Text(
                "开发版 · 真实身份入口",
                color = Color(0xFF8A2600),
                style = MaterialTheme.typography.labelSmall,
            )

            OutlinedTextField(
                value = username,
                onValueChange = {
                    username = it
                    error = null
                },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("账号") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
                enabled = !pending,
            )

            OutlinedTextField(
                value = password,
                onValueChange = {
                    password = it
                    error = null
                },
                modifier = Modifier.fillMaxWidth(),
                label = { Text("密码") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                enabled = !pending,
            )

            error?.let { userMsg ->
                ErrorPanel(
                    userMessage = userMsg,
                    hint = hint ?: "请核对员工账号密码后再次尝试。",
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            Button(
                enabled = !pending && username.isNotBlank() && password.isNotBlank(),
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 44.dp),
                onClick = {
                    pending = true
                    error = null
                    hint = null
                    executor.execute {
                        val result = repository.login(username.trim(), password)
                        mainHandler.post {
                            pending = false
                            when (result) {
                                is ApiResult.Success -> {
                                    password = ""
                                    onLoggedIn(result.data)
                                }
                                is ApiResult.Failure -> {
                                    error = result.userMessage
                                    hint = result.hint
                                }
                                ApiResult.Cancelled -> Unit
                            }
                        }
                    }
                },
            ) {
                Text(if (pending) "登录中…" else "登录")
            }
        }
    }
}
