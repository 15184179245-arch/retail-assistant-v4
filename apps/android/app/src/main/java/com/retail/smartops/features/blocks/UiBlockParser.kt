package com.retail.smartops.features.blocks

import com.retail.smartops.core.ui.StatusKind
import org.json.JSONArray
import org.json.JSONObject

/**
 * 11 canonical generative UI block types permitted by `contracts/ui-blocks.schema.json` v1 (C2C-140 §2.4).
 */
enum class UiBlockType(val wire: String, val displayName: String) {
    PRODUCT_CARD("product_card", "商品卡片"),
    PRODUCT_LIST("product_list", "商品列表"),
    COMPARE_TABLE("compare_table", "商品对比"),
    QUOTE_DRAFT("quote_draft", "报价草稿"),
    PURCHASE_DRAFT("purchase_draft", "采购草稿"),
    STOCK_LOCATION("stock_location", "库位库存"),
    WORK_ITEM_LIST("work_item_list", "工作项列表"),
    EVIDENCE_PANEL("evidence_panel", "来源证据"),
    ACTION_CARD("action_card", "L2 待确认动作"),
    FORM_REQUEST("form_request", "补充信息表单"),
    ARTIFACT_LINK("artifact_link", "工作成果链接");

    companion object {
        fun fromWire(raw: String?): UiBlockType? =
            entries.firstOrNull { it.wire == raw?.trim() }
    }
}

data class BlockProvenance(
    val source: String,
    val syncedAt: String?,
    val verifiedBy: String?,
    val statusKind: StatusKind,
    val stale: Boolean = false,
)

sealed interface ParsedUiBlock {
    val blockId: String
    val rawType: String
    val version: String

    /**
     * Streaming placeholder rendered before server `refs` resolution & schema validation complete.
     */
    data class StreamingSkeleton(
        override val blockId: String,
        override val rawType: String,
        override val version: String,
        val hint: String = "正在校验块内容与来源引用…",
    ) : ParsedUiBlock

    /**
     * Safe plain-text degradation for unknown block types, unsupported versions,
     * schema failures, HTML/JS injection attempts, or unverified model self-filled price/stock/amount.
     */
    data class DegradedText(
        override val blockId: String,
        override val rawType: String,
        override val version: String,
        val fallbackText: String,
        val reason: String,
        val statusKind: StatusKind = StatusKind.UNVERIFIED,
    ) : ParsedUiBlock

    /**
     * Strictly validated whitelist block with server-resolved provenance.
     */
    data class Valid(
        override val blockId: String,
        val type: UiBlockType,
        override val version: String,
        val props: Map<String, Any?>,
        val refs: List<Map<String, Any?>>,
        val resolvedRefs: Map<String, Any?>,
        val provenance: BlockProvenance,
        val artifactId: String? = null,
        val rawMap: Map<String, Any?> = emptyMap(),
    ) : ParsedUiBlock {
        override val rawType: String get() = type.wire
    }
}

/**
 * Strict runtime validator for generative UI blocks using `org.json` (per FE-005 ADR).
 * Enforces:
 * - 11-type whitelist
 * - Version compatibility (major version 1; unknown/older versions degrade)
 * - No raw HTML/JS payloads
 * - Zero tolerance for model self-filled price/stock/amount not backed by server `resolved_refs`
 * - Safe degradation to plain text + `StatusKind.UNVERIFIED`
 */
object UiBlockParser {
    private val SUPPORTED_MAJOR_VERSIONS = setOf(1)

    private val SENSITIVE_COMMERCIAL_KEYS = setOf(
        "price",
        "unit_price",
        "unitprice",
        "cost_price",
        "sell_price",
        "sale_price",
        "list_price",
        "final_price",
        "stock",
        "stock_qty",
        "stock_quantity",
        "available_stock",
        "available_qty",
        "on_hand",
        "amount",
        "total_amount",
        "subtotal",
        "discount_amount",
        "balance",
        "cost_amount",
        "line_total",
    )

    private val FORBIDDEN_HTML_KEYS = setOf(
        "html",
        "raw_html",
        "inner_html",
        "innerhtml",
        "outer_html",
        "dangerouslysetinnerhtml",
        "dangerously_set_inner_html",
        "script",
        "js",
        "javascript",
        "markup",
        "onclick",
        "onload",
        "onerror",
    )

