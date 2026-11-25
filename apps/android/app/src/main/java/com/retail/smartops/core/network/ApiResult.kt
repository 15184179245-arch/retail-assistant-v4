package com.retail.smartops.core.network

/**
 * Standard error abstraction providing user-friendly Chinese messaging and troubleshooting hints.
 */
interface ApiError {
    val status: Int
    val code: String
    val userMessage: String
    val hint: String?
}

/**
 * Standard typed API result returned by ApiClient.
 * Categorizes network outcomes, six-field envelope validation, and HTTP business errors.
 */
sealed class ApiResult<out T> {
    data class Success<T>(val data: T) : ApiResult<T>()

    open class Failure(
        override val status: Int,
        override val code: String,
        override val userMessage: String,
        override val hint: String?,
    ) : ApiResult<Nothing>(), ApiError {

        val isUnauthorized: Boolean get() = status == 401
        val isForbidden: Boolean get() = status == 403
        val isConflict: Boolean get() = status == 409
        val isValidationError: Boolean get() = status == 422
        val isServerError: Boolean get() = status in 500..599
        val isOffline: Boolean get() = status == 0 || code == "NETWORK_ERROR" || code == "OFFLINE"

        class Unauthorized(
            code: String = "UNAUTHORIZED",
            userMessage: String = "登录已失效，请重新登录。",
            hint: String? = "请核对员工账号密码后再次登录。",
        ) : Failure(401, code, userMessage, hint)

        class Forbidden(
            code: String = "FORBIDDEN",
            userMessage: String = "当前账号无权执行此操作。",
            hint: String? = "请联系门店店长或系统管理员授予相应权限。",
        ) : Failure(403, code, userMessage, hint)

        class Conflict(
            code: String = "CONFLICT",
            userMessage: String = "数据版本冲突或记录已存在。",
            hint: String? = "请刷新后获取最新数据再尝试提交。",
        ) : Failure(409, code, userMessage, hint)

        class ValidationError(
            code: String = "VALIDATION_ERROR",
            userMessage: String = "输入内容格式有误，请核对。",
            hint: String? = "请检查商品编码、数量或必填字段。",
        ) : Failure(422, code, userMessage, hint)

        class ServerError(
            status: Int = 500,
            code: String = "SERVER_ERROR",
            userMessage: String = "服务暂时不可用，请稍后重试。",
            hint: String? = "若持续出现请联系系统管理员。",
        ) : Failure(status, code, userMessage, hint)

        class NetworkError(
            code: String = "NETWORK_ERROR",
            userMessage: String = "无法连接服务，请检查网络后重试。",
            hint: String? = "请确认 Wi-Fi 或移动网络连接正常。",
        ) : Failure(0, code, userMessage, hint)

        class InvalidEnvelope(
            code: String = "INVALID_ENVELOPE",
            userMessage: String = "服务端返回格式异常，请稍后重试。",
            hint: String? = "响应缺少必须的标准信封字段。",
        ) : Failure(0, code, userMessage, hint)

        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is Failure) return false
            return status == other.status &&
                code == other.code &&
                userMessage == other.userMessage &&
                hint == other.hint
        }

        override fun hashCode(): Int {
            var result = status
            result = 31 * result + code.hashCode()
            result = 31 * result + userMessage.hashCode()
            result = 31 * result + (hint?.hashCode() ?: 0)
            return result
        }

        override fun toString(): String =
            "Failure(status=$status, code='$code', userMessage='$userMessage', hint=$hint)"
    }

    data object Cancelled : ApiResult<Nothing>()

    companion object {
        fun createFailure(
            status: Int,
            code: String,
            userMessage: String,
            hint: String?,
        ): Failure = when (status) {
            401 -> Failure.Unauthorized(code, userMessage, hint)
            403 -> Failure.Forbidden(code, userMessage, hint)
            409 -> Failure.Conflict(code, userMessage, hint)
            422 -> Failure.ValidationError(code, userMessage, hint)
            in 500..599 -> Failure.ServerError(status, code, userMessage, hint)
            0 -> if (code == "INVALID_ENVELOPE") {
                Failure.InvalidEnvelope(code, userMessage, hint)
            } else {
                Failure.NetworkError(code, userMessage, hint)
            }
            else -> Failure(status, code, userMessage, hint)
        }
    }
}
