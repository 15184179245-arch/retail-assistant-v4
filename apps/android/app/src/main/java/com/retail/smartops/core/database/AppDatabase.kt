package com.retail.smartops.core.database

import android.content.Context
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Index
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Transaction

/** No credentials, prices, stock balances, or formal order data belong in this cache. */
@Entity(tableName = "catalog_cache_items",
    primaryKeys = ["snapshotVersion", "productId"],
    indices = [Index(value = ["snapshotVersion"])])
data class CatalogItemEntity(
    val snapshotVersion: String,
    val productId: String,
    val code: String,
    val barcode: String?,
    val name: String,
    val searchText: String,
    val sourceVersion: String,
)

@Entity(tableName = "catalog_cache_state")
data class CatalogStateEntity(
    @PrimaryKey val id: Int = 1,
    val scopeDigest: String,
    val activeVersion: String?,
    val activeObservedAtMs: Long,
    val activeExpiresAtMs: Long,
    val stagingVersion: String?,
    @ColumnInfo(defaultValue = "0") val stagingObservedAtMs: Long,
    @ColumnInfo(defaultValue = "0") val stagingExpiresAtMs: Long,
    val stagedCount: Int,
    val expectedTotal: Int,
    val nextCursor: String?,
    /** Added in schema v2; v1->v2 migration preserves every row and active pointer. */
    @ColumnInfo(defaultValue = "0") val lastVerifiedAtMs: Long = 0L,
)

@Dao
interface CatalogCacheDao {
    @Query("SELECT * FROM catalog_cache_state WHERE id = 1")
    fun state(): CatalogStateEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun putState(state: CatalogStateEntity)

    @Insert(onConflict = OnConflictStrategy.ABORT)
    fun insertItems(items: List<CatalogItemEntity>)

    @Query("SELECT COUNT(*) FROM catalog_cache_items WHERE snapshotVersion = :version")
    fun count(version: String): Int

    @Query("DELETE FROM catalog_cache_items WHERE snapshotVersion = :version")
    fun removeVersion(version: String)

    @Query("DELETE FROM catalog_cache_items WHERE snapshotVersion != :version")
    fun removeOtherVersions(version: String)

    // Rank literal barcode/code matches before truncating broad name/code candidates.
    @Query("""SELECT * FROM catalog_cache_items
        WHERE snapshotVersion = :version AND instr(searchText, :query) > 0
        ORDER BY CASE WHEN code COLLATE NOCASE = :entered OR barcode COLLATE BINARY = :entered
            THEN 0 ELSE 1 END, name, productId LIMIT :limit""")
    fun search(version: String, query: String, limit: Int, entered: String = query): List<CatalogItemEntity>
}

@Entity(
    tableName = "local_sales_drafts",
    indices = [
        Index(value = ["scopeDigest"]),
        Index(value = ["employeeId"]),
    ]
)
data class LocalSalesDraftEntity(
    @PrimaryKey val draftId: String,
    val scopeDigest: String,
    val organizationId: String,
    val storeId: String,
    val employeeId: String,
    val clientVersion: Int,
    val serverVersion: Int? = null,
    // NULL is an unknown server identity, never an alias for the local primary key.
    val serverDraftId: String? = null,
    val currency: String = "CNY",
    val linesJson: String,
    val stage: String,
    val status: String,
    val syncState: String,
    val lastError: String? = null,
    val rawBackup: String? = null,
    val createdAtMs: Long,
    val updatedAtMs: Long,
)

@Entity(
    tableName = "sales_sync_intents",
    indices = [
        Index(value = ["draftId"]),
        Index(value = ["idempotencyKey"], unique = true),
        Index(value = ["state"]),
    ]
)
data class SalesSyncIntentEntity(
    @PrimaryKey val intentId: String,
    val draftId: String,
    val scopeDigest: String,
    val organizationId: String,
    val storeId: String,
    val employeeId: String,
    val idempotencyKey: String,
    val action: String,
    val eventDigest: String,
    val payloadJson: String,
    val state: String,
    @ColumnInfo(defaultValue = "0") val attempts: Int = 0,
    val lastError: String? = null,
    val createdAtMs: Long,
    val updatedAtMs: Long,
)

