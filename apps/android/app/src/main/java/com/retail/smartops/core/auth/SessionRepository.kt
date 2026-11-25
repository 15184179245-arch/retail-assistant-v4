package com.retail.smartops.core.auth

import android.content.Context
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.LoginApi
import com.retail.smartops.core.network.hasValidLoginScope
import com.retail.smartops.core.network.hasValidScope
import com.retail.smartops.core.network.invalidEnvelope
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.security.KeyStore
import java.util.UUID

data class Session(
    val accessToken: String,
    val subjectId: String,
    val organizationId: String,
    val storeId: String,
    val expiresAtEpochMillis: Long,
    val username: String,
)

interface SessionStorage {
    fun save(session: Session)
    fun load(): Session?
    fun clear()
}

class InMemorySessionStorage : SessionStorage {
    private var stored: Session? = null
    override fun save(session: Session) { stored = session }
    override fun load(): Session? = stored
    override fun clear() { stored = null }
}

/**
 * The real app never writes or restores bearer tokens. Revocation can fail both
 * locally and remotely; a restarted process must prove identity with a new login.
 * The active Session lives in the retained repository, not in this policy object.
 */
internal object ProcessOnlySessionStorage : SessionStorage {
    override fun save(session: Session) = Unit
    override fun load(): Session? = null
    override fun clear() = Unit
}

/** Best-effort retirement of a prior APK's locally encrypted bearer, never a draft database. */
internal fun retireLegacySessionArtifacts(context: Context) {
    runCatching {
        context.getSharedPreferences("real_session_restore", Context.MODE_PRIVATE)
            .edit().remove("signed_in_payload_digest").commit()
    }
    runCatching {
        KeyStore.getInstance("AndroidKeyStore").apply { load(null); deleteEntry("retail.real.session") }
    }
    runCatching {
        context.getSharedPreferences("real_session", Context.MODE_PRIVATE)
            .edit().remove("encrypted_session").commit()
    }
}

class RequestScope internal constructor(
    private val repository: SessionRepository,
    internal val generation: Int,
) {
    @Volatile
    internal var cancelled: Boolean = false
        private set

    fun isActive(): Boolean = !cancelled && repository.generation() == generation

    internal fun cancel() { cancelled = true }
}

data class SessionStatus(
    val session: Session?,
    val generation: Int,
    val capabilities: CapabilityState,
    val localClearFailed: Boolean,
)

/**
 * Owns the real employee session. A user switch first invalidates every request
 * scope and clears visible data, so a late response from the previous employee
 * can never be shown or clear the new employee's session.
 */
