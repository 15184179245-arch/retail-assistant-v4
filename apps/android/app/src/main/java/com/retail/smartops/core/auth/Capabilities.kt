package com.retail.smartops.core.auth

/**
 * Standard capabilities definition for Android client navigation.
 * Requirements: FE-003, R-034, AND-004.
 */
object Capabilities {
    const val SALES_DRAFT = "sales.draft"
    const val AGENT_RUN = "agent.run"
    const val PURCHASING_READ = "purchasing.read"
    const val INVENTORY_STOCKTAKE = "inventory.stocktake"
    const val TASKS_READ = "tasks.read"
    const val CATALOG_READ = "catalog.read"

    val DEFAULT_CAPABILITIES: Set<String> = setOf(
        SALES_DRAFT,
        AGENT_RUN,
        PURCHASING_READ,
        INVENTORY_STOCKTAKE,
        TASKS_READ,
        CATALOG_READ,
    )
}

data class CapabilityState(
    val capabilities: Set<String> = emptySet(),
    val version: String = "v0",
) {
    fun has(capability: String?): Boolean {
        if (capability == null || capability.isBlank()) return true
        if (capability == "agent.run") {
            return capabilities.contains("agent.run") || capabilities.contains("agents.run")
        }
        if (capability == "receiving.read") {
            return capabilities.contains("receiving.read") || capabilities.contains("erp.receiving.read")
        }
        return capabilities.contains(capability)
    }
}
