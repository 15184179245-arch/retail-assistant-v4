package com.retail.smartops.features.search

import com.retail.smartops.core.auth.RequestScope
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.CatalogCacheDao
import com.retail.smartops.core.database.OfflineCatalogLookup
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import java.net.URLEncoder
import java.util.Locale
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

data class SearchMatchItem(
    val productId: String,
    val externalCode: String,
    val barcode: String?,
    val name: String,
    val spec: String? = null,
    val unit: String? = null,
    val matchField: String,
    val matchType: String,
    val matchLabel: String,
    val sourceSystem: String,
    val sourceVersion: String,
    val isOffline: Boolean = false,
)

data class SearchUiState(
    val query: String = "",
    val isLoading: Boolean = false,
    val isOffline: Boolean = false,
    val isStaleCache: Boolean = false,
    val cacheSnapshotVersion: String? = null,
    val isAmbiguous: Boolean = false,
    val exactMatches: List<SearchMatchItem> = emptyList(),
    val candidateMatches: List<SearchMatchItem> = emptyList(),
    val confirmedItem: SearchMatchItem? = null,
    val message: String = "",
    val error: String? = null,
    val revision: Long = 0,
    val resultsReady: Boolean = false,
    val hasMore: Boolean = false,
    val scanTruncated: Boolean = false,
    val truncated: Boolean = false,
    val hasUnverifiableRows: Boolean = false,
    val cacheExpiresAtMs: Long? = null,
) {
    val hasIncompleteResults: Boolean get() = hasMore || scanTruncated || truncated
}

