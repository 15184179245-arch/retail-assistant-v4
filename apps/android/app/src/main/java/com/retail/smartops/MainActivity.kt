package com.retail.smartops

import android.app.Application
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.retail.smartops.core.auth.ProcessOnlySessionStorage
import com.retail.smartops.core.auth.SessionLogoutCoordinator
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.auth.retireLegacySessionArtifacts
import com.retail.smartops.core.navigation.AppNavGraph
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionTransport
import com.retail.smartops.core.ui.theme.RetailTheme
import com.retail.smartops.features.agents.PreferencesAgentRunLocatorStore
import com.retail.smartops.features.receiving.ReceivingObservationWorkspace

object BuildIdentity {
    const val developmentBanner = "Android 开发版 · C2C-010"
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val owner = ViewModelProvider(this)[AppSessionOwner::class.java]
        setContent {
            RetailTheme {
                AppNavGraph(
                    repository = owner.repository,
                    apiClient = owner.apiClient,
                    logoutCoordinator = owner.logout,
                    receivingObservations = owner.receivingObservations,
                )
            }
        }
    }
}

/** One session coordinator across activity recreation; no UI or Activity context retained. */
class AppSessionOwner(application: Application) : AndroidViewModel(application) {
    init { retireLegacySessionArtifacts(application) }

    private val agentLocators = PreferencesAgentRunLocatorStore(application)
    val apiClient = HttpApiClient(UrlConnectionTransport(BuildConfig.API_BASE_URL))
    val repository = SessionRepository(
        api = apiClient,
        storage = ProcessOnlySessionStorage,
        onScopeChanged = { agentLocators.clear() },
    )
    val logout = SessionLogoutCoordinator(repository, viewModelScope)
    internal val receivingObservations = ReceivingObservationWorkspace(repository)

    override fun onCleared() {
        receivingObservations.close()
        super.onCleared()
    }
}
