package com.retail.smartops.core.network

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONArray
import org.json.JSONObject

data class HttpResult(val status: Int, val body: String)

interface HttpTransport {
    fun post(path: String, body: String, token: String?): HttpResult
    fun request(method: String, path: String, body: String?, token: String?,
                headers: Map<String, String> = emptyMap()): HttpResult {
        if (method == "POST" && headers.isEmpty()) return post(path, body ?: "{}", token)
        throw IOException("transport does not support this request")
    }
}

class UrlConnectionTransport(private val baseUrl: String) : HttpTransport {
    override fun post(path: String, body: String, token: String?): HttpResult =
        request("POST", path, body, token)

    override fun request(method: String, path: String, body: String?, token: String?,
                         headers: Map<String, String>): HttpResult {
        require(method == "GET" || method == "POST")
        require(headers.keys.all { it == "Idempotency-Key" })
        val connection = (URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 10_000
            readTimeout = 15_000
            doOutput = body != null
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Content-Type", "application/json")
            token?.let { setRequestProperty("Authorization", "Bearer $it") }
            headers.forEach { (key, value) -> setRequestProperty(key, value) }
        }
        return try {
            if (body != null) connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            HttpResult(status, stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: "")
        } finally {
            connection.disconnect()
        }
    }
}

interface JsonAdapter {
    fun encodeObject(values: Map<String, Any?>): String
    fun parseObject(raw: String): Map<String, Any?>?
}

class OrgJsonAdapter : JsonAdapter {
    override fun encodeObject(values: Map<String, Any?>): String =
        JSONObject(values as Map<*, *>).toString()

    override fun parseObject(raw: String): Map<String, Any?>? = try {
        convert(JSONObject(raw)) as? Map<String, Any?>
    } catch (error: Exception) {
        null
    }

    private fun convert(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> value.keys().asSequence().associateWith { key -> convert(value.get(key)) }
        is JSONArray -> (0 until value.length()).map { index -> convert(value.get(index)) }
        else -> value
    }
}

data class LoginData(
    val accessToken: String,
    val subjectId: String,
    val organizationId: String,
    val storeId: String,
    val expiresAtEpochMillis: Long,
)

interface LoginApi {
    fun login(username: String, password: String): ApiResult<LoginData>
    fun logout(token: String): ApiResult<Unit>
}

private val REQUIRED_FIELDS = setOf("success", "data", "code", "error", "user_message", "hint")

class HttpApiClient(
    private val transport: HttpTransport,
    private val json: JsonAdapter = OrgJsonAdapter(),
) : LoginApi {

    override fun login(username: String, password: String): ApiResult<LoginData> {
        val response = try {
            transport.post("/api/v1/auth/login",
                json.encodeObject(mapOf("username" to username, "password" to password)), null)
        } catch (error: IOException) {
            return networkFailure()
        }
        return when {
            response.status == 401 -> unauthorized(response.body)
            else -> {
                val envelope = envelope(response.body)
                if (envelope == null) return invalidEnvelope()
                val success = envelope["success"] as? Boolean ?: return invalidEnvelope()
                val data = envelope["data"] as? Map<*, *>
                if (response.status !in 200..299 || !success) return businessFailure(response.status, envelope, data)
                val login = data.toLoginData() ?: return invalidEnvelope()
                ApiResult.Success(login)
            }
        }
    }

    override fun logout(token: String): ApiResult<Unit> {
        val response = try {
            transport.post("/api/v1/auth/logout", "{}", token)
        } catch (error: IOException) {
            return networkFailure()
        }
        if (response.status == 401) return unauthorized(response.body)
        val envelope = envelope(response.body) ?: return invalidEnvelope()
        if (response.status !in 200..299 || envelope["success"] != true) return businessFailure(response.status, envelope, null)
        return ApiResult.Success(Unit)
    }

    /** Shared six-field transport; caller owns authenticated request scope. */
    fun authenticatedJson(method: String, path: String, token: String,
                          body: Map<String, Any?>? = null,
                          headers: Map<String, String> = emptyMap()): ApiResult<Map<String, Any?>> {
        val response = try {
            transport.request(method, path, body?.let(json::encodeObject), token, headers)
        } catch (error: IOException) { return networkFailure() }
        if (response.status == 401) return unauthorized(response.body)
        val envelope = envelope(response.body) ?: return ApiResult.Failure(
            response.status, "INVALID_ENVELOPE", "服务端返回格式异常，请核对原运行。", null)
        if (response.status !in 200..299 || envelope["success"] != true)
            return businessFailure(response.status, envelope, null)
        val data = envelope["data"] as? Map<String, Any?> ?: return invalidEnvelope()
        return ApiResult.Success(data)
    }

    private fun envelope(raw: String): Map<String, Any?>? {
        val parsed = json.parseObject(raw) ?: return null
        return parsed.takeIf { REQUIRED_FIELDS.all(it::containsKey) }
    }

    private fun businessFailure(status: Int, envelope: Map<String, Any?>,
                                data: Map<*, *>?): ApiResult.Failure = ApiResult.createFailure(
        status = status,
        code = envelope["code"] as? String ?: "UNKNOWN",
        userMessage = envelope["user_message"] as? String ?: "请求失败，请稍后重试。",
        hint = envelope["hint"] as? String,
    )

    private fun unauthorized(raw: String): ApiResult.Failure {
        val envelope = envelope(raw)
        return ApiResult.Failure.Unauthorized(
            code = envelope?.get("code") as? String ?: "UNAUTHORIZED",
            userMessage = envelope?.get("user_message") as? String ?: "登录已失效，请重新登录。",
            hint = envelope?.get("hint") as? String ?: "请核对员工账号密码后再次登录。",
        )
    }

    private fun invalidEnvelope(): ApiResult.Failure = ApiResult.Failure.InvalidEnvelope(
        code = "INVALID_ENVELOPE",
        userMessage = "服务端返回格式异常，请稍后重试。",
        hint = "响应缺少必须的标准信封字段。",
    )

    private fun networkFailure(): ApiResult.Failure = ApiResult.Failure.NetworkError(
        code = "NETWORK_ERROR",
        userMessage = "无法连接服务，请检查网络后重试。",
        hint = "请确认 Wi-Fi 或移动网络连接正常。",
    )
}

private fun Map<*, *>?.toLoginData(): LoginData? {
    if (this == null) return null
    val token = this["access_token"] as? String ?: return null
    val type = this["token_type"] as? String ?: return null
    if (type != "Bearer") return null
    val subject = this["subject_id"] as? String ?: return null
    val organization = this["organization_id"] as? String ?: return null
    val store = this["store_id"] as? String ?: return null
    val expiresRaw = this["expires_at"] as? String ?: return null
    val expires = parseIsoInstant(expiresRaw) ?: return null
    return LoginData(token, subject, organization, store, expires)
}

internal fun parseIsoInstant(raw: String): Long? = try {
    java.time.OffsetDateTime.parse(raw).toInstant().toEpochMilli()
} catch (error: Exception) {
    try {
        java.time.Instant.parse(raw).toEpochMilli()
    } catch (second: Exception) {
        null
    }
}
