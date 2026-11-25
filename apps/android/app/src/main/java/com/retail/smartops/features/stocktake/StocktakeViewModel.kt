package com.retail.smartops.features.stocktake

import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.network.ApiResult
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.HttpTransport
import java.io.File
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject

data class StocktakeSessionUi(
    val sessionId: String,
    val coverageKind: String = "partial_area", // partial_area, partial_shelf, unknown
    val coverageLocationId: String? = null,
    val coverageNote: String,
    val version: Int = 1,
    val isOffline: Boolean = false,
)

data class StocktakeObservationUi(
    val clientEventId: String = UUID.randomUUID().toString(),
    val idempotencyKey: String,
    val productId: String? = null,
    val unknownProductLabel: String? = null,
    val productName: String,
    val locationId: String? = null,
    val unknownLocationLabel: String? = null,
    val locationName: String,
    val quantity: String,
    val unit: String = "piece", // piece, box, kg, meter
    val photoEvidence: PhotoEvidence? = null,
    val observedAtMs: Long,
    val syncState: String = "pending", // pending, synced, failed
    val serverObservationId: String? = null,
    val lastError: String? = null,
)

interface StocktakeLocalStore {
    fun saveSession(scopeDigest: String, session: StocktakeSessionUi?)
    fun loadSession(scopeDigest: String): StocktakeSessionUi?
    fun saveObservations(scopeDigest: String, observations: List<StocktakeObservationUi>)
    fun loadObservations(scopeDigest: String): List<StocktakeObservationUi>
    fun clear(scopeDigest: String)
}

class InMemoryStocktakeLocalStore : StocktakeLocalStore {
    private val sessions = mutableMapOf<String, StocktakeSessionUi?>()
    private val observations = mutableMapOf<String, List<StocktakeObservationUi>>()

    override fun saveSession(scopeDigest: String, session: StocktakeSessionUi?) {
        sessions[scopeDigest] = session
    }

    override fun loadSession(scopeDigest: String): StocktakeSessionUi? =
        sessions[scopeDigest]

    override fun saveObservations(scopeDigest: String, observations: List<StocktakeObservationUi>) {
        this.observations[scopeDigest] = observations
    }

    override fun loadObservations(scopeDigest: String): List<StocktakeObservationUi> =
        observations[scopeDigest] ?: emptyList()

    override fun clear(scopeDigest: String) {
        sessions.remove(scopeDigest)
        observations.remove(scopeDigest)
    }
}

class FileStocktakeLocalStore(private val baseDir: File) : StocktakeLocalStore {
    init {
        if (!baseDir.exists()) baseDir.mkdirs()
    }

    private fun sessionFile(scopeDigest: String) = File(baseDir, "stocktake_session_$scopeDigest.json")
    private fun obsFile(scopeDigest: String) = File(baseDir, "stocktake_obs_$scopeDigest.json")

    override fun saveSession(scopeDigest: String, session: StocktakeSessionUi?) {
        val file = sessionFile(scopeDigest)
        if (session == null) {
            if (file.exists()) file.delete()
            return
        }
        val json = JSONObject().apply {
            put("sessionId", session.sessionId)
            put("coverageKind", session.coverageKind)
            put("coverageLocationId", session.coverageLocationId)
            put("coverageNote", session.coverageNote)
            put("version", session.version)
            put("isOffline", session.isOffline)
        }
        file.writeText(json.toString())
    }

    override fun loadSession(scopeDigest: String): StocktakeSessionUi? {
        val file = sessionFile(scopeDigest)
        if (!file.exists()) return null
        return try {
            val json = JSONObject(file.readText())
            StocktakeSessionUi(
                sessionId = json.getString("sessionId"),
                coverageKind = json.optString("coverageKind", "partial_area"),
                coverageLocationId = if (json.isNull("coverageLocationId")) null else json.optString("coverageLocationId"),
                coverageNote = json.getString("coverageNote"),
                version = json.optInt("version", 1),
                isOffline = json.optBoolean("isOffline", false),
            )
        } catch (_: Exception) {
            null
        }
    }

