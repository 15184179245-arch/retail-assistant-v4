package com.retail.smartops

/** Small, fixed demonstration catalog. Every field is synthetic and has no source-system identity. */
data class SyntheticProduct(
    val id: String,
    val code: String,
    val barcode: String,
    val name: String,
    val unitPriceFen: Int,
)

object SyntheticCatalog {
    val products = listOf(
        SyntheticProduct("demo-001", "SYN-C001", "6900000000017", "合成示例：十字螺丝刀", 1250),
        SyntheticProduct("demo-002", "SYN-C002", "6900000000024", "合成示例：活动扳手", 3680),
        SyntheticProduct("demo-003", "SYN-C003", "6900000000031", "合成示例：绝缘胶带", 560),
        SyntheticProduct("demo-004", "SYN-C004", "6900000000048", "合成示例：卷尺", 2190),
        SyntheticProduct("demo-005", "SYN-C005", "6900000000055", "合成示例：尖嘴钳", 2870),
    )

    fun search(query: String): List<SyntheticProduct> {
        val normalized = query.trim().lowercase()
        if (normalized.isEmpty()) return products
        return products.filter { product ->
            product.code.lowercase().contains(normalized) ||
                product.barcode.lowercase().contains(normalized) ||
                product.name.lowercase().contains(normalized)
        }
    }
}

data class DraftLine(val productId: String, val quantity: Int)

data class DraftMutation(val lines: List<DraftLine>, val statusMessage: String)

object DraftRules {
    const val MAX_QUANTITY = 999_999

    fun changeQuantity(current: Int, delta: Int): Int {
        require(current in 1..MAX_QUANTITY) { "当前数量超出允许范围" }
        return (current.toLong() + delta.toLong()).coerceIn(1L, MAX_QUANTITY.toLong()).toInt()
    }

    fun addLine(lines: List<DraftLine>, productId: String, quantity: Int): List<DraftLine> {
        require(SyntheticCatalog.products.any { it.id == productId }) { "未知的合成商品" }
        require(quantity in 1..MAX_QUANTITY) { "数量必须在 1 到 $MAX_QUANTITY 之间" }
        require(lines.map { it.productId }.distinct().size == lines.size) { "草稿中商品不能重复" }
        require(lines.all { line ->
            SyntheticCatalog.products.any { it.id == line.productId } && line.quantity in 1..MAX_QUANTITY
        }) { "草稿中包含无效商品或数量" }
        val existing = lines.firstOrNull { it.productId == productId }
        return if (existing == null) {
            lines + DraftLine(productId, quantity)
        } else {
            val combined = existing.quantity.toLong() + quantity.toLong()
            require(combined <= MAX_QUANTITY) { "合并后数量不能超过 $MAX_QUANTITY" }
            lines.map { if (it.productId == productId) it.copy(quantity = combined.toInt()) else it }
        }
    }

    fun changeLineQuantity(lines: List<DraftLine>, productId: String, delta: Int): DraftMutation {
        require(lines.any { it.productId == productId }) { "草稿商品不存在" }
        val updated = if (delta < 0 && lines.first { it.productId == productId }.quantity <= 1) {
            lines.filterNot { it.productId == productId }
        } else {
            lines.map {
                if (it.productId == productId) it.copy(quantity = changeQuantity(it.quantity, delta)) else it
            }
        }
        return DraftMutation(updated, "草稿已修改，尚未保存。")
    }

    fun clearDraft(): DraftMutation = DraftMutation(emptyList(), "本机合成草稿已清空。")
}
