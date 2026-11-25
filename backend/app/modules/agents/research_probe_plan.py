"""Inert G1 proposal construction. NO send, auth, tool registration or broker.

A future G2 consumer must revalidate this pin and independently authorize its
own operation. A document's required_permissions or read label is NOT a permit.
"""
from . import research_contracts as c
from . import research_catalog as catalog

WRITE_CONSTRAINTS = (
    "business_writes_not_executed_in_g1",
    "future_host_must_check_current_chuanhuojian_category_and_zero_stock",
    "purchase_price_and_inventory_quantity_changes_forbidden",
    "recheck_before_send_unknown_eligibility_means_no_write",
)


def propose(caller, requester, agent, config, *, candidate_id, corpus_digest, goal, parameters):
    with catalog.access(caller, requester, agent, config) as (identity, data, docs):
        c.identifier(candidate_id)
        c.fingerprint(corpus_digest)
        c.text(goal, 500)
        if corpus_digest != config.manifest_sha256:
            c.deny()
        candidate = next((x for x in data["candidates"] if x["id"] == candidate_id), None)
        if candidate is None:
            c.deny()
        unknowns = set(candidate["unknowns"])
        unknowns.update(c.parameters(parameters, candidate["request_schema"]))
        unknowns.add("current_execution_authority_not_granted_by_research")
        unknowns.add("host_broker_and_full_contract_validation_required")
        if candidate["read_write_semantics"] != "read":
            unknowns.add("read_semantics_not_established")
        if candidate["read_write_semantics"] == "write":
            unknowns.add("business_write_not_enabled_in_g1")
        refs = [c.evidence(docs[r["artifact_id"]][0], corpus_digest, r["start"], r["end"])
                for r in candidate["source_refs"]]
        result = {"version": 1, "authority": "proposal_only", "status": "not_executed",
            "candidate_id": candidate_id, "candidate_digest": c.digest(candidate),
            "corpus_digest": corpus_digest, "identity_view_digest": identity,
            "goal": goal, "read_write_semantics": candidate["read_write_semantics"],
            "normalized_parameters": c.decode(c.canonical(parameters), 8192),
            "request_schema_digest": c.digest(candidate["request_schema"]),
            "result_schema_digest": c.digest(candidate["result_schema"]),
            "required_permissions": candidate["required_permissions"],
            "permissions_authority": "document_claim_only",
            "bounds": {"max_requests": 1, "max_response_bytes": 65536, "timeout_seconds": 5},
            "bounds_authority": "proposal_only_not_execution_configuration",
            "verification_plan": ["revalidate_corpus_and_candidate", "current_host_and_source_authorization",
                "validate_exact_request_result_contract", "verify_scope_and_field_visibility",
                "verify_pagination_identity_counts_and_coverage", "persist_independent_outcome_evidence"],
            "evidence": refs, "unknowns": sorted(unknowns), "write_constraints": list(WRITE_CONSTRAINTS)}
        result["proposal_digest"] = c.digest(result)
    return result


def revalidate(caller, requester, agent, config, proposal):
    """Reject an old/changed/forged proposal; success STILL is not execution."""
    try:
        if type(proposal) is not dict:
            c.deny()
        fresh = propose(caller, requester, agent, config, candidate_id=proposal["candidate_id"],
            corpus_digest=proposal["corpus_digest"], goal=proposal["goal"],
            parameters=proposal["normalized_parameters"])
        if fresh != proposal:
            c.deny()
        return fresh
    except Exception:
        c.deny()