    private val HTML_OR_JS_REGEX = Regex(
        pattern = "<[a-zA-Z!/]|javascript\\s*:|\\bon(error|load|click|mouseover|focus|submit)\\s*=",
        options = setOf(RegexOption.IGNORE_CASE),
    )

    fun parseJsonString(rawJson: String): ParsedUiBlock {
        val obj = try {
            JSONObject(rawJson)
        } catch (_: Exception) {
            return ParsedUiBlock.DegradedText(
                blockId = "invalid-json",
                rawType = "unknown",
                version = "unknown",
                fallbackText = sanitizePlainText(rawJson),
                reason = "块 JSON 格式无效，已降级为纯文本展示。",
            )
        }
        return parseMap(jsonObjectToMap(obj))
    }

    fun parseListJsonString(rawJsonArray: String): List<ParsedUiBlock> {
        val arr = try {
            JSONArray(rawJsonArray)
        } catch (_: Exception) {
            return listOf(
                ParsedUiBlock.DegradedText(
                    blockId = "invalid-list",
                    rawType = "unknown",
                    version = "unknown",
                    fallbackText = sanitizePlainText(rawJsonArray),
                    reason = "块列表 JSON 无效，已降级为纯文本。",
                ),
            )
        }
        return (0 until arr.length()).map { idx ->
            val item = arr.optJSONObject(idx)
            if (item == null) {
                ParsedUiBlock.DegradedText(
                    blockId = "block-$idx",
                    rawType = "unknown",
                    version = "unknown",
                    fallbackText = sanitizePlainText(arr.opt(idx)?.toString().orEmpty()),
                    reason = "块结构非对象，已降级为纯文本。",
                )
            } else {
                parseMap(jsonObjectToMap(item), fallbackId = "block-$idx")
            }
        }
    }