@Dao
interface SalesDraftDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun putDraft(draft: LocalSalesDraftEntity)

    @Query("SELECT * FROM local_sales_drafts WHERE draftId = :draftId")
    fun getDraft(draftId: String): LocalSalesDraftEntity?

    @Query("""SELECT * FROM local_sales_drafts
        WHERE scopeDigest = :scopeDigest AND employeeId = :employeeId
        ORDER BY updatedAtMs DESC""")
    fun listDrafts(scopeDigest: String, employeeId: String): List<LocalSalesDraftEntity>

    @Query("DELETE FROM local_sales_drafts WHERE draftId = :draftId")
    fun deleteDraft(draftId: String)

    @Insert(onConflict = OnConflictStrategy.ABORT)
    fun insertIntent(intent: SalesSyncIntentEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun putIntent(intent: SalesSyncIntentEntity)

    @Query("SELECT * FROM sales_sync_intents WHERE intentId = :intentId")
    fun getIntent(intentId: String): SalesSyncIntentEntity?

    @Query("SELECT * FROM sales_sync_intents WHERE idempotencyKey = :key")
    fun findIntentByKey(key: String): SalesSyncIntentEntity?

    @Query("SELECT * FROM sales_sync_intents WHERE draftId = :draftId ORDER BY createdAtMs, intentId")
    fun listDraftIntents(draftId: String): List<SalesSyncIntentEntity>

    @Transaction
    fun getOrCreateIntent(intent: SalesSyncIntentEntity): SalesSyncIntentEntity {
        val draft = getDraft(intent.draftId) ?: error("draft_not_found")
        check(draft.scopeDigest == intent.scopeDigest && draft.organizationId == intent.organizationId &&
            draft.storeId == intent.storeId && draft.employeeId == intent.employeeId) { "draft_scope_mismatch" }
        val existing = listDraftIntents(intent.draftId).filter { it.action == intent.action }
        check(existing.size <= 1) { "duplicate_intent_hold" }
        existing.singleOrNull()?.let {
            check(it.payloadJson == intent.payloadJson && it.scopeDigest == intent.scopeDigest &&
                it.organizationId == intent.organizationId && it.storeId == intent.storeId &&
                it.employeeId == intent.employeeId) { "intent_payload_conflict" }
            return it
        }
        insertIntent(intent)
        putDraft(draft.copy(syncState = "pending", updatedAtMs = intent.updatedAtMs))
        return intent
    }

    /** Preserve a concurrently accepted server binding while saving local edits. */
    @Transaction
    fun saveOwnedDraft(candidate: LocalSalesDraftEntity): LocalSalesDraftEntity {
        val current = getDraft(candidate.draftId)
        check(current == null || (current.scopeDigest == candidate.scopeDigest &&
            current.organizationId == candidate.organizationId && current.storeId == candidate.storeId &&
            current.employeeId == candidate.employeeId)) { "draft_scope_mismatch" }
        val saved = if (current == null) candidate else candidate.copy(
            clientVersion = current.clientVersion + 1, serverDraftId = current.serverDraftId,
            serverVersion = current.serverVersion, createdAtMs = current.createdAtMs,
        )
        putDraft(saved)
        return saved
    }

    @Transaction
    fun readOwnedDraftSnapshot(
        draftId: String, scopeDigest: String, organizationId: String, storeId: String, employeeId: String,
    ): Pair<LocalSalesDraftEntity, List<SalesSyncIntentEntity>>? {
        val draft = getDraft(draftId)?.takeIf {
            it.scopeDigest == scopeDigest && it.organizationId == organizationId &&
                it.storeId == storeId && it.employeeId == employeeId
        } ?: return null
        val intents = listDraftIntents(draftId)
        require(intents.all {
            it.scopeDigest == scopeDigest && it.organizationId == organizationId &&
                it.storeId == storeId && it.employeeId == employeeId
        }) { "draft_scope_mismatch" }
        return draft to intents
    }

    /** Own the snapshot and first never-sent key in one Room transaction. */
    @Transaction
    fun persistDraftAndInitialIntent(
        candidate: LocalSalesDraftEntity, initialIntent: SalesSyncIntentEntity,
    ): LocalSalesDraftEntity {
        val existing = getDraft(candidate.draftId)
        if (existing == null) {
            check(initialIntent.draftId == candidate.draftId &&
                initialIntent.scopeDigest == candidate.scopeDigest &&
                initialIntent.organizationId == candidate.organizationId &&
                initialIntent.storeId == candidate.storeId && initialIntent.employeeId == candidate.employeeId
            ) { "draft_scope_mismatch" }
            createDraftAndIntent(candidate, initialIntent)
            return candidate
        }
        check(existing.syncState != "corrupt" && existing.status != "paid") { "draft_read_only" }
        check(listDraftIntents(candidate.draftId).none { it.action == "prepare_handoff_v4" }) { "draft_read_only" }
        return saveOwnedDraft(candidate.copy(rawBackup = existing.rawBackup, lastError = existing.lastError))
    }

    @Transaction
    fun saveRepairsOfOwnedDraft(
        expected: LocalSalesDraftEntity, repair: LocalSalesDraftEntity,
    ): Boolean {
        val current = getDraft(expected.draftId) ?: return false
        if (current != expected || current.serverDraftId != null || current.status == "paid" ||
            repair.draftId != current.draftId || repair.scopeDigest != current.scopeDigest ||
            repair.organizationId != current.organizationId || repair.storeId != current.storeId ||
            repair.employeeId != current.employeeId ||
            listDraftIntents(current.draftId).any {
                it.action != "create_draft_v4" || it.state != "pending" || it.attempts != 0
            }
        ) return false
        putDraft(repair)
        return true
    }

    @Query("""SELECT * FROM sales_sync_intents
        WHERE scopeDigest = :scopeDigest AND employeeId = :employeeId AND state IN ('pending', 'in_flight')
        ORDER BY createdAtMs ASC""")
    fun listPendingIntents(scopeDigest: String, employeeId: String): List<SalesSyncIntentEntity>

    @Query("UPDATE sales_sync_intents SET state = :state, attempts = attempts + 1, lastError = :error, updatedAtMs = :nowMs WHERE intentId = :intentId")
    fun updateIntentState(intentId: String, state: String, error: String?, nowMs: Long)

    @Query("DELETE FROM sales_sync_intents WHERE intentId = :intentId")
    fun deleteIntent(intentId: String)

    @Transaction
    fun createDraftAndIntent(draft: LocalSalesDraftEntity, intent: SalesSyncIntentEntity) {
        check(getDraft(draft.draftId) == null) { "draft_already_exists" }
        putDraft(draft)
        insertIntent(intent)
    }

    @Query("""UPDATE sales_sync_intents SET state = 'in_flight', attempts = attempts + 1,
        lastError = NULL, updatedAtMs = :nowMs WHERE intentId = :intentId AND state = 'pending'""")
    fun claimPendingIntent(intentId: String, nowMs: Long): Int

    @Transaction
    fun completePreparation(intentId: String, serverId: String, linesJson: String, version: Int, nowMs: Long): Boolean {
        val intent = getIntent(intentId) ?: return false
        val draft = getDraft(intent.draftId) ?: return false
        if (intent.state != "pending" || draft.serverDraftId != serverId || draft.linesJson != linesJson ||
            draft.scopeDigest != intent.scopeDigest || draft.employeeId != intent.employeeId ||
            draft.organizationId != intent.organizationId || draft.storeId != intent.storeId) return false
        putDraft(draft.copy(serverVersion = version, syncState = "synced", lastError = null, updatedAtMs = nowMs))
        putIntent(intent.copy(state = "prepared", lastError = null, updatedAtMs = nowMs))
        return true
    }

    @Transaction
    fun completeDraftCreation(intentId: String, serverId: String, serverVersion: Int, nowMs: Long): Boolean {
        val intent = getIntent(intentId) ?: return false
        val draft = getDraft(intent.draftId) ?: return false
        if (intent.state != "in_flight" || intent.scopeDigest != draft.scopeDigest ||
            intent.organizationId != draft.organizationId || intent.storeId != draft.storeId ||
            intent.employeeId != draft.employeeId ||
            (draft.serverDraftId != null && draft.serverDraftId != serverId)) return false
        putDraft(draft.copy(serverDraftId = serverId, serverVersion = serverVersion,
            syncState = "synced", lastError = null, updatedAtMs = nowMs))
        putIntent(intent.copy(state = "synced", lastError = null, updatedAtMs = nowMs))
        return true
    }
}

/** v4 keeps local draft keys and adds an explicitly verified server identity. */
@Database(
    entities = [
        CatalogItemEntity::class,
        CatalogStateEntity::class,
        LocalSalesDraftEntity::class,
        SalesSyncIntentEntity::class,
    ],
    version = 4,
    exportSchema = true
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun catalog(): CatalogCacheDao
    abstract fun salesDraft(): SalesDraftDao

    companion object {
        fun open(context: Context, scope: CacheIdentity): AppDatabase =
            Room.databaseBuilder(
                context.applicationContext,
                AppDatabase::class.java,
                "catalog-cache-${scope.digest()}.db"
            )
                .addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4)
                // Intentionally no fallbackToDestructiveMigration.
                .build()
    }
}