    override fun saveObservations(scopeDigest: String, observations: List<StocktakeObservationUi>) {
        val file = obsFile(scopeDigest)
        val array = JSONArray()
        for (obs in observations) {
            val obj = JSONObject().apply {
                put("clientEventId", obs.clientEventId)
                put("idempotencyKey", obs.idempotencyKey)
                put("productId", obs.productId)
                put("unknownProductLabel", obs.unknownProductLabel)
                put("productName", obs.productName)
                put("locationId", obs.locationId)
                put("unknownLocationLabel", obs.unknownLocationLabel)
                put("locationName", obs.locationName)
                put("quantity", obs.quantity)
                put("unit", obs.unit)
                put("observedAtMs", obs.observedAtMs)
                put("syncState", obs.syncState)
                put("serverObservationId", obs.serverObservationId)
                put("lastError", obs.lastError)
                if (obs.photoEvidence != null) {
                    val p = JSONObject().apply {
                        put("photoId", obs.photoEvidence.photoId)
                        put("localPath", obs.photoEvidence.localPath)
                        put("sha256", obs.photoEvidence.sha256)
                        put("sizeBytes", obs.photoEvidence.sizeBytes)
                        put("capturedAtMs", obs.photoEvidence.capturedAtMs)
                        put("remoteMediaId", obs.photoEvidence.remoteMediaId)
                        put("uploadState", obs.photoEvidence.uploadState)
                        put("lastError", obs.photoEvidence.lastError)
                    }
                    put("photoEvidence", p)
                }
            }
            array.put(obj)
        }
        file.writeText(array.toString())
    }

    override fun loadObservations(scopeDigest: String): List<StocktakeObservationUi> {
        val file = obsFile(scopeDigest)
        if (!file.exists()) return emptyList()
        return try {
            val array = JSONArray(file.readText())
            val list = mutableListOf<StocktakeObservationUi>()
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i)
                val photoObj = obj.optJSONObject("photoEvidence")
                val photo = if (photoObj != null) {
                    PhotoEvidence(
                        photoId = photoObj.getString("photoId"),
                        localPath = photoObj.getString("localPath"),
                        sha256 = photoObj.getString("sha256"),
                        sizeBytes = photoObj.getLong("sizeBytes"),
                        capturedAtMs = photoObj.getLong("capturedAtMs"),
                        remoteMediaId = if (photoObj.isNull("remoteMediaId")) null else photoObj.optString("remoteMediaId"),
                        uploadState = photoObj.optString("uploadState", "local_only"),
                        lastError = if (photoObj.isNull("lastError")) null else photoObj.optString("lastError"),
                    )
                } else null

                list.add(
                    StocktakeObservationUi(
                        clientEventId = obj.getString("clientEventId"),
                        idempotencyKey = obj.getString("idempotencyKey"),
                        productId = if (obj.isNull("productId")) null else obj.optString("productId"),
                        unknownProductLabel = if (obj.isNull("unknownProductLabel")) null else obj.optString("unknownProductLabel"),
                        productName = obj.getString("productName"),
                        locationId = if (obj.isNull("locationId")) null else obj.optString("locationId"),
                        unknownLocationLabel = if (obj.isNull("unknownLocationLabel")) null else obj.optString("unknownLocationLabel"),
                        locationName = obj.getString("locationName"),
                        quantity = obj.getString("quantity"),
                        unit = obj.getString("unit"),
                        photoEvidence = photo,
                        observedAtMs = obj.getLong("observedAtMs"),
                        syncState = obj.optString("syncState", "pending"),
                        serverObservationId = if (obj.isNull("serverObservationId")) null else obj.optString("serverObservationId"),
                        lastError = if (obj.isNull("lastError")) null else obj.optString("lastError"),
                    )
                )
            }
            list
        } catch (_: Exception) {
            emptyList()
        }
    }

    override fun clear(scopeDigest: String) {
        sessionFile(scopeDigest).delete()
        obsFile(scopeDigest).delete()
    }
}

data class StocktakeUiState(
    val session: StocktakeSessionUi? = null,
    val observations: List<StocktakeObservationUi> = emptyList(),
    val isOffline: Boolean = false,
    val isSyncing: Boolean = false,
    val selectedLocationId: String? = null,
    val unknownLocationLabel: String = "",
    val activeQuantityInput: String = "1",
    val activeUnit: String = "piece",
    val pendingPhoto: PhotoEvidence? = null,
    val debounceWarning: String? = null,
    val message: String = "",
    val error: String? = null,
)

