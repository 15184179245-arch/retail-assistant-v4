package com.retail.smartops.core.auth

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.LoginApi
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

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

/** AES/GCM at rest in app-private prefs; the key never leaves Android Keystore. */
class SecureSessionStorage(context: Context) : SessionStorage {
    private val preferences: SharedPreferences =
        context.getSharedPreferences("real_session", Context.MODE_PRIVATE)

    override fun save(session: Session) {
        val plain = listOf(
            session.accessToken, session.subjectId, session.organizationId,
            session.storeId, session.expiresAtEpochMillis.toString(), session.username,
        ).joinToString(SEPARATOR)
        require(plain.split(SEPARATOR).size == 6) { "session fields must not contain the separator" }
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
            init(Cipher.ENCRYPT_MODE, key())
        }
        val encrypted = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        val payload = Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + "." +
            Base64.encodeToString(encrypted, Base64.NO_WRAP)
        check(preferences.edit().putString(KEY, payload).commit()) { "cannot persist session" }
    }

    override fun load(): Session? = try {
        val payload = preferences.getString(KEY, null) ?: return null
        val parts = payload.split(".", limit = 2)
        require(parts.size == 2) { "invalid session payload" }
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
            init(Cipher.DECRYPT_MODE, key(),
                GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)))
        }
        val fields = String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)),
            Charsets.UTF_8).split(SEPARATOR)
        require(fields.size == 6) { "invalid session fields" }
        Session(
            accessToken = fields[0], subjectId = fields[1], organizationId = fields[2],
            storeId = fields[3], expiresAtEpochMillis = fields[4].toLong(), username = fields[5],
        )
    } catch (error: Exception) {
        preferences.edit().remove(KEY).commit()
        null
    }

    override fun clear() {
        // Invalidate the decryption key first. A later prefs failure cannot revive the token.
        val keyRemoved = runCatching {
            KeyStore.getInstance(KEYSTORE).apply { load(null); deleteEntry(ALIAS) }
        }.isSuccess
        val payloadRemoved = runCatching { preferences.edit().remove(KEY).commit() }.getOrDefault(false)
        check(keyRemoved || payloadRemoved) { "cannot invalidate local session" }
    }

    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).apply {
            init(
                KeyGenParameterSpec.Builder(ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build(),
            )
        }.generateKey()
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "retail.real.session"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val KEY = "encrypted_session"
        const val SEPARATOR = "\u001F"
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
    @Volatile private var generation = 0
    @Volatile var localClearFailed = false
        private set
    private var session: Session? = loadValidSession()
    private val scopes = linkedSetOf<RequestScope>()
    private val scopeListeners = linkedSetOf<() -> Unit>()
    private var capabilityState: CapabilityState =
        if (session != null) CapabilityState(Capabilities.DEFAULT_CAPABILITIES, "v1")
        else CapabilityState(emptySet(), "v0")

    @Synchronized
    fun getCapabilities(): Set<String> = capabilityState.capabilities

    @Synchronized
    fun getCapabilityVersion(): String = capabilityState.version

    @Synchronized
    fun hasCapability(capability: String?): Boolean = capabilityState.has(capability)

    @Synchronized
    fun setCapabilities(capabilities: Set<String>, version: String = "v1") {
        capabilityState = CapabilityState(capabilities, version)
        runCatching { onScopeChanged() }
        scopeListeners.toList().forEach { runCatching { it() } }
    }

    @Synchronized
    fun clearCapabilities() {
        capabilityState = CapabilityState(emptySet(), "v0")
        runCatching { onScopeChanged() }
        scopeListeners.toList().forEach { runCatching { it() } }
    }

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
    fun currentSession(): Session? {
        val value = session ?: return null
        if (value.expiresAtEpochMillis <= clock()) {
            clearIdentity()
            return null
        }
        return value
    }

    fun canSubmitFormalAction(): Boolean = currentSession() != null

    @Synchronized
    fun beginRequest(): RequestScope =
        RequestScope(this, generation).also { scopes.add(it) }

    fun login(username: String, password: String): ApiResult<Session> {
        val scope = synchronized(this) {
            clearIdentity()
            beginRequest()
        }
        val result = api.login(username, password)
        return synchronized(this) {
            scopes.remove(scope)
            if (!scope.isActive()) return@synchronized ApiResult.Cancelled
            when (result) {
                is ApiResult.Success -> {
                    val data = result.data
                    val next = Session(data.accessToken, data.subjectId, data.organizationId,
                        data.storeId, data.expiresAtEpochMillis, username)
                    storage.save(next)
                    session = next
                    capabilityState = CapabilityState(Capabilities.DEFAULT_CAPABILITIES, "v1")
                    ApiResult.Success(next)
                }
                is ApiResult.Failure -> result
                ApiResult.Cancelled -> ApiResult.Cancelled
            }
        }
    }

    /** Clear local identity/storage/draft on the caller's (main) thread. */
    @Synchronized
    fun logoutLocal(): String? {
        val token = session?.accessToken
        clearIdentity()
        return token
    }

    /** Best-effort remote revocation; failure never restores the local identity. */
    fun revokeToken(token: String?): ApiResult<Unit> {
        if (token == null) return ApiResult.Success(Unit)
        return try {
            api.logout(token)
        } catch (error: Exception) {
            ApiResult.Failure(0, "NETWORK_ERROR", "已退出本机登录。", null)
        }
    }

    fun logout(): ApiResult<Unit> = revokeToken(logoutLocal())

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
        capabilityState = CapabilityState(emptySet(), "v0")
        try {
            storage.clear()
            localClearFailed = false
        } catch (_: Exception) {
            // The secure store invalidates its encryption key if prefs removal fails.
            // Keep the local identity revoked and allow callers to revoke the server token.
            localClearFailed = true
        }
        runCatching { onScopeChanged() }
        scopeListeners.toList().forEach { runCatching { it() } }
    }

    private fun loadValidSession(): Session? {
        val stored = storage.load() ?: return null
        if (stored.expiresAtEpochMillis <= clock()) {
            try { storage.clear() } catch (_: Exception) { localClearFailed = true }
            return null
        }
        return stored
    }
}
