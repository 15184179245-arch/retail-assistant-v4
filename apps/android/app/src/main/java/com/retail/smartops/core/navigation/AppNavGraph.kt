package com.retail.smartops.core.navigation

import android.os.Handler
import android.os.Looper
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.retail.smartops.BuildConfig
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionLogoutCoordinator
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.auth.SessionStatus
import com.retail.smartops.core.database.CacheIdentity
import com.retail.smartops.core.database.CatalogCacheRepository
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionSseTransport
import com.retail.smartops.core.network.UrlConnectionTransport
import com.retail.smartops.core.network.WorkspaceApiClient
import com.retail.smartops.core.ui.ConnectionBar
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.core.ui.theme.RetailSpacing
import com.retail.smartops.features.agents.AgentChatScreen
import com.retail.smartops.features.agents.AgentRunRepository
import com.retail.smartops.features.auth.LoginScreen
import com.retail.smartops.features.receiving.ReceivingObservationWorkspace
import com.retail.smartops.features.receiving.ReceivingScreen
import com.retail.smartops.features.sales.SalesDraftRepository
import com.retail.smartops.features.sales.SalesScreen
import com.retail.smartops.features.sales.SalesViewModel
import com.retail.smartops.features.search.SearchScreen
import com.retail.smartops.features.search.SearchViewModel
import com.retail.smartops.features.shell.FieldShellController
import com.retail.smartops.features.shell.FieldShellState
import com.retail.smartops.features.shell.FieldWorkObjectSummary
import com.retail.smartops.features.shell.FieldWorkbenchShell
import com.retail.smartops.features.stocktake.FileStocktakeLocalStore
import com.retail.smartops.features.stocktake.StocktakeScreen
import com.retail.smartops.features.stocktake.StocktakeViewModel
import java.io.File
import java.util.concurrent.Executors
import kotlinx.coroutines.delay

internal object AppBuildIdentity {
    const val developmentBanner = "Android 开发版 · C2C-140"
    // Preserved merge assets; Agent, SSE and audited save gates are not verified.
    const val fieldShellEnabled = false
}

@Composable
internal fun AppNavGraph(
    repository: SessionRepository,
    apiClient: HttpApiClient,
    logoutCoordinator: SessionLogoutCoordinator,
    receivingObservations: ReceivingObservationWorkspace,
) {
    val status = repository.status.collectAsState().value
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val logoutOutcome by logoutCoordinator.outcome.collectAsState()
    val logoutNotice = logoutOutcome
        ?.takeIf { it.remoteUnconfirmed && it.generation == status.generation }
        ?.let { "暂未确认服务端已撤销该会话。" }

    DisposableEffect(repository, lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_START || event == Lifecycle.Event.ON_RESUME) {
                repository.currentSession()
            }
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }
    LaunchedEffect(repository, status.generation, status.session) {
        while (status.session != null) {
            val remaining = repository.remainingSessionMillis(status.generation)
            if (remaining <= 0) break
            // Bound the wait as the wall clock, unlike coroutine delay, can change.
            delay(remaining.coerceAtMost(60_000))
        }
    }

    val logout: () -> Unit = { logoutCoordinator.request(status.generation) }

    // Login has no privileged graph to navigate. Each employee generation gets a
    // new graph and screen state, even if logout and login happen before one frame.
    val current = status.session
    if (current == null) {
        LoginScreen(repository, status.localClearFailed, logoutNotice, onRetryLogout = logout)
    } else {
        key(repository.navigationSessionKey, status.generation) {
            SignedInGraph(current, status, repository, apiClient, receivingObservations, logout)
        }
    }
}