class StocktakeViewModel(
    private val apiClient: HttpApiClient,
    private val transport: HttpTransport,
    private val sessionRepository: SessionRepository,
    private val localStore: StocktakeLocalStore = InMemoryStocktakeLocalStore(),
    private val photoManager: PhotoEvidenceManager = PhotoEvidenceManager(),
    private val clock: () -> Long = System::currentTimeMillis,
) {
    private val _state = MutableStateFlow(StocktakeUiState())
    val state: StateFlow<StocktakeUiState> = _state.asStateFlow()

    // Anti-double-count debounce trackers
    private var lastScannedProductKey: String? = null
    private var lastScannedTimestampMs: Long = 0L
    private val DEBOUNCE_WINDOW_MS = 1500L

    private val unsubscribeScope = sessionRepository.onScopeInvalidated(::clearOnLogout)

    fun close() { unsubscribeScope() }

    init {
        restoreFromBreakpoint()
    }

    private fun currentScope(): CacheIdentity? {
        val s = sessionRepository.currentSession() ?: return null
        return CacheIdentity.from(s)
    }

    fun clearOnLogout() {
        _state.value = StocktakeUiState()
    }

    /**
     * Breakpoint recovery (续盘): loads saved session and un-synced observations.
     */
    fun restoreFromBreakpoint() {
        val scope = currentScope() ?: return
        val savedSession = localStore.loadSession(scope.digest())
        val savedObs = localStore.loadObservations(scope.digest())
        if (savedSession != null || savedObs.isNotEmpty()) {
            val pendingCount = savedObs.count { it.syncState != "synced" }
            _state.value = _state.value.copy(
                session = savedSession,
                observations = savedObs,
                message = if (savedSession != null) "已恢复现场盘点断点 (待同步: $pendingCount 项)" else "",
            )
        }
    }

    suspend fun startSession(
        coverageKind: String,
        coverageLocationId: String?,
        coverageNote: String,
    ) {
        val scope = currentScope()
        if (scope == null) {
            _state.value = _state.value.copy(error = "员工未登录或会话已过期")
            return
        }

        val session = sessionRepository.currentSession()
        val idempotencyKey = "stk-session-${UUID.randomUUID()}"
        val body = mapOf(
            "coverage_kind" to coverageKind,
            "coverage_location_id" to coverageLocationId,
            "coverage_note" to coverageNote,
        )

        _state.value = _state.value.copy(isSyncing = true, error = null)

        val result = if (session != null) {
            apiClient.authenticatedJson(
                method = "POST",
                path = "/api/v1/stocktake/sessions",
                token = session.accessToken,
                body = body,
                headers = mapOf("Idempotency-Key" to idempotencyKey),
            )
        } else {
            ApiResult.Failure.NetworkError("OFFLINE", "网络不可用", null)
        }

        when (result) {
            is ApiResult.Success -> {
                val data = result.data
                val sessionId = data["id"] as? String ?: UUID.randomUUID().toString()
                val version = (data["version"] as? Number)?.toInt() ?: 1
                val uiSession = StocktakeSessionUi(
                    sessionId = sessionId,
                    coverageKind = coverageKind,
                    coverageLocationId = coverageLocationId,
                    coverageNote = coverageNote,
                    version = version,
                    isOffline = false,
                )
                localStore.saveSession(scope.digest(), uiSession)
                _state.value = _state.value.copy(
                    session = uiSession,
                    isOffline = false,
                    isSyncing = false,
                    message = "盘点会话已创建",
                    error = null,
                )
            }
            else -> {
                // Offline fallback: create local offline session
                val offlineId = "offline-session-${UUID.randomUUID()}"
                val uiSession = StocktakeSessionUi(
                    sessionId = offlineId,
                    coverageKind = coverageKind,
                    coverageLocationId = coverageLocationId,
                    coverageNote = coverageNote,
                    version = 1,
                    isOffline = true,
                )
                localStore.saveSession(scope.digest(), uiSession)
                _state.value = _state.value.copy(
                    session = uiSession,
                    isOffline = true,
                    isSyncing = false,
                    message = "网络离线：已开启本地离线盘点草稿",
                    error = null,
                )
            }
        }
    }

    fun attachPhoto(bytes: ByteArray, filename: String = "evidence.jpg") {
        val evidence = photoManager.createEvidenceFromBytes(bytes, filename)
        _state.value = _state.value.copy(
            pendingPhoto = evidence,
            message = "已拍摄现场证据照片 (SHA-256: ${evidence.sha256.take(8)}...)",
        )
    }

    fun clearPendingPhoto() {
        _state.value = _state.value.copy(pendingPhoto = null)
    }

    /**
     * Record an observation.
     * Guarantees:
     * - Anti-double count: duplicate scan within debounce window is blocked.
     * - Multiple locations: same product in different locations can be recorded.
     * - Photo failure tolerance: photo upload failure keeps the observation intact.
     */
    suspend fun recordObservation(
        productId: String?,
        unknownProductLabel: String?,
        productName: String,
        locationId: String?,
        unknownLocationLabel: String?,
        locationName: String,
        quantity: String,
        unit: String,
        photo: PhotoEvidence? = null,
    ): Boolean {
        val scope = currentScope()
        if (scope == null) {
            _state.value = _state.value.copy(error = "员工未登录")
            return false
        }
        val currentSession = _state.value.session
        if (currentSession == null) {
            _state.value = _state.value.copy(error = "请先开启或进入盘点会话")
            return false
        }

        // 1. Anti-double count debounce check
        val dedupeKey = "${productId ?: unknownProductLabel}@${locationId ?: unknownLocationLabel}"
        val now = clock()
        if (dedupeKey == lastScannedProductKey && (now - lastScannedTimestampMs) < DEBOUNCE_WINDOW_MS) {
            _state.value = _state.value.copy(
                debounceWarning = "检测到短时间内重复扫码同一个商品/货位！已防抖拦截，请在行内修改数量。",
            )
            return false
        }
        lastScannedProductKey = dedupeKey
        lastScannedTimestampMs = now

        val idempotencyKey = "obs-${UUID.randomUUID()}"
        val activePhoto = photo ?: _state.value.pendingPhoto

        val observation = StocktakeObservationUi(
            clientEventId = UUID.randomUUID().toString(),
            idempotencyKey = idempotencyKey,
            productId = productId,
            unknownProductLabel = unknownProductLabel,
            productName = productName,
            locationId = locationId,
            unknownLocationLabel = unknownLocationLabel,
            locationName = locationName,
            quantity = quantity,
            unit = unit,
            photoEvidence = activePhoto,
            observedAtMs = now,
            syncState = "pending",
        )

        // Save locally first (never lose data)
        val updatedList = _state.value.observations + observation
        localStore.saveObservations(scope.digest(), updatedList)

        _state.value = _state.value.copy(
            observations = updatedList,
            pendingPhoto = null,
            debounceWarning = null,
            message = "已记录第 ${updatedList.size} 项观察: $productName x $quantity $unit",
        )

        // If online and session is online, attempt direct sync
        val sessionUser = sessionRepository.currentSession()
        if (!currentSession.isOffline && sessionUser != null) {
            syncSingleObservation(observation, sessionUser)
        }

        return true
    }

    private suspend fun syncSingleObservation(obs: StocktakeObservationUi, session: Session) {
        val currentSession = _state.value.session ?: return
        val scope = currentScope() ?: return

        // 1. Photo upload (with failure tolerance)
        var updatedPhoto = obs.photoEvidence
        if (updatedPhoto != null && updatedPhoto.remoteMediaId == null) {
            updatedPhoto = photoManager.uploadPhoto(updatedPhoto, transport, session.accessToken)
        }

        // 2. Submit observation
        val isoObservedAt = DateTimeFormatter.ISO_INSTANT.format(Instant.ofEpochMilli(obs.observedAtMs).atOffset(ZoneOffset.UTC))
        val body = mutableMapOf<String, Any?>(
            "expected_session_version" to currentSession.version,
            "kind" to "observed",
            "quantity" to (obs.quantity.toDoubleOrNull() ?: 1.0),
            "unit" to obs.unit,
            "observed_at" to isoObservedAt,
        )
        if (obs.productId != null) body["product_id"] = obs.productId
        else body["unknown_product_label"] = obs.unknownProductLabel

        if (obs.locationId != null) body["location_id"] = obs.locationId
        else body["unknown_location_label"] = obs.unknownLocationLabel

        if (updatedPhoto?.remoteMediaId != null) {
            body["media_id"] = updatedPhoto.remoteMediaId
        }

        val result = apiClient.authenticatedJson(
            method = "POST",
            path = "/api/v1/stocktake/sessions/${currentSession.sessionId}/observations",
            token = session.accessToken,
            body = body,
            headers = mapOf("Idempotency-Key" to obs.idempotencyKey),
        )

        val updatedObs = when (result) {
            is ApiResult.Success -> {
                val serverId = result.data["id"] as? String
                obs.copy(
                    syncState = "synced",
                    serverObservationId = serverId,
                    photoEvidence = updatedPhoto,
                    lastError = null,
                )
            }
            is ApiResult.Failure -> {
                obs.copy(
                    syncState = "failed",
                    photoEvidence = updatedPhoto,
                    lastError = result.userMessage,
                )
            }
            else -> {
                obs.copy(
                    syncState = "pending",
                    photoEvidence = updatedPhoto,
                )
            }
        }

        val nextList = _state.value.observations.map {
            if (it.clientEventId == obs.clientEventId) updatedObs else it
        }
        localStore.saveObservations(scope.digest(), nextList)
        _state.value = _state.value.copy(observations = nextList)
    }

    suspend fun syncPendingObservations() {
        val scope = currentScope() ?: return
        val currentSession = _state.value.session
        val sessionUser = sessionRepository.currentSession()

        if (currentSession == null || sessionUser == null) {
            _state.value = _state.value.copy(error = "未登录或无盘点会话")
            return
        }

        _state.value = _state.value.copy(isSyncing = true, error = null)

        val pending = _state.value.observations.filter { it.syncState != "synced" }
        for (obs in pending) {
            syncSingleObservation(obs, sessionUser)
        }

        _state.value = _state.value.copy(
            isSyncing = false,
            message = "同步完成，已成功提交盘点观察",
        )
    }
}
