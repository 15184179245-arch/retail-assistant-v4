package com.retail.smartops.core.database

import android.content.Context
import com.retail.smartops.core.auth.RequestScope
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import java.security.MessageDigest
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean

class CacheUnavailable(message: String) : IllegalStateException(message)

data class CacheIdentity(val organizationId: String, val storeId: String, val subjectId: String) {
    init {
        val id = Regex("[A-Za-z0-9_.:-]{1,120}")
        require(listOf(organizationId, storeId, subjectId).all { id.matches(it) }) {
            "invalid scope identity"
        }
    }

    fun digest(): String = MessageDigest.getInstance("SHA-256")
        .digest(listOf(organizationId, storeId, subjectId).joinToString("\u0000")
            .toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    companion object {
        fun from(session: Session): CacheIdentity =
            CacheIdentity(session.organizationId, session.storeId, session.subjectId)
    }
}

data class CachedCatalogProduct(
    val productId: String,
    val code: String,
    val barcode: String?,
    val name: String,
    val sourceVersion: String,
)

data class CacheView(
    val snapshotVersion: String,
    val observedAtMs: Long,
    val expiresAtMs: Long,
    val items: List<CachedCatalogProduct>,
    /** Caller must keep "offline cache" provenance visible to the employee. */
    val offline: Boolean = true,
)

data class CatalogPageItem(
    val productId: String,
    val code: String,
    val barcode: String?,
    val name: String,
    val sourceVersion: String,
)

/** Pure validation, usable by JVM tests without Android/Room. */
object CacheRules {
    const val MAX_CACHE_AGE_MS = 24L * 60 * 60 * 1000
    const val MAX_PRODUCTS = 100_000
    const val MAX_PAGE_SIZE = 500

    fun validSnapshot(version: String, total: Int, observedAtMs: Long,
                      expiresAtMs: Long, nowMs: Long) {
        require(version.matches(Regex("[A-Za-z0-9_.:-]{1,120}")))
        require(total in 1..MAX_PRODUCTS)
        require(observedAtMs > 0 && observedAtMs <= nowMs + 5L * 60 * 1000)
        require(expiresAtMs > nowMs && expiresAtMs > observedAtMs)
        require(expiresAtMs - observedAtMs <= MAX_CACHE_AGE_MS)
    }

    fun validatePage(items: List<CatalogPageItem>) {
        require(items.isNotEmpty() && items.size <= MAX_PAGE_SIZE)
        require(items.map { it.productId }.distinct().size == items.size)
        items.forEach {
            require(it.productId.matches(Regex("[A-Za-z0-9_.:-]{1,120}")))
            require(it.sourceVersion.matches(Regex("[A-Za-z0-9_.:-]{1,120}")))
            require(it.code.length <= 128 && (it.barcode?.length ?: 0) <= 128)
            require(it.name.isNotBlank() && it.name.length <= 256)
            require((it.code + (it.barcode ?: "") + it.name).none(Char::isISOControl))
        }
    }

    fun normalizeSearch(text: String): String {
        require(text.isNotBlank() && text.length <= 128 && text.none { ch -> Character.isISOControl(ch) })
        return text.trim().lowercase(Locale.ROOT)
    }