@Composable
private fun SignedInGraph(
    current: Session,
    status: SessionStatus,
    repository: SessionRepository,
    apiClient: HttpApiClient,
    receivingObservations: ReceivingObservationWorkspace,
    onLogout: () -> Unit,
) {
    val navController = rememberNavController()
    // Used at action time, not just on the frame where the UI was shown.
    fun stillCurrent(): Boolean = repository.currentSession() == current &&
        repository.status.value.generation == status.generation
    var useFieldShell by remember { mutableStateOf(AppBuildIdentity.fieldShellEnabled) }
    val mainHandler = remember { Handler(Looper.getMainLooper()) }
    val agentGateway = remember(apiClient, repository) { AgentRunRepository(apiClient, repository) }
    val workbench = remember(useFieldShell, apiClient, repository) {
        WorkbenchStartupGate.activate(
            enabled = useFieldShell,
            createWorkspace = {
                WorkspaceApiClient(
                    api = apiClient,
                    sessions = repository,
                    sseTransport = UrlConnectionSseTransport(BuildConfig.API_BASE_URL),
                    baseUrl = BuildConfig.API_BASE_URL,
                )
            },
            createExecutor = { Executors.newSingleThreadExecutor() },
        )
    }
    var shellState by remember { mutableStateOf(FieldShellState(session = repository.currentSession())) }
    val shellController = remember(repository, workbench) {
        FieldShellController(
            sessions = repository,
            workspaceApi = workbench?.workspace,
            onStateChanged = { updated ->
                mainHandler.post { shellState = updated }
            },
        )
    }

    DisposableEffect(shellController, workbench) {
        shellState = shellController.state
        onDispose {
            workbench?.close()
            shellController.close()
        }
    }

    val currentBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = currentBackStackEntry?.destination?.route
    val identity = CacheIdentity.from(current)

    val cacheContext = navController.context.applicationContext
    val searchViewModel = remember(current, status.generation, apiClient, repository, cacheContext) {
        SearchViewModel(
            apiClient = apiClient,
            sessionRepository = repository,
            ownerGeneration = status.generation,
            catalogCacheRepositoryProvider = {
                CatalogCacheRepository(cacheContext, repository, ownerGeneration = status.generation)
            },
        )
    }

    val stocktakePage = remember(workbench, current, apiClient, repository) {
        workbench?.localPage(isCurrent = ::stillCurrent) {
            StocktakeViewModel(
                apiClient = apiClient,
                transport = UrlConnectionTransport(BuildConfig.API_BASE_URL),
                sessionRepository = repository,
                localStore = FileStocktakeLocalStore(File(navController.context.filesDir, "stocktake")),
            )
        }
    }

    val salesViewModel = remember(current, apiClient, repository) {
        val db = runCatching {
            com.retail.smartops.core.database.AppDatabase.open(navController.context, identity)
        }.getOrNull()
        SalesViewModel(
            salesDraftRepository = db?.let { SalesDraftRepository(it) },
            sessionRepository = repository,
            apiClient = apiClient,
            closeOwnedDatabase = db?.let { it::close },
        )
    }

    DisposableEffect(searchViewModel) {
        onDispose { searchViewModel.close() }
    }
    DisposableEffect(salesViewModel) {
        onDispose { salesViewModel.close() }
    }
    DisposableEffect(stocktakePage) {
        onDispose { if (stocktakePage?.isInitialized() == true) stocktakePage.value?.close() }
    }

    val salesUiState by salesViewModel.state.collectAsState()
    LaunchedEffect(salesUiState.syncState, current) {
        shellController.updateUnsyncedLocalDraftCount(if (salesUiState.syncState == "pending_sync") 1 else 0)
    }

    val visibleDestinations = AppDestination.getVisibleBottomNav(status.capabilities::has)

    val navHostContent: @Composable () -> Unit = {
        NavHost(
            navController = navController,
            startDestination = AppDestination.startDestination.route,
        ) {
            composable(AppDestination.CatalogSearch.route) {
                SearchScreen(
                    viewModel = searchViewModel,
                    onUseConfirmedItem = if (AppDestination.SalesDraft in visibleDestinations) {
                        { renderedRevision ->
                            val moved = searchViewModel.transferConfirmedItem(renderedRevision) { selected ->
                                val sales = salesViewModel.state.value
                                if (!stillCurrent() ||
                                    !repository.hasCapability(AppDestination.SalesDraft.requiredCapability) ||
                                    sales.isOpeningDraft || sales.isLoading || sales.submissionLocked
                                ) false
                                else {
                                    salesViewModel.selectConfirmedProduct(selected)
                                    true
                                }
                            }
                            if (!moved || !stillCurrent()) false
                            else {
                                shellController.selectZone(AppDestination.Objects)
                                navController.navigateToDestination(AppDestination.SalesDraft)
                                true
                            }
                        }
                    } else null,
                )
            }
            if (stocktakePage != null) {
                composable(AppDestination.Stocktake.route) {
                    // Continuation files load once, only upon actual stocktake entry.
                    stocktakePage.value?.let { StocktakeScreen(viewModel = it) }
                }
            }
            composable(AppDestination.SalesDraft.route) {
                SalesScreen(
                    viewModel = salesViewModel,
                    onNavigateToSearch = {
                        salesViewModel.clearPendingProduct()
                        searchViewModel.clearConfirmedItem()
                        shellController.selectZone(AppDestination.Objects)
                        navController.navigate(AppDestination.CatalogSearch.route)
                    },
                )
            }
            composable(AppDestination.AgentChat.route) {
                AgentChatScreen(
                    sessions = repository,
                    gateway = agentGateway,
                    onLogout = onLogout,
                    onOpenDraft = {
                        shellController.selectZone(AppDestination.Objects)
                        navController.navigateToDestination(AppDestination.SalesDraft)
                    },
                )
            }
            composable(AppDestination.Receiving.route) {
                val latest by repository.status.collectAsState()
                val form = remember(receivingObservations, current, status.generation, latest.capabilities, latest.generation) {
                    receivingObservations.formFor(current, status.generation)
                }
                if (form != null) ReceivingScreen(form)
                else Text("当前账号不能查看到货观察。")
            }
            if (workbench != null) {
                // C2C-140 parameterized field shell routes
                composable(
                    route = FieldRouteHelper.OBJECT_ROUTE_PATTERN,
                    arguments = listOf(
                        navArgument("kind") { type = NavType.StringType; nullable = true; defaultValue = null },
                        navArgument("id") { type = NavType.StringType; nullable = true; defaultValue = null },
                        navArgument("threadId") { type = NavType.StringType; nullable = true; defaultValue = null },
                        navArgument("artifactId") { type = NavType.StringType; nullable = true; defaultValue = null },
                        navArgument("scan") { type = NavType.StringType; nullable = true; defaultValue = null },
                    ),
                ) { entry ->
                    val kind = entry.arguments?.getString("kind")
                    val id = entry.arguments?.getString("id")
                    val artifactId = entry.arguments?.getString("artifactId")
                    val scan = entry.arguments?.getString("scan")
                    LaunchedEffect(kind, id, artifactId, scan) {
                        if (!scan.isNullOrBlank()) shellController.injectBarcodeChip(scan)
                        if (!id.isNullOrBlank() || !artifactId.isNullOrBlank()) {
                            shellController.focusWorkObject(
                                FieldWorkObjectSummary(
                                    id = id ?: artifactId.orEmpty(),
                                    kind = kind ?: "quote_draft",
                                    title = "工作对象 #${(id ?: artifactId).orEmpty().take(8)}",
                                    statusLabel = "进行中",
                                    statusKind = StatusKind.FACT,
                                    artifactId = artifactId,
                                ),
                            )
                        }
                    }
                    SalesScreen(
                        viewModel = salesViewModel,
                        onNavigateToSearch = { navController.navigate(AppDestination.CatalogSearch.route) },
                    )
                }
            }
        }
    }

    if (workbench != null) {
        FieldWorkbenchShell(
            session = current,
            state = shellState,
            controller = shellController,
            currentNavRoute = currentRoute,
            hasCapability = repository::hasCapability,
            capabilities = repository.getCapabilities(),
            onNavigateObjectRoute = { destination ->
                shellController.selectZone(AppDestination.Objects)
                navController.navigateToDestination(destination)
            },
            onResumeRemoteThread = { threadId ->
                workbench.resumeRemote(isCurrent = ::stillCurrent) {
                    shellController.loadThreadWorkspaceFromBackend(threadId)
                }
            },
            onLogout = onLogout,
            onSwitchLegacyShell = {
                workbench.close()
                useFieldShell = false
            },
            workObjectNavContent = navHostContent,
        )
    } else {
        Scaffold(
            topBar = {
                AppTopBar(
                    session = current,
                    onLogout = onLogout,
                )
            },
            bottomBar = {
                AppBottomBar(
                    currentRoute = currentRoute,
                    visibleDestinations = visibleDestinations,
                    onNavigate = { destination ->
                        navController.navigateToDestination(destination)
                    },
                )
            },
        ) { paddingValues ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(paddingValues),
            ) {
                navHostContent()
            }
        }
    }
}

