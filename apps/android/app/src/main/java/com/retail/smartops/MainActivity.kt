package com.retail.smartops

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.navigation.compose.rememberNavController
import com.retail.smartops.core.auth.SecureSessionStorage
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.navigation.AppNavGraph
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionTransport
import com.retail.smartops.core.ui.theme.RetailTheme
import com.retail.smartops.features.agents.PreferencesAgentRunLocatorStore

object BuildIdentity {
    const val developmentBanner = "Android 开发版 · C2C-010"
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val draftStore = LocalDraftStore(applicationContext)
        // The Agent run locator is application-level state, not page state. A cold start on the
        // draft page (or any session where the Agent page was never composed) still has to drop
        // the previous identity's locator, so it cannot be restored by a later logout/re-login.
        val agentLocators = PreferencesAgentRunLocatorStore(applicationContext)
        // One transport-backed client is shared by login, catalog search, draft workspace and
        // the Agent run gateway; no separate unauthenticated channels are opened.
        val apiClient = HttpApiClient(UrlConnectionTransport(BuildConfig.API_BASE_URL))
        val repository = SessionRepository(
            api = apiClient,
            storage = SecureSessionStorage(applicationContext),
            onScopeChanged = {
                // Both clears are attempted: a draft failure must never leave the previous
                // identity's Agent locator somewhere a re-login could restore it from.
                try {
                    draftStore.clear()
                } finally {
                    runCatching { agentLocators.clear() }
                }
            },
        )

        setContent {
            RetailTheme {
                val navController = rememberNavController()
                AppNavGraph(
                    navController = navController,
                    repository = repository,
                    apiClient = apiClient,
                    draftStore = draftStore,
                )
            }
        }
    }
}
