package com.retail.smartops.core.navigation

import com.retail.smartops.core.auth.Capabilities
import java.net.URLDecoder
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

/**
 * Typed route arguments for C2C-140 cross-device ThreadWorkspace & Work Object resumption.
 * Uses pure JVM/UTF-8 encoding (per FE-005 ADR: navigation-compose 2.7.7 without kotlinx.serialization plugin).
 */
data class WorkspaceRouteArgs(
    val threadId: String? = null,
    val objectKind: String? = null,
    val objectId: String? = null,
    val artifactId: String? = null,
    val scanChip: String? = null,
) {
    fun hasTargetObject(): Boolean = !objectId.isNullOrBlank() || !artifactId.isNullOrBlank()
}

object FieldRouteHelper {
    const val OBJECT_ROUTE_PATTERN =
        "field/objects?kind={kind}&id={id}&threadId={threadId}&artifactId={artifactId}&scan={scan}"
    const val THREAD_ROUTE_PATTERN =
        "field/threads?threadId={threadId}&kind={kind}&id={id}&scan={scan}"

    private fun enc(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name())

    private fun dec(value: String): String =
        URLDecoder.decode(value, StandardCharsets.UTF_8.name())

    fun buildObjectRoute(args: WorkspaceRouteArgs): String {
        val params = buildList {
            args.objectKind?.takeIf { it.isNotBlank() }?.let { add("kind=${enc(it)}") }
            args.objectId?.takeIf { it.isNotBlank() }?.let { add("id=${enc(it)}") }
            args.threadId?.takeIf { it.isNotBlank() }?.let { add("threadId=${enc(it)}") }
            args.artifactId?.takeIf { it.isNotBlank() }?.let { add("artifactId=${enc(it)}") }
            args.scanChip?.takeIf { it.isNotBlank() }?.let { add("scan=${enc(it)}") }
        }
        return if (params.isEmpty()) AppDestination.Objects.route else "${AppDestination.Objects.route}?${params.joinToString("&")}"
    }

    fun buildThreadRoute(args: WorkspaceRouteArgs): String {
        val params = buildList {
            args.threadId?.takeIf { it.isNotBlank() }?.let { add("threadId=${enc(it)}") }
            args.objectKind?.takeIf { it.isNotBlank() }?.let { add("kind=${enc(it)}") }
            args.objectId?.takeIf { it.isNotBlank() }?.let { add("id=${enc(it)}") }
            args.scanChip?.takeIf { it.isNotBlank() }?.let { add("scan=${enc(it)}") }
        }
        return if (params.isEmpty()) AppDestination.Threads.route else "${AppDestination.Threads.route}?${params.joinToString("&")}"
    }

    fun parseArgs(route: String?): WorkspaceRouteArgs {
        if (route.isNullOrBlank()) return WorkspaceRouteArgs()
        val queryIndex = route.indexOf('?')
        if (queryIndex < 0 || queryIndex == route.lastIndex) return WorkspaceRouteArgs()
        val query = route.substring(queryIndex + 1)
        val map = mutableMapOf<String, String>()
        query.split('&').forEach { pair ->
            val eq = pair.indexOf('=')
            if (eq > 0) {
                val k = dec(pair.substring(0, eq))
                val v = dec(pair.substring(eq + 1))
                if (v.isNotBlank() && !v.startsWith("{")) {
                    map[k] = v
                }
            }
        }
        return WorkspaceRouteArgs(
            threadId = map["threadId"],
            objectKind = map["kind"],
            objectId = map["id"],
            artifactId = map["artifactId"],
            scanChip = map["scan"],
        )
    }

    fun baseRoute(route: String?): String? {
        if (route.isNullOrBlank()) return null
        val qIdx = route.indexOf('?')
        return if (qIdx >= 0) route.substring(0, qIdx) else route
    }
}

/**
 * Destinations for Android Navigation Compose.
 * Preserves legacy `startDestination` and `bottomNavDestinations` contracts while adding
 * C2C-140 5-zone field shell destinations (`Today`, `Objects`, `ScanSpeak`, `Threads`, `Profile`).
 */