class SessionRepository(
    private val api: LoginApi,
    private val storage: SessionStorage,
    private val clock: () -> Long = System::currentTimeMillis,
    private val onScopeChanged: () -> Unit = {},
) {
    // Not a credential. Keeps saved Navigation state distinct across process restarts
    // even if the same employee logs back in at the same generation number.
    internal val navigationSessionKey: UUID = UUID.randomUUID()
    @Volatile private var generation = 0
    @Volatile var localClearFailed = false
        private set
    private var session: Session? = loadValidSession()
    private var tokenForLogoutRetry: String? = null
    private val scopes = linkedSetOf<RequestScope>()
    private val scopeListeners = linkedSetOf<() -> Unit>()
    private var capabilityState: CapabilityState =
        if (session != null) CapabilityState(Capabilities.DEFAULT_CAPABILITIES, "v1")
        else CapabilityState(emptySet(), "v0")

    private val _status = MutableStateFlow(status())
    val status: StateFlow<SessionStatus> = _status.asStateFlow()

    private fun status() = SessionStatus(session, generation, capabilityState, localClearFailed)

    @Synchronized
    fun getCapabilities(): Set<String> = capabilityState.capabilities

    @Synchronized
    fun getCapabilityVersion(): String = capabilityState.version

    @Synchronized
    fun hasCapability(capability: String?): Boolean = capabilityState.has(capability)

    @Synchronized
    fun setCapabilities(capabilities: Set<String>, version: String = "v1") {
        capabilityState = if (currentSession() == null) CapabilityState()
            else CapabilityState(capabilities.toSet(), version)
        notifyScopeChanged()
    }

    @Synchronized
    fun clearCapabilities() = setCapabilities(emptySet(), "v0")

    @Synchronized
    fun onScopeInvalidated(listener: () -> Unit): () -> Unit {
        scopeListeners.add(listener)
        return { synchronized(this) { scopeListeners.remove(listener) }; Unit }
    }

    @Synchronized
    fun captureRequest(): Pair<Session, RequestScope>? {
        val value = currentSession() ?: return null
        return value to beginRequest()
    }

    /** Short local persistence only; never run network work while holding this lock. */
    @Synchronized
    fun whileCurrent(expected: Session, block: () -> Unit): Boolean {
        if (currentSession() != expected) return false
        block()
        return true
    }

    @Synchronized
    fun finishRequest(scope: RequestScope): Boolean {
        scopes.remove(scope)
        val active = currentSession() != null && scope.isActive()
        scope.cancel()
        return active
    }


    @Synchronized
    fun currentSession(): Session? = sessionAt(clock())

    private fun sessionAt(now: Long): Session? {
        val value = session ?: return null
        if (value.expiresAtEpochMillis <= now) {
            clearIdentity()
            return null
        }
        return value
    }

    fun canSubmitFormalAction(): Boolean = currentSession() != null

    @Synchronized
    fun beginRequest(): RequestScope =
        RequestScope(this, generation).also { scopes.add(it) }

    /** Also used by UI timer; qualifiers prevent an old timer from expiring a newer session. */
    @Synchronized
    fun remainingSessionMillis(expectedGeneration: Int): Long {
        if (expectedGeneration != generation) return 0
        val now = clock()
        return sessionAt(now)?.let { it.expiresAtEpochMillis - now } ?: 0
    }

    fun login(username: String, password: String): ApiResult<Session> {
        val scope = synchronized(this) {
            // Every attempt supersedes any previous in-flight login and scope.
            clearIdentity()
            if (localClearFailed) return localCleanupFailure()
            beginRequest()
        }
        val result = try {
            api.login(username, password)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            ApiResult.Cancelled
        } catch (_: Exception) {
            ApiResult.Failure(0, "LOGIN_UNAVAILABLE", "暂时无法登录，请稍后重试。", null)
        }
        var discardToken: String? = null
        val outcome = synchronized(this) {
            scopes.remove(scope)
            if (!scope.isActive()) {
                discardToken = (result as? ApiResult.Success)?.data?.takeIf { it.hasValidScope() }?.accessToken
                return@synchronized ApiResult.Cancelled
            }
            scope.cancel()
            when (result) {
                is ApiResult.Success -> {
                    val data = result.data
                    if (!data.hasValidScope()) return@synchronized invalidEnvelope()
                    val next = Session(data.accessToken, data.subjectId, data.organizationId,
                        data.storeId, data.expiresAtEpochMillis, username)
                    if (next.expiresAtEpochMillis <= clock()) {
                        discardToken = next.accessToken
                        return@synchronized ApiResult.Failure(
                            0, "LOGIN_EXPIRED", "本次登录会话已过期，请重试。", null,
                        )
                    }
                    try {
                        storage.save(next)
                    } catch (_: Exception) {
                        discardToken = next.accessToken
                        // A storage implementation may have partially written before failing.
                        clearIdentity()
                        return@synchronized if (localClearFailed) localCleanupFailure() else
                            ApiResult.Failure(0, "SESSION_SAVE_FAILED", "本机无法保存会话，请重试。", null)
                    }
                    session = next
                    capabilityState = CapabilityState(Capabilities.DEFAULT_CAPABILITIES, "v1")
                    _status.value = status()
                    ApiResult.Success(next)
                }
                is ApiResult.Failure -> result
                ApiResult.Cancelled -> ApiResult.Cancelled
            }
        }
        // Never hold the session monitor during remote compensation. Do not revoke a current token.
        discardToken?.takeIf { token -> currentSession()?.accessToken != token }?.let(::revokeToken)
        return outcome
    }

    /** Invalidate all local presentation and persisted restore permission before remote work. */
    @Synchronized
    fun logoutLocal(): String? {
        val token = session?.accessToken ?: tokenForLogoutRetry
        clearIdentity()
        tokenForLogoutRetry = token.takeIf { localClearFailed }
        return token
    }

    @Synchronized
    internal fun beginLogout(expectedGeneration: Int): LogoutTicket? {
        if (generation != expectedGeneration) return null
        val token = logoutLocal()
        return LogoutTicket(generation, token)
    }

    fun revokeToken(token: String?): ApiResult<Unit> {
        if (token == null) return ApiResult.Success(Unit)
        return try {
            api.logout(token)
        } catch (_: Exception) {
            ApiResult.Failure(0, "LOGOUT_UNREACHABLE", "暂未确认服务端已撤销该会话。", null)
        }
    }

    fun logout(): ApiResult<Unit> {
        val token = logoutLocal()
        val cleanupFailed = localClearFailed
        val remote = revokeToken(token)
        return if (cleanupFailed) localCleanupFailure() else remote
    }

    private fun localCleanupFailure() = ApiResult.Failure(
        0, "SESSION_CLEANUP_FAILED", "未能确认已清除本机登录。", "请重试退出，或等待设备存储恢复。",
    )

    /** A 401 only clears the session when it belongs to the current identity scope. */
    @Synchronized
    fun handleUnauthorized(scope: RequestScope): Boolean {
        if (!scope.isActive()) return false
        clearIdentity()
        return true
    }

    internal fun generation(): Int = generation

    private fun clearIdentity() {
        generation += 1
        scopes.forEach { it.cancel() }
        scopes.clear()
        session = null
        capabilityState = CapabilityState()
        localClearFailed = runCatching { storage.clear() }.isFailure
        if (!localClearFailed) tokenForLogoutRetry = null
        notifyScopeChanged()
    }

    private fun notifyScopeChanged() {
        _status.value = status()
        runCatching { onScopeChanged() }
        scopeListeners.toList().forEach { runCatching { it() } }
    }

    private fun loadValidSession(): Session? {
        val stored = runCatching { storage.load() }.getOrNull() ?: return null
        if (stored.expiresAtEpochMillis <= clock() ||
            !hasValidLoginScope(stored.accessToken, stored.subjectId, stored.organizationId, stored.storeId)) {
            try { storage.clear() } catch (_: Exception) { localClearFailed = true }
            return null
        }
        return stored
    }
}

internal class LogoutTicket(val generation: Int, val token: String?)
data class LogoutOutcome(val generation: Int, val remoteUnconfirmed: Boolean)

/**
 * Scope is owned by the retained activity session coordinator, not by the
 * login/signed-in composition. Only the in-flight job captures the old token.
 * UI sees a generation-tagged outcome and ignores it after a fresh login.
 */
class SessionLogoutCoordinator(
    private val repository: SessionRepository,
    private val ownerScope: CoroutineScope,
    private val dispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    private val _outcome = MutableStateFlow<LogoutOutcome?>(null)
    val outcome: StateFlow<LogoutOutcome?> = _outcome.asStateFlow()

    fun request(expectedGeneration: Int) {
        val request = repository.beginLogout(expectedGeneration) ?: return
        if (request.token == null) return
        ownerScope.launch(dispatcher) {
            val incomplete = repository.revokeToken(request.token) !is ApiResult.Success
            // Different requests may finish out of order. Never evict a newer outcome.
            _outcome.update { previous ->
                if (previous != null && previous.generation > request.generation) previous
                else LogoutOutcome(request.generation, incomplete)
            }
        }
    }
}
