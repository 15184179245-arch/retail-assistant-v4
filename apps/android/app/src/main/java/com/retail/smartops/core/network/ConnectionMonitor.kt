package com.retail.smartops.core.network

/**
 * High-level connection state for the field client.
 */
enum class ConnectionStatus {
    ONLINE,
    BACKEND_UNREACHABLE,
    MODEL_OFFLINE,
    OFFLINE,
}

/**
 * Health evaluation snapshot.
 */
data class SystemHealth(
    val backendReachable: Boolean,
    val modelAvailable: Boolean,
    val lastCheckedTimestamp: Long = System.currentTimeMillis(),
)

/**
 * Observes backend reachability and model availability.
 * Model offline state specifically preserves manual operations (scan, search, manual draft).
 */
class ConnectionMonitor(
    private var currentStatus: ConnectionStatus = ConnectionStatus.ONLINE,
) {
    val status: ConnectionStatus get() = currentStatus

    fun updateStatus(status: ConnectionStatus) {
        currentStatus = status
    }

    fun evaluateHealth(health: SystemHealth): ConnectionStatus {
        currentStatus = when {
            !health.backendReachable -> ConnectionStatus.BACKEND_UNREACHABLE
            !health.modelAvailable -> ConnectionStatus.MODEL_OFFLINE
            else -> ConnectionStatus.ONLINE
        }
        return currentStatus
    }

    fun isManualDraftPermitted(): Boolean =
        currentStatus == ConnectionStatus.ONLINE ||
            currentStatus == ConnectionStatus.MODEL_OFFLINE ||
            currentStatus == ConnectionStatus.OFFLINE ||
            currentStatus == ConnectionStatus.BACKEND_UNREACHABLE

    fun isAgentPermitted(): Boolean =
        currentStatus == ConnectionStatus.ONLINE
}