@Composable
private fun AppTopBar(
    session: Session,
    onLogout: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 8.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "员工 ${session.username}",
                    fontWeight = FontWeight.Bold,
                    style = MaterialTheme.typography.titleMedium,
                )
                Text(
                    text = "${session.organizationId} / ${session.storeId}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            StatusBadge(
                status = StatusKind.DEVELOPMENT,
                modifier = Modifier.padding(end = 8.dp),
            )
            OutlinedButton(
                onClick = onLogout,
                modifier = Modifier.heightIn(min = RetailSpacing.minTouchTarget),
            ) {
                Text("退出")
            }
        }
        Text(
            text = "仅合成数据 · 未连接真实上游",
            color = Color(0xFF8A2600),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold,
        )
        ConnectionBar(
            status = null, // This shell has no trusted connection-health observation.
            modifier = Modifier.padding(top = 4.dp),
        )
    }
}

@Composable
private fun AppBottomBar(
    currentRoute: String?,
    visibleDestinations: List<AppDestination>,
    onNavigate: (AppDestination) -> Unit,
) {
    NavigationBar {
        visibleDestinations.forEach { destination ->
            val selected = currentRoute == destination.route
            NavigationBarItem(
                selected = selected,
                onClick = { onNavigate(destination) },
                icon = {
                    Text(
                        text = when (destination) {
                            AppDestination.CatalogSearch -> "🔍"
                            AppDestination.SalesDraft -> "📝"
                            AppDestination.AgentChat -> "🤖"
                            AppDestination.Receiving -> "📦"
                            else -> "●"
                        },
                    )
                },
                label = {
                    Text(destination.navLabel ?: destination.title)
                },
            )
        }
    }
}
