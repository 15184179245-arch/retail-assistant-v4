package com.retail.smartops.core.navigation

import java.util.concurrent.ExecutorService
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.atomic.AtomicBoolean

/** Owns only resources for the opt-in field workbench, never the shared manual controller. */
internal class WorkbenchStartupGate<Workspace> private constructor(
    val workspace: Workspace,
    private val executor: ExecutorService,
) : AutoCloseable {
    private val open = AtomicBoolean(true)

    /** A UI-thread page is created only on first visit and retained until gate disposal. */
    fun <Page> localPage(isCurrent: () -> Boolean, create: () -> Page): Lazy<Page?> =
        lazy(LazyThreadSafetyMode.NONE) { if (open.get() && isCurrent()) create() else null }

    fun resumeRemote(isCurrent: () -> Boolean, resume: () -> Unit) {
        if (!open.get() || !isCurrent()) return
        try {
            executor.execute {
                if (open.get() && !Thread.currentThread().isInterrupted && isCurrent()) resume()
            }
        } catch (error: RejectedExecutionException) {
            // Disposal may race submission. Other executor failures remain visible.
            if (open.get()) throw error
        }
    }

    override fun close() {
        if (open.getAndSet(false)) executor.shutdownNow()
    }

    companion object {
        fun <Workspace> activate(
            enabled: Boolean,
            createWorkspace: () -> Workspace,
            createExecutor: () -> ExecutorService,
        ): WorkbenchStartupGate<Workspace>? {
            if (!enabled) return null
            return WorkbenchStartupGate(createWorkspace(), createExecutor())
        }
    }
}
