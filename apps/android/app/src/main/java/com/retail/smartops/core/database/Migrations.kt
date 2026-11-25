package com.retail.smartops.core.database

import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * Frozen schema:
 * v1: catalog_cache_state and catalog_cache_items; v1 has no lastVerifiedAtMs.
 * v2: adds lastVerifiedAtMs non-null default 0, preserving all items/cursors.
 * v3: MOB-002 adds local_sales_drafts and sales_sync_intents without touching catalog tables.
 * v4: CLIENT-004 adds a nullable, explicitly verified server draft identity.
 *
 * Never delete/rename the database after an OpenHelper/Room failure: corruption
 * and incompatible versions must surface as an explicit recovery gate.
 */
val MIGRATION_1_2: Migration = object : Migration(1, 2) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE catalog_cache_state " +
            "ADD COLUMN stagingObservedAtMs INTEGER NOT NULL DEFAULT 0")
        db.execSQL("ALTER TABLE catalog_cache_state " +
            "ADD COLUMN stagingExpiresAtMs INTEGER NOT NULL DEFAULT 0")
        db.execSQL("ALTER TABLE catalog_cache_state " +
            "ADD COLUMN lastVerifiedAtMs INTEGER NOT NULL DEFAULT 0")
    }
}

val MIGRATION_2_3: Migration = object : Migration(2, 3) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE IF NOT EXISTS `local_sales_drafts` (
                `draftId` TEXT NOT NULL,
                `scopeDigest` TEXT NOT NULL,
                `organizationId` TEXT NOT NULL,
                `storeId` TEXT NOT NULL,
                `employeeId` TEXT NOT NULL,
                `clientVersion` INTEGER NOT NULL,
                `serverVersion` INTEGER,
                `currency` TEXT NOT NULL,
                `linesJson` TEXT NOT NULL,
                `stage` TEXT NOT NULL,
                `status` TEXT NOT NULL,
                `syncState` TEXT NOT NULL,
                `lastError` TEXT,
                `rawBackup` TEXT,
                `createdAtMs` INTEGER NOT NULL,
                `updatedAtMs` INTEGER NOT NULL,
                PRIMARY KEY(`draftId`)
            )
        """.trimIndent())
        db.execSQL("CREATE INDEX IF NOT EXISTS `index_local_sales_drafts_scopeDigest` ON `local_sales_drafts` (`scopeDigest`)")
        db.execSQL("CREATE INDEX IF NOT EXISTS `index_local_sales_drafts_employeeId` ON `local_sales_drafts` (`employeeId`)")

        db.execSQL("""
            CREATE TABLE IF NOT EXISTS `sales_sync_intents` (
                `intentId` TEXT NOT NULL,
                `draftId` TEXT NOT NULL,
                `scopeDigest` TEXT NOT NULL,
                `organizationId` TEXT NOT NULL,
                `storeId` TEXT NOT NULL,
                `employeeId` TEXT NOT NULL,
                `idempotencyKey` TEXT NOT NULL,
                `action` TEXT NOT NULL,
                `eventDigest` TEXT NOT NULL,
                `payloadJson` TEXT NOT NULL,
                `state` TEXT NOT NULL,
                `attempts` INTEGER NOT NULL DEFAULT 0,
                `lastError` TEXT,
                `createdAtMs` INTEGER NOT NULL,
                `updatedAtMs` INTEGER NOT NULL,
                PRIMARY KEY(`intentId`)
            )
        """.trimIndent())
        db.execSQL("CREATE INDEX IF NOT EXISTS `index_sales_sync_intents_draftId` ON `sales_sync_intents` (`draftId`)")
        db.execSQL("CREATE UNIQUE INDEX IF NOT EXISTS `index_sales_sync_intents_idempotencyKey` ON `sales_sync_intents` (`idempotencyKey`)")
        db.execSQL("CREATE INDEX IF NOT EXISTS `index_sales_sync_intents_state` ON `sales_sync_intents` (`state`)")
    }
}

/** No backfill: historical local IDs and successful-looking v3 states prove no server ID. */
val MIGRATION_3_4: Migration = object : Migration(3, 4) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE local_sales_drafts ADD COLUMN serverDraftId TEXT")
    }
}