    @Suppress("UNCHECKED_CAST")
    fun parseMap(raw: Map<String, Any?>, fallbackId: String = "block-1"): ParsedUiBlock {
        val blockId = (raw["id"] as? String)?.takeIf { it.isNotBlank() }
            ?: (raw["block_id"] as? String)?.takeIf { it.isNotBlank() }
            ?: fallbackId
        val rawType = (raw["type"] as? String)?.trim().orEmpty()
        val versionStr = stringifyVersion(raw["version"])

        // 1. Streaming skeleton check before full validation
        if (raw["streaming"] == true) {
            return ParsedUiBlock.StreamingSkeleton(
                blockId = blockId,
                rawType = rawType.ifBlank { "pending" },
                version = versionStr.ifBlank { "1" },
            )
        }

        // 2. Explicit server degradation check
        if (raw["degraded"] == true) {
            val fallback = (raw["fallback_text"] as? String)?.takeIf { it.isNotBlank() }
                ?: extractFallbackSummary(raw)
            val reason = (raw["reason"] as? String)?.takeIf { it.isNotBlank() }
                ?: "服务端未通过校验，已安全降级。"
            return ParsedUiBlock.DegradedText(
                blockId = blockId,
                rawType = rawType.ifBlank { "unknown" },
                version = versionStr.ifBlank { "unknown" },
                fallbackText = sanitizePlainText(fallback),
                reason = reason,
                statusKind = StatusKind.UNVERIFIED,
            )
        }

        // 3. Whitelist check
        val blockType = UiBlockType.fromWire(rawType)
        if (blockType == null) {
            return degrade(
                blockId = blockId,
                rawType = rawType.ifBlank { "unknown" },
                version = versionStr,
                raw = raw,
                reason = "未知块类型「${rawType.ifBlank { "空" }}」，不在 v1 白名单内，已降级为纯文本。",
            )
        }

        // 4. Version compatibility check (current major 1)
        val major = parseMajorVersion(raw["version"])
        if (major == null || major !in SUPPORTED_MAJOR_VERSIONS) {
            return degrade(
                blockId = blockId,
                rawType = rawType,
                version = versionStr,
                raw = raw,
                reason = "不支持的块版本「${versionStr.ifBlank { "缺失" }}」，已降级为纯文本。",
            )
        }

        // 5. Required props & refs structure
        val props = raw["props"] as? Map<String, Any?>
            ?: return degrade(blockId, rawType, versionStr, raw, "块缺少 props 对象，已降级为纯文本。")

        val refsAny = raw["refs"]
            ?: return degrade(blockId, rawType, versionStr, raw, "块缺少服务端 refs 引用，已降级为纯文本。")
        val refsList = normalizeRefs(refsAny)
            ?: return degrade(blockId, rawType, versionStr, raw, "块 refs 格式无效，已降级为纯文本。")

        // 6. Reject raw HTML / JS anywhere in props
        if (containsForbiddenHtmlOrJs(props)) {
            return degrade(
                blockId = blockId,
                rawType = rawType,
                version = versionStr,
                raw = raw,
                reason = "检测到受禁用的 HTML/脚本内容，已阻止渲染并降级为纯文本。",
            )
        }

        // 7. Reject model self-filled price/stock/amount without server resolved_data/resolved_refs backing
        val resolvedData = (raw["resolved_data"] as? Map<String, Any?>) ?: emptyMap()
        val explicitResolvedRefs = (raw["resolved_refs"] as? Map<String, Any?>) ?: emptyMap()
        val resolvedRefs = resolvedData + explicitResolvedRefs
        val serverResolvedFlag = raw["server_resolved"] == true || resolvedRefs.isNotEmpty()
        if (hasUnverifiedCommercialValues(props, resolvedRefs, serverResolvedFlag)) {
            return degrade(
                blockId = blockId,
                rawType = rawType,
                version = versionStr,
                raw = raw,
                reason = "检测到未由服务端 refs 核验的价格/库存/金额字段（禁止模型自填），已降级为待核实纯文本。",
            )
        }

        // 8. Type-specific minimal structural contract
        val validationError = validatePropsByType(blockType, props, refsList, resolvedRefs)
        if (validationError != null) {
            return degrade(blockId, rawType, versionStr, raw, validationError)
        }

        // 9. Provenance & stale-ref check (stale refs MUST map to UNVERIFIED, never VERIFIED)
        val provenance = parseProvenance(raw, refsList, resolvedRefs)

        val artifactId = (props["artifact_id"] as? String)
            ?: (raw["artifact_id"] as? String)

        return ParsedUiBlock.Valid(
            blockId = blockId,
            type = blockType,
            version = versionStr.ifBlank { "1" },
            props = props,
            refs = refsList,
            resolvedRefs = resolvedRefs,
            provenance = provenance,
            artifactId = artifactId,
            rawMap = raw,
        )
    }

