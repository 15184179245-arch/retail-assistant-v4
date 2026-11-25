package com.retail.smartops.features.blocks

import com.retail.smartops.core.network.ArtifactOpRecord
import com.retail.smartops.core.network.L2ConfirmationResult
import com.retail.smartops.core.network.L2ConfirmationStatus
import com.retail.smartops.core.network.PendingSuggestion
import com.retail.smartops.core.network.WorkspaceArtifact
import java.util.UUID

/**
 * Triple-encoded field co-edit state for C2C-140 §2.2 / §5.1.
 * - AI edits use icon ("✦") + label ("AI 刚改") + background tint (never color alone).
 * - Human edits automatically apply field lock ("🔒 人工锁定").
 */
data class FieldCoeditVisualState(
    val field: String,
    val isHumanLocked: Boolean,
    val isAiJustModified: Boolean,
    val pendingSuggestion: PendingSuggestion? = null,
    val aiBadgeIcon: String = "✦",
    val aiBadgeLabel: String = "AI 刚改",
    val lockBadgeLabel: String = "🔒 人工锁定",
)

data class CommandConfirmModel(
    val actionKey: String,
    val title: String,
    val whatHappens: String,
    val impactScope: String,
    val willNotDo: String,
    val approver: String,
    val artifactId: String,
    val baseVersion: Int,
    val idempotencyKey: String = UUID.randomUUID().toString(),
    val inFlight: Boolean = false,
    val outcome: L2ConfirmationResult? = null,
)

data class ArtifactCoeditUiState(
    val artifact: WorkspaceArtifact,
    val opsHistory: List<ArtifactOpRecord> = emptyList(),
    val previousValuesByOp: Map<String, Map<String, Any?>> = emptyMap(),
    val partialUndoNotice: String? = null,
    val conflictNotice: String? = null,
    val activeEditorNotice: String? = null,
    val confirmDialog: CommandConfirmModel? = null,
    val lastL2Outcome: L2ConfirmationResult? = null,
) {
    fun fieldState(field: String): FieldCoeditVisualState = FieldCoeditVisualState(
        field = field,
        isHumanLocked = field in artifact.lockedFields,
        isAiJustModified = field in artifact.aiModifiedFields && field !in artifact.lockedFields,
        pendingSuggestion = artifact.pendingSuggestions.firstOrNull { it.field == field },
    )
}

/**
 * Pure state machine implementing C2C-140 L1 co-editing (lock/unlock/suggestion/partial undo)
 * and L2 confirmation safety state.
 */
object ArtifactCoeditEngine {
    fun fromArtifact(
        artifact: WorkspaceArtifact,
        ops: List<ArtifactOpRecord> = emptyList(),
    ): ArtifactCoeditUiState = ArtifactCoeditUiState(
        artifact = artifact,
        opsHistory = if (ops.isNotEmpty()) ops else listOfNotNull(artifact.lastOp),
        partialUndoNotice = artifact.skippedFields.takeIf { it.isNotEmpty() }?.let {
            "${it.size} 个字段已被人工修改，未撤销"
        },
        activeEditorNotice = artifact.activeEditorNotice,
    )

