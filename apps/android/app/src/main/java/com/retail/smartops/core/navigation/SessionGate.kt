package com.retail.smartops.core.navigation

import androidx.navigation.NavController
import androidx.navigation.NavGraph.Companion.findStartDestination
import com.retail.smartops.core.auth.Session

/**
 * Gatekeeper logic for session navigation, scope invalidation and backstack protection.
 */
class SessionGateController(
    private var currentSession: Session?,
    private val onNavigate: (route: String, clearBackStack: Boolean) -> Unit,
) {
    fun getSession(): Session? = currentSession

    fun updateSession(next: Session?) {
        val previous = currentSession
        currentSession = next

        if (next == null) {
            // Unauthenticated / Logged out: immediately clear back stack and route to Login
            onNavigate(AppDestination.Login.route, true)
        } else if (previous == null) {
            // Logged in: route to start destination, removing login screen from back stack
            onNavigate(AppDestination.startDestination.route, true)
        } else if (previous.subjectId != next.subjectId ||
            previous.organizationId != next.organizationId ||
            previous.storeId != next.storeId
        ) {
            // Identity / Store switched: clear back stack to prevent cross-person back navigation
            onNavigate(AppDestination.startDestination.route, true)
        }
    }

    fun isAllowed(destination: AppDestination): Boolean {
        if (destination == AppDestination.Login) return true
        return currentSession != null
    }
}

/**
 * Extension helpers for NavController to enforce session-gated navigation and stack cleanup.
 */
fun NavController.navigateToDestination(destination: AppDestination) {
    navigate(destination.route) {
        popUpTo(graph.findStartDestination().id) {
            saveState = true
        }
        launchSingleTop = true
        restoreState = true
    }
}

fun NavController.navigateToLoginAndClearStack() {
    navigate(AppDestination.Login.route) {
        popUpTo(0) { inclusive = true }
        launchSingleTop = true
    }
}

fun NavController.navigateToHomeAndClearLogin() {
    navigate(AppDestination.startDestination.route) {
        popUpTo(AppDestination.Login.route) { inclusive = true }
        launchSingleTop = true
    }
}