    private fun validatePropsByType(
        type: UiBlockType,
        props: Map<String, Any?>,
        refs: List<Map<String, Any?>>,
        resolvedRefs: Map<String, Any?>,
    ): String? = when (type) {
        UiBlockType.PRODUCT_CARD -> {
            val hasId = !(props["product_id"] as? String).isNullOrBlank()
            val hasTitle = !(props["title"] as? String).isNullOrBlank() || !(props["name"] as? String).isNullOrBlank()
            if (!hasId && !hasTitle && refs.isEmpty() && resolvedRefs.isEmpty()) "product_card 缺少 product_id 或名称字段" else null
        }
        UiBlockType.PRODUCT_LIST -> {
            if (props["items"] !is List<*> && refs.isEmpty() && resolvedRefs.isEmpty()) "product_list 缺少 items 列表" else null
        }
        UiBlockType.COMPARE_TABLE -> {
            val hasData = props["columns"] is List<*> || props["rows"] is List<*> || props["items"] is List<*>
            if (!hasData) "compare_table 缺少 columns/rows/items 对比数据" else null
        }
        UiBlockType.QUOTE_DRAFT, UiBlockType.PURCHASE_DRAFT -> {
            val stage = props["stage"] as? String
            val status = props["status"] as? String
            when {
                stage != null && stage !in setOf("draft", "ready") ->
                    "单据 stage 仅允许 draft/ready，收到非法值 '$stage'"
                status != null && status !in setOf("pending", "paid") ->
                    "单据 status 仅允许 pending/paid，收到非法值 '$status'"
                props["lines"] == null && props["line_refs"] == null && props["draft_id"] == null &&
                    props["artifact_id"] == null && refs.isEmpty() ->
                    "${type.wire} 缺少 lines / line_refs / draft_id / artifact_id"
                else -> null
            }
        }
        UiBlockType.STOCK_LOCATION -> {
            val hasLoc = !(props["location_code"] as? String).isNullOrBlank() ||
                !(props["location_id"] as? String).isNullOrBlank() ||
                props["locations"] is List<*> ||
                refs.isNotEmpty() ||
                resolvedRefs.isNotEmpty()
            if (!hasLoc) "stock_location 缺少库位字段" else null
        }
        UiBlockType.WORK_ITEM_LIST -> {
            if (props["items"] !is List<*> && refs.isEmpty() && resolvedRefs.isEmpty()) "work_item_list 缺少 items 列表" else null
        }
        UiBlockType.EVIDENCE_PANEL -> {
            val hasEvidence = props["evidences"] is List<*> ||
                props["items"] is List<*> ||
                !(props["summary"] as? String).isNullOrBlank() ||
                refs.isNotEmpty() ||
                resolvedRefs.isNotEmpty()
            if (!hasEvidence) "evidence_panel 缺少证据条目或摘要" else null
        }
        UiBlockType.ACTION_CARD -> {
            val hasActionKey = !(props["action_key"] as? String).isNullOrBlank()
            val hasTitle = !(props["title"] as? String).isNullOrBlank()
            val hasDesc = !(props["what_happens"] as? String).isNullOrBlank() ||
                !(props["description"] as? String).isNullOrBlank()
            if ((!hasActionKey && !hasTitle) || !hasDesc) "action_card 缺少 action_key/title 或动作说明" else null
        }
        UiBlockType.FORM_REQUEST -> {
            if (props["fields"] !is List<*>) "form_request 缺少 fields 定义" else null
        }
        UiBlockType.ARTIFACT_LINK -> {
            if ((props["artifact_id"] as? String).isNullOrBlank()) "artifact_link 缺少 artifact_id" else null
        }
    }

    private fun containsForbiddenHtmlOrJs(node: Any?): Boolean = when (node) {
        is Map<*, *> -> node.entries.any { (k, v) ->
            val key = k?.toString()?.lowercase().orEmpty()
            key in FORBIDDEN_HTML_KEYS || containsForbiddenHtmlOrJs(v)
        }
        is List<*> -> node.any { containsForbiddenHtmlOrJs(it) }
        is String -> HTML_OR_JS_REGEX.containsMatchIn(node)
        else -> false
    }

    private fun hasUnverifiedCommercialValues(
        props: Map<String, Any?>,
        resolvedRefs: Map<String, Any?>,
        serverResolvedFlag: Boolean,
    ): Boolean {
        if (props["model_unverified"] == true) return true
        val containsCommercial = containsCommercialKey(props)
        if (!containsCommercial) return false
        // If commercial keys (price/stock/amount) appear inside props without server resolution, block them.
        if (!serverResolvedFlag) return true
        // Even with resolvedRefs present, reject if props explicitly flags unverified numeric source
        if (resolvedRefs["unverified_commercial_override"] == true) return true
        return false
    }

    private fun containsCommercialKey(node: Any?): Boolean = when (node) {
        is Map<*, *> -> node.entries.any { (k, v) ->
            val key = k?.toString()?.lowercase().orEmpty()
            val isCommercial = key in SENSITIVE_COMMERCIAL_KEYS && v != null && v.toString().isNotBlank()
            isCommercial || containsCommercialKey(v)
        }
        is List<*> -> node.any { containsCommercialKey(it) }
        else -> false
    }

    @Suppress("UNCHECKED_CAST")
    private fun normalizeRefs(refsAny: Any?): List<Map<String, Any?>>? = when (refsAny) {
        is List<*> -> {
            if (refsAny.any { it !is Map<*, *> }) null
            else refsAny.map { it as Map<String, Any?> }
        }
        is Map<*, *> -> listOf(refsAny as Map<String, Any?>)
        else -> null
    }