    fun canRead(nowMs: Long, sessionExpiryMs: Long, cachedExpiryMs: Long): Boolean =
        nowMs < sessionExpiryMs && nowMs < cachedExpiryMs
}

interface OfflineCatalogLookup : AutoCloseable {
    fun lookup(text: String, limit: Int = 20): CacheView?
}

/**
 * Binding is immutable for one session generation, including an identical re-login.
 * The caller owns this handle and its database and must close it on IO when retired.
 */
class CatalogCacheRepository(
    context: Context,
    private val sessions: SessionRepository,
    private val clock: () -> Long = System::currentTimeMillis,
    database: AppDatabase? = null,
    private val ownerGeneration: Int = sessions.status.value.generation,
) : OfflineCatalogLookup {
    private val owner = synchronized(sessions) {
        sessions.currentSession()?.takeIf { sessions.status.value.generation == ownerGeneration }
    } ?: throw CacheUnavailable("cache_scope_unavailable")
    private val identity = CacheIdentity.from(owner)
    private val database = database ?: AppDatabase.open(context, identity)
    private val closed = AtomicBoolean()

    private fun ensureCurrent(actual: Session, scope: RequestScope) {
        if (closed.get() || scope.generation != ownerGeneration || !scope.isActive() || actual != owner ||
            CacheIdentity.from(actual) != identity || clock() >= actual.expiresAtEpochMillis
        ) throw CacheUnavailable("cache_scope_unavailable")
    }

    private fun <T> guarded(block: (CatalogCacheDao) -> T): T {
        val (actual, scope) = sessions.captureRequest()
            ?: throw CacheUnavailable("login_required")
        try {
            ensureCurrent(actual, scope)
            val result = database.runInTransaction<T> {
                ensureCurrent(actual, scope)
                val value = block(database.catalog())
                ensureCurrent(actual, scope)
                value
            }
            ensureCurrent(actual, scope)
            return result
        } finally {
            sessions.finishRequest(scope)
        }
    }

    fun begin(version: String, expectedTotal: Int, observedAtMs: Long,
              expiresAtMs: Long): Int = guarded { dao ->
        val now = clock()
        CacheRules.validSnapshot(version, expectedTotal, observedAtMs, expiresAtMs, now)
        val state = dao.state()
        if (state != null && state.scopeDigest != identity.digest())
            throw CacheUnavailable("cache_scope_corrupt")
        if (state?.activeVersion == version)
            throw CacheUnavailable("active_snapshot_cannot_be_overwritten")
        if (state?.stagingVersion != null) {
            if (state.stagingVersion == version && state.expectedTotal == expectedTotal &&
                state.stagingObservedAtMs == observedAtMs &&
                state.stagingExpiresAtMs == expiresAtMs &&
                clock() < state.stagingExpiresAtMs
            ) return@guarded state.stagedCount
            throw CacheUnavailable("another_snapshot_in_progress")
        }
        dao.removeVersion(version) // only an abandoned non-active staging version
        dao.putState(CatalogStateEntity(scopeDigest = identity.digest(),
            activeVersion = state?.activeVersion,
            activeObservedAtMs = state?.activeObservedAtMs ?: 0,
            activeExpiresAtMs = state?.activeExpiresAtMs ?: 0,
            stagingVersion = version, stagingObservedAtMs = observedAtMs,
            stagingExpiresAtMs = expiresAtMs,
            stagedCount = 0, expectedTotal = expectedTotal,
            nextCursor = null, lastVerifiedAtMs = state?.lastVerifiedAtMs ?: 0))
        0
    }

    /** One page per offset; stale/duplicate/out-of-order pages fail closed. */
    fun addPage(version: String, expectedOffset: Int,
                rows: List<CatalogPageItem>, nextCursor: String?): Int = guarded { dao ->
        CacheRules.validatePage(rows)
        require(nextCursor == null || nextCursor.length <= 512)
        val state = dao.state() ?: throw CacheUnavailable("no_snapshot_started")
        if (state.scopeDigest != identity.digest() || state.stagingVersion != version)
            throw CacheUnavailable("wrong_staging_scope_or_version")
        if (clock() >= state.stagingExpiresAtMs)
            throw CacheUnavailable("staging_expired")
        if (expectedOffset != state.stagedCount ||
            expectedOffset + rows.size > state.expectedTotal)
            throw CacheUnavailable("staging_watermark_mismatch")
        val entities = rows.map {
            CatalogItemEntity(version, it.productId, it.code, it.barcode, it.name,
                listOf(it.code, it.barcode ?: "", it.name)
                    .joinToString(" ").lowercase(Locale.ROOT), it.sourceVersion)
        }
        dao.insertItems(entities)
        val newCount = state.stagedCount + entities.size
        dao.putState(state.copy(stagedCount = newCount, nextCursor = nextCursor))
        newCount
    }

    /** Nothing is promoted until the full declared batch is durable. */
    fun publish(version: String, observedAtMs: Long, expiresAtMs: Long) = guarded { dao ->
        val state = dao.state() ?: throw CacheUnavailable("no_snapshot_started")
        if (state.scopeDigest != identity.digest() || state.stagingVersion != version ||
            state.stagedCount != state.expectedTotal || dao.count(version) != state.expectedTotal ||
            state.stagingObservedAtMs != observedAtMs || state.stagingExpiresAtMs != expiresAtMs)
            throw CacheUnavailable("incomplete_snapshot")
        CacheRules.validSnapshot(version, state.expectedTotal, observedAtMs,
            expiresAtMs, clock())
        dao.putState(state.copy(activeVersion = version, activeObservedAtMs = observedAtMs,
            activeExpiresAtMs = expiresAtMs, stagingVersion = null,
            stagingObservedAtMs = 0, stagingExpiresAtMs = 0, nextCursor = null,
            stagedCount = 0, expectedTotal = 0, lastVerifiedAtMs = clock()))
        dao.removeOtherVersions(version)
    }

    /** Explicitly discard one unfinished batch; never touches the active snapshot. */
    fun abort(version: String) = guarded { dao ->
        val state = dao.state() ?: throw CacheUnavailable("no_snapshot_started")
        if (state.scopeDigest != identity.digest() || state.stagingVersion != version)
            throw CacheUnavailable("wrong_staging_scope_or_version")
        dao.removeVersion(version)
        dao.putState(state.copy(stagingVersion = null, stagingObservedAtMs = 0,
            stagingExpiresAtMs = 0, stagedCount = 0, expectedTotal = 0,
            nextCursor = null))
    }

    fun resume(): Pair<Int, String?>? = guarded { dao ->
        val state = dao.state() ?: return@guarded null
        if (state.scopeDigest != identity.digest())
            throw CacheUnavailable("cache_scope_corrupt")
        if (state.stagingVersion == null) null else state.stagedCount to state.nextCursor
    }

    override fun lookup(text: String, limit: Int): CacheView? = guarded { dao ->
        require(limit in 1..100)
        val term = CacheRules.normalizeSearch(text)
        val state = dao.state() ?: return@guarded null
        if (state.scopeDigest != identity.digest())
            throw CacheUnavailable("cache_scope_corrupt")
        val version = state.activeVersion ?: return@guarded null
        if (!CacheRules.canRead(clock(), owner.expiresAtEpochMillis, state.activeExpiresAtMs))
            throw CacheUnavailable("cache_expired_or_session_revoked")
        CacheView(version, state.activeObservedAtMs, state.activeExpiresAtMs,
            dao.search(version, term, limit, entered = text.trim()).map {
                CachedCatalogProduct(it.productId, it.code, it.barcode, it.name,
                    it.sourceVersion)
            })
    }

    override fun close() {
        // No session callback or monitor may be held across Room.close().
        if (closed.compareAndSet(false, true)) database.close()
    }
}
