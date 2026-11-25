package com.retail.smartops.features.agents

import android.content.Context
import com.retail.smartops.core.auth.Session
import java.util.UUID

data class AgentIdentity(val organizationId: String, val storeId: String, val subjectId: String) {
    companion object {
        fun from(session: Session) = AgentIdentity(session.organizationId, session.storeId, session.subjectId)
    }
}
data class RunLocator(val identity: AgentIdentity, val runId: String)

interface AgentRunLocatorStore {
    fun load(identity: AgentIdentity): RunLocator?
    /** Must complete durably before a command can be sent. */
    fun save(locator: RunLocator)
    fun clear()
}

class MemoryAgentRunLocatorStore : AgentRunLocatorStore {
    private var value: RunLocator? = null
    @Synchronized override fun load(identity: AgentIdentity) = value?.takeIf { it.identity == identity }
    @Synchronized override fun save(locator: RunLocator) { value = locator }
    @Synchronized override fun clear() { value = null }
}

/** One app-private schema=1 locator; never stores token, question or action parameters. */
class PreferencesAgentRunLocatorStore(context: Context) : AgentRunLocatorStore {
    private val prefs = context.getSharedPreferences("agent_run_locator", Context.MODE_PRIVATE)
    @Synchronized override fun load(identity: AgentIdentity): RunLocator? {
        return try {
            if (!prefs.contains("schema")) return null
            if (prefs.getInt("schema", 0) != 1) { clear(); return null }
            val stored = AgentIdentity(prefs.getString("org", null) ?: error("org"),
                prefs.getString("store", null) ?: error("store"),
                prefs.getString("subject", null) ?: error("subject"))
            val runId = prefs.getString("run", null) ?: error("run")
            UUID.fromString(runId)
            if (stored != identity) { clear(); null } else RunLocator(stored, runId)
        } catch (_: Exception) { clear(); null }
    }
    @Synchronized override fun save(locator: RunLocator) {
        UUID.fromString(locator.runId)
        check(prefs.edit().clear().putInt("schema", 1)
            .putString("org", locator.identity.organizationId)
            .putString("store", locator.identity.storeId)
            .putString("subject", locator.identity.subjectId)
            .putString("run", locator.runId).commit()) { "Cannot persist Agent locator" }
    }
    @Synchronized override fun clear() {
        check(prefs.edit().clear().commit()) { "Cannot clear Agent locator" }
    }
}