    private fun parseProvenance(
        raw: Map<String, Any?>,
        refs: List<Map<String, Any?>>,
        resolvedRefs: Map<String, Any?>,
    ): BlockProvenance {
        val provMap = raw["provenance"] as? Map<*, *>
        val provStaleRefs = provMap?.get("stale_refs") as? List<*>
        val stale = (provMap?.get("stale") == true) ||
            (provStaleRefs?.isNotEmpty() == true) ||
            (resolvedRefs["stale"] == true) ||
            refs.any { it["stale"] == true }

        val rawStatus = (provMap?.get("status_kind") as? String)
            ?: (resolvedRefs["status_kind"] as? String)
            ?: (raw["status_kind"] as? String)

        val statusKind = when {
            stale -> StatusKind.UNVERIFIED
            rawStatus != null -> StatusKind.fromKind(rawStatus) ?: StatusKind.UNVERIFIED
            resolvedRefs.isNotEmpty() -> StatusKind.FACT
            else -> StatusKind.SUGGESTION
        }

        val source = (provMap?.get("source") as? String)
            ?: (resolvedRefs["source"] as? String)
            ?: refs.firstNotNullOfOrNull { it["source"] as? String }
            ?: "V4 工作台"

        val syncedAt = (provMap?.get("synced_at") as? String)
            ?: (resolvedRefs["synced_at"] as? String)
            ?: (raw["synced_at"] as? String)

        val verifiedBy = (provMap?.get("verified_by") as? String)
            ?: (resolvedRefs["verified_by"] as? String)

        return BlockProvenance(
            source = source,
            syncedAt = syncedAt,
            verifiedBy = verifiedBy,
            statusKind = statusKind,
            stale = stale,
        )
    }

    private fun parseMajorVersion(rawVersion: Any?): Int? {
        return when (rawVersion) {
            is Number -> rawVersion.toInt().takeIf { it > 0 }
            is String -> {
                val trimmed = rawVersion.trim().lowercase().removePrefix("v")
                val head = trimmed.substringBefore('.')
                head.toIntOrNull()?.takeIf { it > 0 }
            }
            else -> null
        }
    }

    private fun stringifyVersion(rawVersion: Any?): String = when (rawVersion) {
        null -> ""
        is Number -> rawVersion.toInt().toString()
        else -> rawVersion.toString().trim()
    }

    private fun degrade(
        blockId: String,
        rawType: String,
        version: String,
        raw: Map<String, Any?>,
        reason: String,
    ): ParsedUiBlock.DegradedText {
        val summary = extractFallbackSummary(raw)
        return ParsedUiBlock.DegradedText(
            blockId = blockId,
            rawType = rawType,
            version = version.ifBlank { "unknown" },
            fallbackText = sanitizePlainText(summary),
            reason = reason,
            statusKind = StatusKind.UNVERIFIED,
        )
    }

    private fun extractFallbackSummary(raw: Map<String, Any?>): String {
        val fallback = raw["fallback_text"] as? String
        if (!fallback.isNullOrBlank()) return fallback
        val props = raw["props"] as? Map<*, *>
        val title = (props?.get("title") as? String)
            ?: (props?.get("name") as? String)
            ?: (props?.get("summary") as? String)
            ?: (raw["title"] as? String)
        if (!title.isNullOrBlank()) return title
        val rawType = (raw["type"] as? String)?.takeIf { it.isNotBlank() } ?: "块"
        return "[$rawType] 请人工核对文本内容"
    }

    internal fun sanitizePlainText(text: String): String {
        return text
            .replace(Regex("<[^>]*>"), "")
            .replace(Regex("javascript:", RegexOption.IGNORE_CASE), "[blocked-js]:")
            .trim()
            .ifBlank { "（无可渲染文本）" }
    }

    internal fun jsonObjectToMap(obj: JSONObject): Map<String, Any?> {
        val map = linkedMapOf<String, Any?>()
        val keys = obj.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            map[key] = convertJsonValue(obj.get(key))
        }
        return map
    }

    private fun convertJsonValue(value: Any?): Any? = when (value) {
        null, JSONObject.NULL -> null
        is JSONObject -> jsonObjectToMap(value)
        is JSONArray -> (0 until value.length()).map { idx -> convertJsonValue(value.get(idx)) }
        else -> value
    }
}
