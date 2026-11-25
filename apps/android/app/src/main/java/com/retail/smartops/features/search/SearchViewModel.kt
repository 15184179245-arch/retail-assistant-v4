package com.retail.smartops.features.search

import com.retail.smartops.core.auth.RequestScope
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.CatalogCacheDao
import com.retail.smartops.core.database.CatalogCacheRepository
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import java.net.URLEncoder
import java.util.Locale
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

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
    val sourceSystem: String = "管家婆",
    val sourceVersion: String = "v1",
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
)

class SearchViewModel(
    private val apiClient: HttpApiClient,
    private val sessionRepository: SessionRepository,
    private val catalogCacheRepositoryProvider: (() -> CatalogCacheRepository?)? = null,
    private val directDaoProvider: (() -> CatalogCacheDao?)? = null,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    private val _state = MutableStateFlow(SearchUiState())
    val state: StateFlow<SearchUiState> = _state.asStateFlow()

    private var activeSearchScope: RequestScope? = null

    init {
        sessionRepository.onScopeInvalidated {
            clearOnLogout()
        }
    }

    fun clearOnLogout() {
        activeSearchScope?.cancel()
        activeSearchScope = null
        _state.value = SearchUiState()
    }

    fun onQueryChange(newQuery: String) {
        _state.value = _state.value.copy(
            query = newQuery,
            error = null,
        )
    }

    suspend fun search(queryText: String? = null) {
        activeSearchScope?.cancel()
        activeSearchScope = null

        val text = (queryText ?: _state.value.query).trim()
        if (text.isBlank()) {
            _state.value = SearchUiState(query = _state.value.query)
            return
        }

        if (text.codePointCount(0, text.length) > 200) {
            _state.value = SearchUiState(
                query = _state.value.query,
                error = "搜索关键词不能超过 200 个字符",
            )
            return
        }

        val sessionAndScope = sessionRepository.captureRequest()
        if (sessionAndScope == null) {
            _state.value = _state.value.copy(isLoading = false, error = "员工未登录或会话已过期")
            return
        }
        val (session, scope) = sessionAndScope
        activeSearchScope = scope

        _state.value = _state.value.copy(isLoading = true, error = null)

        try {
            val encodedQuery = URLEncoder.encode(text, "UTF-8")
            val onlineResult = apiClient.authenticatedJson(
                method = "GET",
                path = "/api/v1/search?q=$encodedQuery&limit=20",
                token = session.accessToken,
            )

            // CRITICAL: Scope revocation check. If user switched accounts or logged out while
            // the search was in-flight, discard the late response!
            if (!isCurrentSearch(scope)) {
                return
            }

            when (onlineResult) {
                is ApiResult.Success -> {
                    val data = onlineResult.data
                    val exactList = parseMatches(data["exact"] as? List<*>)
                    val candidateList = parseMatches(data["candidates"] as? List<*>)
                    val ambiguous = data["ambiguous"] as? Boolean ?: false
                    val message = data["message"] as? String ?: ""

                    // Safety gate: if ambiguous or multiple exact matches, force isAmbiguous = true
                    val effectiveAmbiguous = ambiguous || exactList.size > 1

                    _state.value = _state.value.copy(
                        isLoading = false,
                        isOffline = false,
                        isStaleCache = false,
                        cacheSnapshotVersion = null,
                        isAmbiguous = effectiveAmbiguous,
                        exactMatches = exactList,
                        candidateMatches = candidateList,
                        message = message,
                        error = null,
                    )
                }
                is ApiResult.Failure.NetworkError -> {
                    performOfflineSearch(scope, session, text)
                }
                is ApiResult.Failure.Unauthorized -> {
                    sessionRepository.handleUnauthorized(scope)
                    _state.value = _state.value.copy(
                        isLoading = false,
                        error = "登录已失效，请重新登录",
                    )
                }
                is ApiResult.Failure -> {
                    if (onlineResult.status == 0 || onlineResult.status >= 500) {
                        performOfflineSearch(scope, session, text)
                    } else {
                        _state.value = _state.value.copy(
                            isLoading = false,
                            error = onlineResult.userMessage,
                        )
                    }
                }
                is ApiResult.Cancelled -> {
                    // Handled gracefully
                }
            }
        } finally {
            if (activeSearchScope === scope) activeSearchScope = null
            sessionRepository.finishRequest(scope)
        }
    }

    private fun isCurrentSearch(scope: RequestScope): Boolean =
        activeSearchScope === scope && scope.isActive()

    private fun publishSearchState(scope: RequestScope, state: SearchUiState) {
        if (!isCurrentSearch(scope)) return
        _state.value = state
    }

    private fun performOfflineSearch(scope: RequestScope, session: Session, text: String) {
        if (!isCurrentSearch(scope)) return

        try {
            val cacheRepo = catalogCacheRepositoryProvider?.invoke()
            if (cacheRepo != null) {
                val cacheView = try {
                    cacheRepo.lookup(text, 20)
                } catch (e: Exception) {
                    null
                }
                if (cacheView != null) {
                    val items = cacheView.items.map {
                        SearchMatchItem(
                            productId = it.productId,
                            externalCode = it.code,
                            barcode = it.barcode,
                            name = it.name,
                            matchField = "offline",
                            matchType = "offline_cache",
                            matchLabel = "离线缓存",
                            sourceSystem = "离线快照",
                            sourceVersion = it.sourceVersion,
                            isOffline = true,
                        )
                    }
                    val isStale = clock() > cacheView.expiresAtMs
                    val exact = items.filter { it.externalCode.equals(text, ignoreCase = true) || it.barcode == text }
                    val candidates = items.filterNot { it.externalCode.equals(text, ignoreCase = true) || it.barcode == text }
                    val isAmbiguous = exact.size > 1

                    publishSearchState(scope, _state.value.copy(
                        isLoading = false,
                        isOffline = true,
                        isStaleCache = isStale,
                        cacheSnapshotVersion = cacheView.snapshotVersion,
                        isAmbiguous = isAmbiguous,
                        exactMatches = exact,
                        candidateMatches = candidates,
                        message = if (items.isEmpty()) "离线缓存中未找到匹配商品" else "当前处于离线模式，显示本地缓存商品",
                        error = null,
                    ))
                    return
                }
            }

            val directDao = directDaoProvider?.invoke()
            if (directDao != null) {
                val state = directDao.state()
                val version = state?.activeVersion
                if (version != null) {
                    val isStale = clock() > state.activeExpiresAtMs
                    val term = text.trim().lowercase(Locale.ROOT)
                    val items = directDao.search(version, term, 20).map {
                        SearchMatchItem(
                            productId = it.productId,
                            externalCode = it.code,
                            barcode = it.barcode,
                            name = it.name,
                            matchField = "offline",
                            matchType = "offline_cache",
                            matchLabel = "离线缓存",
                            sourceSystem = "离线快照",
                            sourceVersion = it.sourceVersion,
                            isOffline = true,
                        )
                    }
                    val exact = items.filter { it.externalCode.equals(text, ignoreCase = true) || it.barcode == text }
                    val candidates = items.filterNot { it.externalCode.equals(text, ignoreCase = true) || it.barcode == text }
                    val isAmbiguous = exact.size > 1

                    publishSearchState(scope, _state.value.copy(
                        isLoading = false,
                        isOffline = true,
                        isStaleCache = isStale,
                        cacheSnapshotVersion = version,
                        isAmbiguous = isAmbiguous,
                        exactMatches = exact,
                        candidateMatches = candidates,
                        message = if (items.isEmpty()) "离线缓存中未找到匹配商品" else "当前处于离线模式，显示本地缓存商品",
                        error = null,
                    ))
                    return
                }
            }

            publishSearchState(scope, _state.value.copy(
                isLoading = false,
                isOffline = true,
                isStaleCache = false,
                cacheSnapshotVersion = null,
                isAmbiguous = false,
                exactMatches = emptyList(),
                candidateMatches = emptyList(),
                message = "网络不可用且本地暂无有效商品缓存",
                error = "无法连接服务且无可用离线缓存",
            ))
        } catch (e: Exception) {
            publishSearchState(scope, _state.value.copy(
                isLoading = false,
                isOffline = true,
                exactMatches = emptyList(),
                candidateMatches = emptyList(),
                message = "离线缓存读取失败",
                error = e.message ?: "读取离线缓存失败",
            ))
        }
    }

    fun confirmItem(item: SearchMatchItem) {
        _state.value = _state.value.copy(confirmedItem = item)
    }

    fun clearConfirmedItem() {
        _state.value = _state.value.copy(confirmedItem = null)
    }

    private fun parseMatches(rawList: List<*>?): List<SearchMatchItem> {
        if (rawList == null) return emptyList()
        val result = mutableListOf<SearchMatchItem>()
        for (item in rawList) {
            val map = item as? Map<*, *> ?: continue
            val productId = map["product_id"] as? String ?: continue
            val code = map["external_code"] as? String ?: ""
            val barcode = map["barcode"] as? String
            val name = map["name"] as? String ?: "未命名商品"
            val spec = map["spec"] as? String
            val unit = map["unit"] as? String
            val matchField = map["match_field"] as? String ?: "code"
            val matchType = map["match_type"] as? String ?: "exact_code"
            val sourceSystem = map["source_system"] as? String ?: "管家婆"
            val sourceVersion = map["source_version"] as? String ?: "v1"

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
