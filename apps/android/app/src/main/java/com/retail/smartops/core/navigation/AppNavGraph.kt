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
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.navArgument
import com.retail.smartops.BuildConfig
import com.retail.smartops.LocalDraftStore
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import com.retail.smartops.core.database.CatalogCacheRepository
import com.retail.smartops.core.network.ConnectionStatus
import com.retail.smartops.core.network.HttpApiClient
import com.retail.smartops.core.network.UrlConnectionSseTransport
import com.retail.smartops.core.network.WorkspaceApiClient
import com.retail.smartops.core.ui.ConnectionBar
import com.retail.smartops.core.ui.StatusBadge
import com.retail.smartops.core.ui.StatusKind
import com.retail.smartops.core.ui.theme.RetailSpacing
import com.retail.smartops.features.agents.AgentChatScreen
import com.retail.smartops.features.agents.AgentRunRepository
import com.retail.smartops.features.auth.LoginScreen
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
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

internal object AppBuildIdentity {
    const val developmentBanner = "Android 开发版 · C2C-140"
    // Preserved merge assets; Agent, SSE and audited save gates are not verified.
    const val fieldShellEnabled = false
}

@Composable
fun AppNavGraph(
    navController: NavHostController,
    repository: SessionRepository,
    apiClient: HttpApiClient,
    draftStore: LocalDraftStore,
) {
    var session by remember { mutableStateOf(repository.currentSession()) }
    var useFieldShell by remember { mutableStateOf(AppBuildIdentity.fieldShellEnabled) }
    val executor: ExecutorService = remember { Executors.newSingleThreadExecutor() }
    val mainHandler = remember { Handler(Looper.getMainLooper()) }
    val agentGateway = remember(apiClient, repository) { AgentRunRepository(apiClient, repository) }
    val workspaceApi = remember(apiClient, repository) {
        WorkspaceApiClient(
            api = apiClient,
            sessions = repository,
            sseTransport = UrlConnectionSseTransport(BuildConfig.API_BASE_URL),
            baseUrl = BuildConfig.API_BASE_URL,
        )
    }
    var shellState by remember { mutableStateOf(FieldShellState(session = repository.currentSession())) }
    val shellController = remember(repository, workspaceApi) {
        FieldShellController(
            sessions = repository,
            workspaceApi = workspaceApi,
            onStateChanged = { updated ->
                mainHandler.post { shellState = updated }
            },
        )
    }

    DisposableEffect(repository, shellController) {
        shellState = shellController.state
        val unsubscribe = repository.onScopeInvalidated {
            shellController.invalidateScope()
            mainHandler.post {
                session = repository.currentSession()
                shellState = shellController.state
                navController.navigateToLoginAndClearStack()
            }
        }
        onDispose {
            unsubscribe()
            shellController.close()
            executor.shutdown()
        }
    }

    val currentBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = currentBackStackEntry?.destination?.route

    val onLogout: () -> Unit = {
        shellController.invalidateScope()
        val token = repository.logoutLocal()
        session = null
        navController.navigateToLoginAndClearStack()
        executor.execute {
            repository.revokeToken(token)
            mainHandler.post {
                session = repository.currentSession()
                shellState = shellController.state
            }
        }
    }

    val current = session
    val searchViewModel = remember(current, apiClient, repository) {
        SearchViewModel(
            apiClient = apiClient,
            sessionRepository = repository,
            catalogCacheRepositoryProvider = {
                current?.let {
                    try {
                        CatalogCacheRepository(navController.context, repository)
                    } catch (e: Exception) {
                        null
                    }
                }
            },
        )
    }

    val stocktakeViewModel = remember(current, apiClient, repository) {
        StocktakeViewModel(
            apiClient = apiClient,
            transport = com.retail.smartops.core.network.UrlConnectionTransport(BuildConfig.API_BASE_URL),
            sessionRepository = repository,
            localStore = FileStocktakeLocalStore(File(navController.context.filesDir, "stocktake")),
        )
    }

    val salesViewModel = remember(current, apiClient, repository) {
        val db = current?.let { s ->
            try {
                val scope = com.retail.smartops.core.database.CacheIdentity(
                    organizationId = s.organizationId,
                    storeId = s.storeId,
                    subjectId = s.subjectId,
                )
                com.retail.smartops.core.database.AppDatabase.open(navController.context, scope)
            } catch (e: Exception) {
                null
            }
        }
        val salesDraftRepo = db?.let { SalesDraftRepository(it) }
        SalesViewModel(
            salesDraftRepository = salesDraftRepo,
            sessionRepository = repository,
            apiClient = apiClient,
        )
    }

    val salesUiState by salesViewModel.state.collectAsState()
    LaunchedEffect(salesUiState.syncState, current) {
        val localCount = runCatching { draftStore.load().size }.getOrDefault(0)
        val pendingCount = if (salesUiState.syncState == "pending_sync") 1 else 0
        shellController.updateUnsyncedLocalDraftCount(localCount + pendingCount)
    }

    if (current == null) {
        LoginScreen(repository) { loggedInSession ->
            session = loggedInSession
            shellController.invalidateScope()
            shellState = shellController.state
            navController.navigateToHomeAndClearLogin()
        }
    } else {
        val visibleDestinations = remember(current, repository.getCapabilityVersion()) {
            AppDestination.getVisibleBottomNav(repository::hasCapability)
        }

        val navHostContent: @Composable () -> Unit = {
            NavHost(
                navController = navController,
                startDestination = AppDestination.startDestination.route,
            ) {
                composable(AppDestination.Login.route) {
                    LoginScreen(repository) { loggedInSession ->
                        session = loggedInSession
                        shellController.invalidateScope()
                        navController.navigateToHomeAndClearLogin()
                    }
                }
                composable(AppDestination.CatalogSearch.route) {
                    SearchScreen(
                        viewModel = searchViewModel,
                        onUseConfirmedItem = if (AppDestination.SalesDraft in visibleDestinations) {
                            {
                                val fresh = repository.currentSession()
                                val searchState = searchViewModel.state.value
                                val selected = searchState.confirmedItem
                                val sameIdentity = fresh != null &&
                                    fresh.subjectId == current.subjectId &&
                                    fresh.organizationId == current.organizationId &&
                                    fresh.storeId == current.storeId &&
                                    fresh.accessToken == current.accessToken
                                if (!sameIdentity || selected == null || searchState.isLoading ||
                                    AppDestination.SalesDraft !in AppDestination.getVisibleBottomNav(repository::hasCapability)) {
                                    false
                                } else {
                                    salesViewModel.selectConfirmedProduct(selected)
                                    searchViewModel.clearConfirmedItem()
                                    shellController.selectZone(AppDestination.Objects)
                                    navController.navigateToDestination(AppDestination.SalesDraft)
                                    true
                                }
                            }
                        } else null,
                    )
                }
                composable(AppDestination.Stocktake.route) {
                    StocktakeScreen(viewModel = stocktakeViewModel)
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
                    ReceivingScreen(
                        scopeKey = "${current.subjectId}@${current.storeId}",
                        onReviewDraft = { draft ->
                            shellController.focusWorkObject(
                                FieldWorkObjectSummary(
                                    id = draft.deliveryNoteSha256.take(12).ifBlank { "receiving" },
                                    kind = "receiving_batch",
                                    title = "到货清点批次",
                                    statusLabel = "待核对",
                                    statusKind = StatusKind.UNVERIFIED,
                                    isComplexBatchReview = draft.lines.size > 5,
                                    batchItemCount = draft.lines.size,
                                    desktopHint = if (draft.lines.size > 5) "去电脑处理" else null,
                                ),
                            )
                        },
                    )
                }
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

        if (useFieldShell) {
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
                    executor.execute {
                        shellController.loadThreadWorkspaceFromBackend(threadId)
                    }
                },
                onLogout = onLogout,
                onSwitchLegacyShell = { useFieldShell = false },
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
            status = ConnectionStatus.ONLINE,
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
