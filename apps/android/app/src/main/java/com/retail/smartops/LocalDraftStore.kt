package com.retail.smartops

import android.content.Context
import android.content.SharedPreferences
import java.nio.charset.StandardCharsets
import java.util.Base64

/** Pure serialization for an app-private, synthetic-only local draft. */
object DraftCodec {
    private const val VERSION = "synthetic-draft-v1"

    fun encode(lines: List<DraftLine>): String = buildString {
        require(lines.map { it.productId }.distinct().size == lines.size) { "草稿商品不能重复" }
        append(VERSION)
        lines.forEach { line ->
            require(SyntheticCatalog.products.any { it.id == line.productId }) { "草稿商品无效" }
            require(line.quantity in 1..DraftRules.MAX_QUANTITY) { "草稿数量超出允许范围" }
            append('\n')
            append(Base64.getUrlEncoder().withoutPadding().encodeToString(line.productId.toByteArray(StandardCharsets.UTF_8)))
            append('\t')
            append(line.quantity)
        }
    }

    fun decode(encoded: String?): List<DraftLine> {
        if (encoded.isNullOrBlank()) return emptyList()
        val rows = encoded.split('\n')
        require(rows.firstOrNull() == VERSION) { "草稿版本无法识别" }
        return rows.drop(1).filter { it.isNotBlank() }.map { row ->
            val fields = row.split('\t')
            require(fields.size == 2) { "草稿行格式无效" }
            val id = String(Base64.getUrlDecoder().decode(fields[0]), StandardCharsets.UTF_8)
            val quantity = fields[1].toInt()
            require(
                SyntheticCatalog.products.any { it.id == id } && quantity in 1..DraftRules.MAX_QUANTITY,
            ) { "草稿内容无效" }
            DraftLine(id, quantity)
        }.also { lines ->
            require(lines.map { it.productId }.distinct().size == lines.size) { "草稿商品不能重复" }
        }
    }
}

class LocalDraftStore(context: Context) {
    private val preferences: SharedPreferences = context.getSharedPreferences("synthetic_preview", Context.MODE_PRIVATE)

    fun load(): List<DraftLine> = runCatching { DraftCodec.decode(preferences.getString(KEY, null)) }.getOrDefault(emptyList())

    fun save(lines: List<DraftLine>): Boolean = preferences.edit().putString(KEY, DraftCodec.encode(lines)).commit()

    fun clear(): Boolean = preferences.edit().remove(KEY).commit()

    private companion object { const val KEY = "local_draft" }
}