sealed class AppDestination(
    val route: String,
    val title: String,
    val navLabel: String? = null,
    val inBottomNav: Boolean = false,
    val requiredCapability: String? = null,
    val inFiveZoneBar: Boolean = false,
) {
    data object Login : AppDestination(
        route = "auth/login",
        title = "员工登录",
    )

    data object CatalogSearch : AppDestination(
        route = "catalog/search",
        title = "商品查询",
        navLabel = "找货查询",
        inBottomNav = false,
        requiredCapability = Capabilities.CATALOG_READ,
    )

    data object Stocktake : AppDestination(
        route = "stocktake/count",
        title = "现场盘点",
        navLabel = "现场盘点",
        inBottomNav = false,
        requiredCapability = Capabilities.INVENTORY_STOCKTAKE,
    )

    data object SalesDraft : AppDestination(
        route = "sales/draft",
        title = "现场开单",
        navLabel = "手工草稿",
        inBottomNav = true,
        requiredCapability = Capabilities.SALES_DRAFT,
    )

    data object AgentChat : AppDestination(
        route = "agent/chat",
        title = "Agent 助手",
        navLabel = "Agent 助手",
        inBottomNav = true,
        requiredCapability = Capabilities.AGENT_RUN,
    )

    data object Receiving : AppDestination(
        route = "receiving/review",
        title = "到货清点",
        navLabel = "到货清点",
        inBottomNav = true,
        requiredCapability = Capabilities.PURCHASING_READ,
    )

    // --- C2C-140 5-zone Field Workbench Shell Destinations ---
    data object Today : AppDestination(
        route = "field/today",
        title = "今日",
        navLabel = "今日",
        inBottomNav = false,
        requiredCapability = null,
        inFiveZoneBar = true,
    )

    data object Objects : AppDestination(
        route = "field/objects",
        title = "工作对象",
        navLabel = "对象",
        inBottomNav = false,
        requiredCapability = null,
        inFiveZoneBar = true,
    )

    data object ScanSpeak : AppDestination(
        route = "field/scan-speak",
        title = "扫码 / 按住说话",
        navLabel = "扫码/说话",
        inBottomNav = false,
        requiredCapability = null,
        inFiveZoneBar = true,
    )

    data object Threads : AppDestination(
        route = "field/threads",
        title = "对话线程",
        navLabel = "线程",
        inBottomNav = false,
        requiredCapability = null,
        inFiveZoneBar = true,
    )

    data object Profile : AppDestination(
        route = "field/profile",
        title = "我的",
        navLabel = "我的",
        inBottomNav = false,
        requiredCapability = null,
        inFiveZoneBar = true,
    )

    companion object {
        // Use lazy initialization on companion properties so accessing any sealed data object subclass first
        // never observes null INSTANCE fields from Kotlin JVM sealed-class <clinit> ordering.
        val startDestination: AppDestination get() = SalesDraft
        val allBottomNavDestinations: List<AppDestination> by lazy { listOf(SalesDraft, AgentChat, Receiving) }
        val bottomNavDestinations: List<AppDestination> get() = allBottomNavDestinations

        // C2C-140 §2.5 five-zone field shell bottom bar
        val fiveZoneDestinations: List<AppDestination> by lazy {
            listOf(
                Today,
                Objects,
                ScanSpeak,
                Threads,
                Profile,
            )
        }

        fun getVisibleBottomNav(hasCapability: (String?) -> Boolean): List<AppDestination> =
            allBottomNavDestinations.filter { dest ->
                dest.inBottomNav && (dest.requiredCapability == null || hasCapability(dest.requiredCapability))
            }

        fun getVisibleFiveZoneNav(hasCapability: (String?) -> Boolean): List<AppDestination> =
            fiveZoneDestinations.filter { dest ->
                dest.inFiveZoneBar && (dest.requiredCapability == null || hasCapability(dest.requiredCapability))
            }

        fun fromRoute(route: String?): AppDestination? = when (FieldRouteHelper.baseRoute(route)) {
            Login.route -> Login
            CatalogSearch.route -> CatalogSearch
            Stocktake.route -> Stocktake
            SalesDraft.route -> SalesDraft
            AgentChat.route -> AgentChat
            Receiving.route -> Receiving
            Today.route -> Today
            Objects.route -> Objects
            ScanSpeak.route -> ScanSpeak
            Threads.route -> Threads
            Profile.route -> Profile
            else -> null
        }
    }
}
