package com.retail.smartops.features.receiving

import com.retail.smartops.core.auth.Capabilities
import com.retail.smartops.core.auth.Session
import com.retail.smartops.core.auth.SessionRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

internal const val RECEIVING_NOT_SUBMITTED = "仅本页观察 · 未提交；此版本未接入接收或复核流程。"

internal enum class ReceivingField {
    NOTE_HASH, LINE_KEY, LABEL, QUANTITY, UNIT, EVIDENCE_HASH,
}

internal data class ReceivingObservationLine(
    val lineKey: String,
    val productLabel: String,
    val quantity: String,
    val unit: String,
    val evidenceSha256: String,
)

internal data class ReceivingObservationState(
    val noteHash: String = "",
    val lineKey: String = "",
    val label: String = "",
    val quantity: String = "",
    val unit: String = "piece",
    val evidenceHash: String = "",
    val lines: List<ReceivingObservationLine> = emptyList(),
    val error: String? = null,
    val confirmDiscard: Boolean = false,
) {
    val hasInput: Boolean
        get() = lines.isNotEmpty() || noteHash.isNotEmpty() || lineKey.isNotEmpty() ||
            label.isNotEmpty() || quantity.isNotEmpty() || unit != "piece" || evidenceHash.isNotEmpty()

    fun value(field: ReceivingField): String = when (field) {
        ReceivingField.NOTE_HASH -> noteHash
        ReceivingField.LINE_KEY -> lineKey
        ReceivingField.LABEL -> label
        ReceivingField.QUANTITY -> quantity
        ReceivingField.UNIT -> unit
        ReceivingField.EVIDENCE_HASH -> evidenceHash
    }
}

/** Local editor without any destination, persistence, or claimed acknowledgement. */
internal class ReceivingObservationForm(private val editWhileCurrent: (() -> Unit) -> Unit) {
    private val mutableState = MutableStateFlow(ReceivingObservationState())
    val state = mutableState.asStateFlow()

    fun edit(field: ReceivingField, value: String) = editWhileCurrent {
        val current = state.value
        val next = when (field) {
            ReceivingField.NOTE_HASH -> current.copy(noteHash = value.trim().lowercase())
            ReceivingField.LINE_KEY -> current.copy(lineKey = value)
            ReceivingField.LABEL -> current.copy(label = value)
            ReceivingField.QUANTITY -> current.copy(quantity = value)
            ReceivingField.UNIT -> current.copy(unit = value)
            ReceivingField.EVIDENCE_HASH -> current.copy(evidenceHash = value.trim().lowercase())
        }
        mutableState.value = next.copy(error = null)
    }

    fun addLine() = editWhileCurrent {
        val current = state.value
        val key = current.lineKey.trim()
        val label = current.label.trim()
        val quantity = current.quantity.trim()
        val unit = current.unit.trim()
        val evidence = current.evidenceHash
        val error = when {
            key.isEmpty() || label.isEmpty() -> "请填写行标识和商品原文。"
            current.lines.any { it.lineKey == key } -> "本批行标识已存在，未修改原记录。"
            !DECIMAL.matches(quantity) || unit !in UNITS -> "数量或单位无效；不会自动换算。"
            !SHA256.matches(evidence) -> "请填写 64 位现场证据 SHA-256。"
            else -> null
        }
        mutableState.value = if (error != null) current.copy(error = error) else current.copy(
            lineKey = "", label = "", quantity = "", evidenceHash = "",
            lines = current.lines + ReceivingObservationLine(key, label,
                quantity.toBigDecimal().toPlainString(), unit, evidence),
            error = null,
        )
    }

    fun requestDiscard() = editWhileCurrent {
        if (state.value.hasInput) mutableState.value = state.value.copy(confirmDiscard = true)
    }

    fun dismissDiscard() = editWhileCurrent {
        mutableState.value = state.value.copy(confirmDiscard = false)
    }

    fun confirmDiscard() = editWhileCurrent {
        if (state.value.confirmDiscard) clear()
    }

    internal fun clear() { mutableState.value = ReceivingObservationState() }

    private companion object {
        val DECIMAL = Regex("[0-9]{1,24}(\\.[0-9]{1,3})?")
        val SHA256 = Regex("[0-9a-f]{64}")
        val UNITS = setOf("piece", "box", "kg", "meter")
    }
}

/** Retained by the activity owner, but never past logout, expiry, or process death. */
internal class ReceivingObservationWorkspace(private val sessions: SessionRepository) : AutoCloseable {
    // Accessed only under the repository's session monitor.
    private data class Active(val generation: Int, val lease: Any, val form: ReceivingObservationForm)
    private var active: Active? = null
    private var closed = false
    private val unsubscribe = sessions.onScopeInvalidated {
        val status = sessions.status.value
        if (status.session == null || active?.generation != status.generation ||
            !status.capabilities.has(Capabilities.PURCHASING_READ)
        ) clearActive()
    }

    fun formFor(expected: Session, generation: Int): ReceivingObservationForm? {
        var result: ReceivingObservationForm? = null
        whileAllowed(expected, generation) {
            if (active?.generation != generation) {
                clearActive()
                val lease = Any()
                active = Active(generation, lease, ReceivingObservationForm { action ->
                    whileAllowed(expected, generation) {
                        if (active?.lease === lease) action()
                    }
                })
            }
            result = active?.form
        }
        return result
    }

    private fun whileAllowed(expected: Session, generation: Int, action: () -> Unit) {
        sessions.whileCurrent(expected) {
            if (!closed && sessions.status.value.generation == generation &&
                sessions.hasCapability(Capabilities.PURCHASING_READ)
            ) action()
        }
    }

    private fun clearActive() {
        active?.form?.clear()
        active = null
    }

    override fun close() {
        synchronized(sessions) {
            closed = true
            clearActive()
            unsubscribe()
        }
    }
}
