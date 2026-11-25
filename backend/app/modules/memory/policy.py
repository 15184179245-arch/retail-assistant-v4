"""Policy and scope guard for business memories.
Requirements: MEM-002, R-014, R-017.

Guards restricted topics (pricing negotiations, bottom costs, supplier discounts, salary)
from becoming store-wide shared facts without authorization, and enforces strict scope
containment (no cross-store promotion, submitter separation for approval).
"""
from __future__ import annotations

import re
from typing import Any

RESTRICTED_KEYWORDS: frozenset[str] = frozenset({
    "底价",
    "进价",
    "成本价",
    "毛利",
    "返点",
    "谈判扣点",
    "批发特价",
    "回扣",
    "内部折扣",
    "协议价",
    "采购成本",
    "供货商返点",
    "薪资",
    "底薪",
    "提成",
})

RESTRICTED_PERMISSION = "memory.restricted.read"
PROMOTE_PERMISSION = "memory.promote"


def classify_content(content: str) -> dict[str, Any]:
    """Classifies memory content and identifies restricted commercial topics."""
    if not content:
        return {"is_restricted": False, "matched_keywords": [], "category": None}

    matched = [kw for kw in RESTRICTED_KEYWORDS if kw in content]
    if matched:
        return {
            "is_restricted": True,
            "matched_keywords": sorted(matched),
            "category": "commercial_pricing_or_cost" if any(
                k in matched for k in ("底价", "进价", "成本价", "毛利", "返点", "谈判扣点", "批发特价", "回扣", "内部折扣", "协议价", "采购成本", "供货商返点")
            ) else "confidential_personnel",
        }
    return {"is_restricted": False, "matched_keywords": [], "category": None}


def validate_scope_promotion(
    *,
    actor_org_id: str,
    actor_store_id: str,
    memory_org_id: str,
    memory_store_id: str,
    target_scope_type: str,
    target_scope_ref: str | None,
) -> None:
    """Enforces strict scope validation during memory promotion.
    
    Raises ValueError with standard error code on violation.
    """
    if actor_org_id != memory_org_id or actor_store_id != memory_store_id:
        raise ValueError("cross_store_promotion_forbidden")

    if target_scope_type not in {"store", "product", "category", "global"}:
        raise ValueError("invalid_scope_type")

    if target_scope_type != "store" and not (target_scope_ref or "").strip():
        raise ValueError("scope_ref_required")


def filter_content_for_reader(
    content: str,
    *,
    can_read_restricted: bool,
) -> str:
    """Return no confidential content when the reader lacks authorization."""
    if can_read_restricted or not content:
        return content

    classification = classify_content(content)
    if not classification["is_restricted"]:
        return content

    # Removing labels while retaining confidential numbers is not redaction.
    # Knowledge-domain callers omit unreadable records using the shared policy.
    return ""