class SearchViewModel(
    private val apiClient: HttpApiClient,
    private val sessionRepository: SessionRepository,
    catalogCacheRepositoryProvider: (() -> OfflineCatalogLookup?)? = null,
    private val directDaoProvider: (() -> CatalogCacheDao?)? = null,
    private val clock: () -> Long = System::currentTimeMillis,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val ownerGeneration: Int = sessionRepository.status.value.generation,
) {
    private val _state = MutableStateFlow(SearchUiState())
    val state: StateFlow<SearchUiState> = _state.asStateFlow()

    // Guard the displayed result stamp, request and selection under the same
    // session monitor that invalidates a signed-in graph. Never hold it for I/O.
    private var activeSearchScope: RequestScope? = null
    private var closed = false
    private val offlineCache = catalogCacheRepositoryProvider?.let { SearchCacheOwner(it) }
    private var unsubscribeScope: (() -> Unit)? = null

    init {
        synchronized(sessionRepository) {
            if (isOwnerGraph()) unsubscribeScope = sessionRepository.onScopeInvalidated(::onScopeChanged)
            else close()
        }
    }

    private data class SearchRequest(
        val query: String, val session: Session, val scope: RequestScope, val startingState: SearchUiState,
    ) {
        fun complete() = startingState.copy(isLoading = false)
    }

    private sealed interface Lookup {
        data class View(val state: SearchUiState) : Lookup
        data object Unauthorized : Lookup
    }

    private fun isOwnerGraph(): Boolean =
        !closed && sessionRepository.status.value.generation == ownerGeneration

    fun close() = synchronized(sessionRepository) {
        if (!closed) {
            closed = true
            unsubscribeScope?.invoke()
            unsubscribeScope = null
            invalidateResults()
            offlineCache?.retire()
        }
    }

    private fun onScopeChanged() = synchronized(sessionRepository) {
        if (isOwnerGraph()) invalidateResults() // e.g. new capabilities, same cache owner
        else close()
    }

    private fun invalidateResults(query: String = "", error: String? = null, loading: Boolean = false) {
        activeSearchScope?.cancel()
        activeSearchScope = null
        _state.value = SearchUiState(query = query, error = error, isLoading = loading,
            revision = _state.value.revision + 1)
    }

    fun onQueryChange(newQuery: String): Long = synchronized(sessionRepository) {
        if (isOwnerGraph() && _state.value.query != newQuery) invalidateResults(query = newQuery)
        _state.value.revision
    }

    suspend fun search(queryText: String? = null, expectedRevision: Long? = null) {
        val request = synchronized(sessionRepository) {
            val current = _state.value
            if (!isOwnerGraph() || (expectedRevision != null && current.revision != expectedRevision)) return
            val raw = queryText ?: current.query
            val text = raw.trim()
            if (text.isBlank()) {
                invalidateResults(query = raw)
                return
            }
            if (text.codePointCount(0, text.length) > 200) {
                invalidateResults(query = raw, error = "搜索关键词不能超过 200 个字符")
                return
            }
            val captured = sessionRepository.captureRequest()
            if (captured == null || !isOwnerGraph()) {
                captured?.let { sessionRepository.finishRequest(it.second) }
                if (isOwnerGraph()) invalidateResults(query = raw, error = "员工未登录或会话已过期")
                return
            }
            invalidateResults(query = raw, loading = true)
            activeSearchScope = captured.second
            SearchRequest(text, captured.first, captured.second, _state.value)
        }

        try {
            // Production HTTP and Room entry points are synchronous. Publish only
            // after returning from cancellable IO, not from the blocking worker.
            when (val result = withContext(ioDispatcher) { lookup(request) }) {
                is Lookup.View -> publishSearchState(request.scope, result.state)
                Lookup.Unauthorized -> synchronized(sessionRepository) {
                    if (isCurrentSearch(request.scope)) sessionRepository.handleUnauthorized(request.scope)
                }
                null -> Unit // Superseded before or during IO.
            }
        } catch (cancelled: CancellationException) {
            // A live caller can cancel a search without invalidating its page.
            // An old caller must not stop another search's loading indicator.
            publishSearchState(request.scope, request.complete())
            throw cancelled
        } catch (_: Exception) {
            publishSearchState(request.scope, request.complete().copy(error = "查询暂未完成，请重试。"))
        } finally {
            synchronized(sessionRepository) {
                if (activeSearchScope === request.scope) activeSearchScope = null
                sessionRepository.finishRequest(request.scope)
            }
        }
    }

    private fun isCurrentSearch(scope: RequestScope): Boolean = synchronized(sessionRepository) {
        sessionRepository.currentSession() != null && isOwnerGraph() &&
            activeSearchScope === scope && scope.isActive()
    }

    private suspend fun stillSearching(scope: RequestScope): Boolean {
        currentCoroutineContext().ensureActive()
        return isCurrentSearch(scope)
    }

    private fun publishSearchState(scope: RequestScope, state: SearchUiState) = synchronized(sessionRepository) {
        if (isCurrentSearch(scope)) _state.value = state
    }

    private suspend fun lookup(request: SearchRequest): Lookup? {
        if (!stillSearching(request.scope)) return null
        val encoded = URLEncoder.encode(request.query, "UTF-8")
        val response = apiClient.authenticatedJson("GET", "/api/v1/search?q=$encoded&limit=20", request.session.accessToken)
        if (!stillSearching(request.scope)) return null
        val state = request.complete()
        return when (response) {
            is ApiResult.Success -> onlineResult(state, response.data)
            is ApiResult.Failure.Unauthorized -> Lookup.Unauthorized
            is ApiResult.Failure -> when {
                response.status in 200..299 || (response.status == 0 && response.code == "INVALID_ENVELOPE") ->
                    unverifiableResponse(state)
                response.status == 0 || response.status >= 500 -> lookupOffline(request)?.let { Lookup.View(it) }
                else -> Lookup.View(state.copy(error = response.userMessage))
            }
            ApiResult.Cancelled -> Lookup.View(state)
        }
    }

    private fun unverifiableResponse(state: SearchUiState): Lookup.View =
        Lookup.View(state.copy(error = "服务端返回的商品结果无法核验，请重试。"))

    private fun onlineResult(state: SearchUiState, data: Map<String, Any?>): Lookup.View {
        if (data["query"] !is String) return unverifiableResponse(state)
        val rawExact = data["exact"] as? List<*> ?: return unverifiableResponse(state)
        val rawCandidates = data["candidates"] as? List<*> ?: return unverifiableResponse(state)
        val ambiguous = data["ambiguous"] as? Boolean ?: return unverifiableResponse(state)
        val message = data["message"] as? String ?: return unverifiableResponse(state)
        val exact = parseMatches(rawExact)
        val candidates = parseMatches(rawCandidates)
        val hasUnverifiableRows = exact.size != rawExact.size || candidates.size != rawCandidates.size
        if (hasUnverifiableRows && exact.isEmpty() && candidates.isEmpty()) return unverifiableResponse(state)
        return Lookup.View(state.copy(
            exactMatches = exact,
            candidateMatches = candidates,
            isAmbiguous = ambiguous || exact.size > 1,
            resultsReady = true,
            message = message,
            hasMore = data["has_more"] as? Boolean ?: false,
            scanTruncated = data["scan_truncated"] as? Boolean ?: false,
            truncated = data["truncated"] as? Boolean ?: false,
            hasUnverifiableRows = hasUnverifiableRows,
        ))
    }

    private suspend fun lookupOffline(request: SearchRequest): SearchUiState? {
        if (!stillSearching(request.scope)) return null
        val state = request.complete().copy(isOffline = true)
        val text = request.query
        return try {
            val repository = offlineCache?.get()
            if (!stillSearching(request.scope)) return null
            val cache = try {
                repository?.lookup(text, 20)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                null // A usable scoped fallback can still provide a local read.
            }
            if (!stillSearching(request.scope)) return null
            if (cache != null) {
                return offlineResult(state, text, cache.snapshotVersion, cache.expiresAtMs, cache.items.map {
                    offlineItem(it.productId, it.code, it.barcode, it.name, it.sourceVersion)
                })
            }

            val dao = directDaoProvider?.invoke()
            if (!stillSearching(request.scope)) return null
            if (dao != null) {
                val snapshot = dao.state()
                val version = snapshot?.activeVersion
                if (version != null) {
                    val term = text.lowercase(Locale.ROOT)
                    val items = dao.search(version, term, 20, entered = text).map {
                        offlineItem(it.productId, it.code, it.barcode, it.name, it.sourceVersion)
                    }
                    return offlineResult(state, text, version, snapshot.activeExpiresAtMs, items)
                }
            }
            state.copy(
                message = "网络不可用且本地暂无有效商品缓存",
                error = "无法连接服务且无可用离线缓存",
            )
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            state.copy(message = "离线缓存读取失败", error = "读取离线缓存失败，请重试。")
        }
    }

    private fun offlineResult(
        state: SearchUiState, text: String, version: String, expiresAtMs: Long, items: List<SearchMatchItem>,
    ): SearchUiState {
        val (exact, candidates) = items.partition { it.externalCode.equals(text, ignoreCase = true) || it.barcode == text }
        return state.copy(
            cacheSnapshotVersion = version,
            isStaleCache = clock() >= expiresAtMs,
            cacheExpiresAtMs = expiresAtMs,
            isAmbiguous = exact.size > 1,
            exactMatches = exact,
            candidateMatches = candidates,
            resultsReady = true,
            message = if (items.isEmpty()) "离线缓存中未找到匹配商品" else "当前处于离线模式，显示本地缓存商品",
        )
    }

    private fun offlineItem(productId: String, code: String, barcode: String?, name: String, version: String) =
        SearchMatchItem(
            productId = productId, externalCode = code, barcode = barcode, name = name,
            matchField = "offline", matchType = "offline_cache", matchLabel = "离线缓存",
            sourceSystem = "离线快照", sourceVersion = version, isOffline = true,
        )

    private fun hasMatchingOwner(revision: Long): Boolean =
        sessionRepository.currentSession() != null && isOwnerGraph() && _state.value.revision == revision

    fun confirmItem(item: SearchMatchItem, renderedRevision: Long): Boolean = synchronized(sessionRepository) {
        if (!hasMatchingOwner(renderedRevision)) return@synchronized false
        val current = _state.value
        if (!current.resultsReady || current.isLoading) return@synchronized false
        val displayed = current.exactMatches.firstOrNull { it == item }
            ?: current.candidateMatches.firstOrNull { it == item }
            ?: return@synchronized false
        _state.value = current.copy(confirmedItem = displayed, revision = current.revision + 1)
        true
    }

    /** Move exactly the displayed choice into a local editor once. The callback must not do I/O. */
    fun transferConfirmedItem(renderedRevision: Long, accept: (SearchMatchItem) -> Boolean): Boolean =
        synchronized(sessionRepository) {
            if (!hasMatchingOwner(renderedRevision)) return@synchronized false
            val current = _state.value
            val selected = current.confirmedItem ?: return@synchronized false
            if (!current.resultsReady || current.isLoading || !accept(selected)) return@synchronized false
            _state.value = current.copy(confirmedItem = null, revision = current.revision + 1)
            true
        }

    fun clearConfirmedItem(renderedRevision: Long? = null) = synchronized(sessionRepository) {
        val current = _state.value
        if (isOwnerGraph() && (renderedRevision == null || current.revision == renderedRevision) &&
            current.confirmedItem != null
        ) _state.value = current.copy(confirmedItem = null, revision = current.revision + 1)
    }

    private fun parseMatches(rawList: List<*>): List<SearchMatchItem> {
        val result = mutableListOf<SearchMatchItem>()
        for (item in rawList) {
            val map = item as? Map<*, *> ?: continue
            fun requiredText(key: String) = (map[key] as? String)?.takeIf { it.isNotBlank() }
            val productId = requiredText("product_id") ?: continue
            val code = requiredText("external_code") ?: continue
            val matchField = requiredText("match_field") ?: continue
            val matchType = requiredText("match_type") ?: continue
            val sourceSystem = requiredText("source_system") ?: continue
            val sourceVersion = requiredText("source_version") ?: continue
            if (listOf("name", "spec", "unit", "barcode").any { map[it] != null && map[it] !is String }) continue
            val barcode = map["barcode"] as? String
            val name = map["name"] as? String ?: "未命名商品"
            val spec = map["spec"] as? String
            val unit = map["unit"] as? String

            val label = when (matchType) {
                "exact_code" -> "精确编码"
                "exact_barcode" -> "精确条码"
                "normalized_barcode" -> "标准条码"
                "alias_exact" -> "别名精确"
                "fuzzy_candidate" -> "模糊候选"
                else -> "匹配结果"
            }

            result.add(
                SearchMatchItem(
                    productId = productId,
                    externalCode = code,
                    barcode = barcode,
                    name = name,
                    spec = spec,
                    unit = unit,
                    matchField = matchField,
                    matchType = matchType,
                    matchLabel = label,
                    sourceSystem = sourceSystem,
                    sourceVersion = sourceVersion,
                    isOffline = false,
                )
            )
        }
        return result
    }
}


// Only IO workers take the init mutex. Retirement is an atomic detach and never
// takes that mutex, waits for a factory, acquires Room, or closes under session lock.
private class SearchCacheOwner(private val create: () -> OfflineCatalogLookup?) {
    private sealed interface Slot {
        data object Empty : Slot
        data object Retired : Slot
        data class Open(val cache: OfflineCatalogLookup) : Slot
    }

    private val slot = AtomicReference<Slot>(Slot.Empty)
    private val initializer = Mutex()

    suspend fun get(): OfflineCatalogLookup? = initializer.withLock {
        when (val existing = slot.get()) {
            is Slot.Open -> existing.cache
            Slot.Retired -> null
            Slot.Empty -> {
                val cache = create() ?: return@withLock null // a transient open error is retryable
                if (slot.compareAndSet(Slot.Empty, Slot.Open(cache))) cache
                else {
                    release(cache) // retirement won while this factory was running
                    null
                }
            }
        }
    }

    fun retire() {
        (slot.getAndSet(Slot.Retired) as? Slot.Open)?.cache?.let(::release)
    }

    private fun release(cache: OfflineCatalogLookup) {
        Dispatchers.IO.dispatch(EmptyCoroutineContext) { runCatching(cache::close) }
    }
}