    fun applyHumanEdit(
        state: ArtifactCoeditUiState,
        field: String,
        newValue: Any?,
        actorSubjectId: String = state.artifact.ownerSubjectId,
        opId: String = "op-${state.opsHistory.size + 1}",
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val prevMap = mapOf(field to current.payload[field])
        val nextPayload = current.payload.toMutableMap().apply { put(field, newValue) }
        val nextLocks = (current.lockedFields + field).distinct()
        val nextAiFields = current.aiModifiedFields.filterNot { it == field }
        val nextSuggestions = current.pendingSuggestions.filterNot { it.field == field }
        val op = ArtifactOpRecord(
            id = opId,
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "patch",
            patch = mapOf(field to newValue),
        )
        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                payload = nextPayload,
                lockedFields = nextLocks,
                aiModifiedFields = nextAiFields,
                pendingSuggestions = nextSuggestions,
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
            previousValuesByOp = state.previousValuesByOp + (opId to prevMap),
            partialUndoNotice = null,
            conflictNotice = null,
        )
    }

    fun applyAiPatch(
        state: ArtifactCoeditUiState,
        patch: Map<String, Any?>,
        reason: String = "AI 建议优化草稿字段",
        actorSubjectId: String = "agent",
        opId: String = "op-${state.opsHistory.size + 1}",
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val nextPayload = current.payload.toMutableMap()
        val prevValues = linkedMapOf<String, Any?>()
        val appliedPatch = linkedMapOf<String, Any?>()
        val addedAiFields = mutableListOf<String>()
        val nextSuggestions = current.pendingSuggestions.toMutableList()

        for ((field, value) in patch) {
            if (field in current.lockedFields) {
                // Locked field: NEVER overwrite; append/replace suggestion for human decision.
                nextSuggestions.removeAll { it.field == field }
                nextSuggestions.add(
                    PendingSuggestion(
                        field = field,
                        suggestedValue = value,
                        reason = reason,
                        opId = opId,
                    ),
                )
            } else {
                prevValues[field] = current.payload[field]
                nextPayload[field] = value
                appliedPatch[field] = value
                addedAiFields.add(field)
            }
        }

        val op = ArtifactOpRecord(
            id = opId,
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "agent",
            actorSubjectId = actorSubjectId,
            opType = "patch",
            patch = appliedPatch,
        )

        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                payload = nextPayload,
                aiModifiedFields = (current.aiModifiedFields + addedAiFields).distinct(),
                pendingSuggestions = nextSuggestions,
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
            previousValuesByOp = state.previousValuesByOp + (opId to prevValues),
            partialUndoNotice = null,
            conflictNotice = null,
        )
    }

    fun unlockField(
        state: ArtifactCoeditUiState,
        field: String,
        actorSubjectId: String = state.artifact.ownerSubjectId,
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val op = ArtifactOpRecord(
            id = "op-${state.opsHistory.size + 1}",
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "unlock",
            patch = mapOf(field to null),
        )
        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                lockedFields = current.lockedFields.filterNot { it == field },
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
        )
    }

    fun lockField(
        state: ArtifactCoeditUiState,
        field: String,
        actorSubjectId: String = state.artifact.ownerSubjectId,
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val op = ArtifactOpRecord(
            id = "op-${state.opsHistory.size + 1}",
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "lock",
            patch = mapOf(field to current.payload[field]),
        )
        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                lockedFields = (current.lockedFields + field).distinct(),
                aiModifiedFields = current.aiModifiedFields.filterNot { it == field },
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
        )
    }

    /**
     * Accepting an AI suggestion applies the suggested value AND keeps the human lock
     * (per C2C-140 §2.2: treated as human-confirmed value).
     */
    fun acceptSuggestion(
        state: ArtifactCoeditUiState,
        field: String,
        opId: String,
        actorSubjectId: String = state.artifact.ownerSubjectId,
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val suggestion = current.pendingSuggestions.firstOrNull {
            it.field == field && (opId.isBlank() || it.opId == opId)
        } ?: return state
        val prevMap = mapOf(field to current.payload[field])
        val nextPayload = current.payload.toMutableMap().apply { put(field, suggestion.suggestedValue) }
        val newOpId = "op-${state.opsHistory.size + 1}"
        val op = ArtifactOpRecord(
            id = newOpId,
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "accept_suggestion",
            patch = mapOf(field to suggestion.suggestedValue),
        )
        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                payload = nextPayload,
                lockedFields = (current.lockedFields + field).distinct(),
                aiModifiedFields = current.aiModifiedFields.filterNot { it == field },
                pendingSuggestions = current.pendingSuggestions.filterNot { it.field == field },
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
            previousValuesByOp = state.previousValuesByOp + (newOpId to prevMap),
        )
    }

    /**
     * Ignoring a suggestion dismisses it while preserving the human field lock.
     */
    fun ignoreSuggestion(
        state: ArtifactCoeditUiState,
        field: String,
        opId: String,
        actorSubjectId: String = state.artifact.ownerSubjectId,
    ): ArtifactCoeditUiState {
        val current = state.artifact
        val op = ArtifactOpRecord(
            id = "op-${state.opsHistory.size + 1}",
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "ignore_suggestion",
        )
        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                pendingSuggestions = current.pendingSuggestions.filterNot {
                    it.field == field && (opId.isBlank() || it.opId == opId)
                },
                lastOp = op,
            ),
            opsHistory = state.opsHistory + op,
        )
    }

    /**
     * Partial undo (C2C-140 §2.2):
     * Reverts fields modified by `targetOpId` ONLY if they were not subsequently modified by the other actor
     * (e.g., human edited a field after AI touched it). Conflicting fields are skipped and reported.
     */
    fun undoOperation(
        state: ArtifactCoeditUiState,
        targetOpId: String,
        actorSubjectId: String = state.artifact.ownerSubjectId,
    ): ArtifactCoeditUiState {
        val targetIdx = state.opsHistory.indexOfFirst { it.id == targetOpId }
        if (targetIdx < 0) return state
        val targetOp = state.opsHistory[targetIdx]
        val laterOps = state.opsHistory.drop(targetIdx + 1)
        val prevValues = state.previousValuesByOp[targetOpId].orEmpty()

        val current = state.artifact
        val nextPayload = current.payload.toMutableMap()
        val revertedFields = mutableListOf<String>()
        val skippedFields = mutableListOf<String>()

        for (field in targetOp.patch.keys) {
            val touchedLaterByOther = laterOps.any { later ->
                later.opType in setOf("patch", "accept_suggestion") &&
                    later.patch.containsKey(field) &&
                    later.actorType != targetOp.actorType
            }
            val humanLockedAfterAi = targetOp.actorType == "agent" && field in current.lockedFields
            if (touchedLaterByOther || humanLockedAfterAi) {
                skippedFields.add(field)
            } else {
                if (prevValues.containsKey(field)) {
                    val oldVal = prevValues[field]
                    if (oldVal == null) nextPayload.remove(field) else nextPayload[field] = oldVal
                }
                revertedFields.add(field)
            }
        }

        val notice = when {
            skippedFields.isNotEmpty() && targetOp.actorType == "agent" ->
                "${skippedFields.size} 个字段已被人工修改，未撤销"
            skippedFields.isNotEmpty() ->
                "${skippedFields.size} 个字段已被后续操作修改，未撤销"
            else -> null
        }

        val nextAiFields = current.aiModifiedFields.filterNot { it in revertedFields }
        val undoOp = ArtifactOpRecord(
            id = "op-${state.opsHistory.size + 1}",
            artifactId = current.id,
            versionBefore = current.version,
            versionAfter = current.version + 1,
            actorType = "human",
            actorSubjectId = actorSubjectId,
            opType = "undo",
            undoOfOpId = targetOpId,
            skippedFields = skippedFields,
            message = notice,
        )

        return state.copy(
            artifact = current.copy(
                version = current.version + 1,
                payload = nextPayload,
                aiModifiedFields = nextAiFields,
                skippedFields = skippedFields,
                lastOp = undoOp,
                message = notice,
            ),
            opsHistory = state.opsHistory + undoOp,
            partialUndoNotice = notice,
        )
    }

    /**
     * Records an L2 outcome. Unknown/timeout MUST remain UNVERIFIED without auto-resend.
     */
    fun recordL2Outcome(
        state: ArtifactCoeditUiState,
        outcome: L2ConfirmationResult,
    ): ArtifactCoeditUiState {
        val nextArtifact = outcome.artifact ?: if (outcome.status == L2ConfirmationStatus.VERIFIED) {
            state.artifact.copy(
                status = "submitted",
                submittedReceiptRef = outcome.receiptRef,
            )
        } else {
            state.artifact
        }
        return state.copy(
            artifact = nextArtifact,
            confirmDialog = state.confirmDialog?.copy(
                inFlight = false,
                outcome = outcome,
            ),
            lastL2Outcome = outcome,
            conflictNotice = if (outcome.status == L2ConfirmationStatus.CONFLICT) outcome.message else null,
        )
    }
}
