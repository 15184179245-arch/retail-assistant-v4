"""New-Run-only application-level append-only public trace, not a transcript.

Shared Host hooks own authority checks and the original transaction. This module
adds no permission or replay path. Independent AuditEvent digests anchor the
mutable execution JSON; privileged database tampering remains out of scope.
"""
from copy import deepcopy
from datetime import timezone
import json
from sqlalchemy import inspect, select
from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents import public_trace_projection as projection
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentPiToolReceipt, AgentStep, AgentToolCall
from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.audit.service import record_event

KEY = "pi_public_trace_v1"
ACTION = "agent_public_trace.captured"
ABSENT = projection.ABSENT
EVENT_BYTES = 40000
MAX_CALLS = 128

class TraceError(ValueError):
    pass

def deny():
    raise TraceError("public_trace_unavailable") from None

def _time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()

def new_namespace(max_tool_calls):
    if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= MAX_CALLS:
        deny()
    return dict(version=1, max_calls=max_tool_calls,
                reserved_bytes=max_tool_calls * 2 * EVENT_BYTES, events=[])

def _namespace(value, run):
    if (type(value) is not dict or set(value) != {"version", "max_calls", "reserved_bytes", "events"}
        or type(value["version"]) is not int or value["version"] != 1
        or type(value["max_calls"]) is not int or value["max_calls"] != run.max_tool_calls
        or not 1 <= value["max_calls"] <= MAX_CALLS
        or type(value["reserved_bytes"]) is not int
        or value["reserved_bytes"] != value["max_calls"] * 2 * EVENT_BYTES
        or type(value["events"]) is not list or len(value["events"]) > value["max_calls"] * 2
        or len(_json(value)) > value["reserved_bytes"] + 1024):
        deny()
    return value

def _scope(model, run):
    return (model.organization_id == run.organization_id,
            model.store_id == run.store_id, model.run_id == run.id)

def _binding(session, run, control, native, phase):
    if (native is None or (native.organization_id, native.store_id, native.run_id,
        native.contract_version, native.model_kind, native.tool_kind, native.toolset_version)
        != (run.organization_id, run.store_id, run.id, 1, "model", "tool", run.toolset_version)):
        deny()
    frozen = control.frozen_context.get("pi_readonly_binding")
    try:
        if (repository.runtime_identity(control.frozen_context) != repository.SDK_RUNTIME
            or repository.pi_record_tool_profile(control.frozen_context.get("pi_runtime_record")) != frozen.get("profile")
            or tool_registry.pi_binding_digest(frozen) != run.toolset_version
            or tuple(frozen.get(k) for k in ("organization_id", "store_id", "run_id", "requester_id"))
                != (run.organization_id, run.store_id, run.id, run.requester_id)):
            deny()
    except (repository.AgentPersistenceError, tool_registry.ToolRegistryError,
            ValueError, TypeError, KeyError, AttributeError):
        deny()
    entries = [x for x in frozen.get("tools", []) if
        (x.get("tool_key"), x.get("version"), x.get("name")) ==
        (native.tool_key, native.tool_version, native.pi_name)]
    if len(entries) != 1:
        deny()
    definition = tool_registry.resolve_tool(native.tool_key, native.tool_version)
    input_schema = definition.input_model.model_json_schema()
    output_schema = definition.output_model.model_json_schema()
    if projection.digest(entries[0]["input_schema"]) != projection.digest(input_schema):
        deny()
    model = session.scalar(select(AgentStep).where(*_scope(AgentStep, run),
        AgentStep.id == native.model_step_id).execution_options(populate_existing=True))
    if model is None or model.kind != "model" or model.state != "succeeded":
        deny()
    sid = None
    output = None
    if phase == "result":
        step = session.scalar(select(AgentStep).where(*_scope(AgentStep, run),
            AgentStep.id == native.tool_step_id).execution_options(populate_existing=True))
        call = session.scalar(select(AgentToolCall).where(*_scope(AgentToolCall, run),
            AgentToolCall.step_id == native.tool_step_id).execution_options(populate_existing=True))
        if (native.state != "succeeded" or native.error_code is not None
            or step is None or call is None or model.sequence_no >= step.sequence_no
            or (step.kind, step.state, step.output_ref) != ("tool", "succeeded", native.output_ref)
            or (call.step_kind, call.tool_key, call.tool_version, call.input_digest)
                != ("tool", native.tool_key, native.tool_version, native.input_digest)
            or call.idempotency_key != "pi:" + projection.digest([run.id, native.native_call_id])
            or type(native.output_ref) is not str or not native.output_ref.startswith("sha256:")):
            deny()
        from . import canonical_result
        record = control.frozen_context.get('pi_runtime_record')
        if type(record) is dict and record.get('launch_profile') in canonical_result.RESULT_PROFILES:
            from .models import AgentPiToolResult
            canonical = session.scalar(select(AgentPiToolResult).where(*_scope(AgentPiToolResult,run),
                AgentPiToolResult.pi_receipt_id==native.id))
            if canonical is None: deny()
            try: envelope = canonical_result.validate_columns(canonical)
            except canonical_result.CanonicalResultError: deny()
            if (envelope['runtime_record'] != record or envelope['scope']!={'organization_id':run.organization_id,'store_id':run.store_id,'requester_id':run.requester_id,'agent_id':frozen['agent_id']}
                or envelope['binding']['tool_key']!=native.tool_key or envelope['binding']['tool_version']!=native.tool_version
                or envelope['binding']['toolset_version']!=native.toolset_version or envelope['binding']['input_digest']!=native.input_digest
                or envelope['binding']['pi_receipt_id']!=native.id
                or envelope['binding']['native_call_id']!=native.native_call_id
                or envelope['binding']['model_step_id']!=model.id or envelope['binding']['tool_step_id']!=step.id
                or envelope['output_ref']!=native.output_ref):deny()
            if canonical.result_kind=='tool_error':
                # A distinct, fixed safe schema is intentionally unsupported
                # by the existing business projection; it is NOT a successful
                # MemorySearchPage with absent fields.
                output_schema={'type':'object','additionalProperties':False,
                    'required':['code','field','hint'], 'properties':{
                        key:{'const':value} for key,value in canonical_result.ERROR_PAYLOAD.items()}}
        sid, output = step.id, native.output_ref[7:]
    elif phase != "accepted":
        deny()
    return dict(organization_id=run.organization_id, store_id=run.store_id,
        requester_id=run.requester_id, run_id=run.id, model_step_id=model.id,
        tool_step_id=sid, receipt_id=native.id, native_call_id=native.native_call_id,
        tool_key=native.tool_key, tool_version=native.tool_version,
        input_schema_sha256=projection.digest(input_schema),
        output_schema_sha256=projection.digest(output_schema),
        input_sha256=native.input_digest, output_sha256=output)

def _field_shape(rule, node):
    if type(node) is not dict:
        deny()
    if node == {"state": "absent"} or node == {"state": "withheld", "reason": "invalid_field"}:
        return
    if type(rule) is dict:
        if (set(node) != {"state", "fields", "unlisted_fields"}
            or node["state"] != "recorded" or node["unlisted_fields"] != "withheld"
            or type(node["fields"]) is not dict or set(node["fields"]) != set(rule)):
            deny()
        for k, child in rule.items():
            _field_shape(child, node["fields"][k])
    elif rule[0] == "withhold":
        if node != {"state": "withheld", "reason": rule[1]}:
            deny()
    elif rule[0] == "array":
        if (set(node) != {"state", "items"} or node["state"] != "recorded"
            or type(node["items"]) is not list or len(node["items"]) > rule[2]):
            deny()
        for item in node["items"]:
            _field_shape(rule[1], item)
    elif (set(node) != {"state", "value"} or node["state"] != "recorded"
            or not projection._scalar(rule, node["value"])):
        deny()

def _projection_shape(value, binding, phase, stamp):
    if type(value) is not dict:
        deny()
    # Derive the expected metadata without copying either original body.
    supported = (binding["tool_version"] == "1" and
        projection.SCHEMA_PINS.get(binding["tool_key"]) ==
        (binding["input_schema_sha256"], binding["output_schema_sha256"]))
    key = binding["tool_key"] if supported else None
    expected = dict(version=1, authority="projection_only", authorization_checked=False,
        binding_sha256=projection.digest(binding), run_id=binding["run_id"],
        model_step_id=binding["model_step_id"], tool_step_id=binding["tool_step_id"],
        receipt_id=binding["receipt_id"],
        native_call_sha256=projection.sha256(binding["native_call_id"].encode()).hexdigest(),
        tool_key=key, tool_version="1" if supported else None,
        projection_schema=key + "/public-trace/1" if supported else None,
        input_schema_sha256=binding["input_schema_sha256"],
        output_schema_sha256=binding["output_schema_sha256"],
        input_sha256=binding["input_sha256"], output_sha256=binding["output_sha256"],
        phase=phase, captured_at=stamp, time_semantics="record_capture_not_execution_duration",
        public_plan={"state":"not_recorded"}, source_authorization="not_checked",
        body_is_original_stream=False)
    if set(value) != set(expected) | {"arguments", "result"}:
        deny()
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items()):
        deny()
    for part, rules in (("arguments", projection.ARG_RULES), ("result", projection.RESULT_RULES)):
        node = value[part]
        if phase == "accepted" and part == "result":
            if node != {"state":"not_recorded"}: deny()
        elif not supported:
            if node != {"state":"withheld","reason":"unsupported_tool_schema"}: deny()
        else:
            _field_shape(rules[key], node)
    if len(projection.canonical(value)) > projection.MAX_PROJECTED_BYTES:
        deny()

def _audit_key(run_id, receipt_id, phase):
    return "public-trace:" + run_id + ":" + receipt_id + ":" + phase

def _anchors(session, run):
    return session.scalars(select(AuditEvent).where(
        AuditEvent.organization_id == run.organization_id,
        AuditEvent.store_id == run.store_id, AuditEvent.action == ACTION,
        AuditEvent.target_type == "agent_public_trace",
        AuditEvent.idempotency_key.startswith("public-trace:" + run.id + ":"))
        .execution_options(populate_existing=True)).all()


def verified_events(session, run, control):
    """Read integrity, not authority. Caller supplies fresh scoped rows."""
    audits = _anchors(session, run)
    if control is None or KEY not in control.frozen_context:
        if audits: deny()  # A recorded Run cannot silently become legacy.
        return None
    namespace = _namespace(control.frozen_context[KEY], run)
    if len(audits) != len(namespace["events"]):
        deny()
    anchors = {a.id: a for a in audits}
    used = set()
    out = []
    for event in namespace["events"]:
        if (type(event) is not dict or set(event) !=
            {"receipt_id", "phase", "projection", "projection_sha256", "audit_id"}
            or event["phase"] not in ("accepted", "result")):
            deny()
        identity = (event["receipt_id"], event["phase"])
        if identity in used: deny()
        used.add(identity)
        native = session.scalar(select(AgentPiToolReceipt).where(*_scope(AgentPiToolReceipt, run),
            AgentPiToolReceipt.id == event["receipt_id"]).execution_options(populate_existing=True))
        binding = _binding(session, run, control, native, event["phase"])
        stamp = _time(native.created_at if event["phase"] == "accepted" else native.updated_at)
        _projection_shape(event["projection"], binding, event["phase"], stamp)
        digest = projection.digest(event["projection"])
        audit = anchors.get(event["audit_id"])
        details = dict(version=1, run_id=run.id, phase=event["phase"],
                       binding_sha256=projection.digest(binding), projection_sha256=digest)
        if (digest != event["projection_sha256"] or audit is None
            or audit.actor_id != run.requester_id or audit.target_id != native.id
            or audit.details != details or audit.idempotency_key !=
                _audit_key(run.id, native.id, event["phase"])):
            deny()
        out.append((event, native, binding))
    return out

def capture(session, ctx, claim, receipt, decision, *, phase, result=ABSENT):
    """Call before other frozen_context updates, in the original guarded root."""
    if inspect(ctx.execution).attrs.frozen_context.history.has_changes():
        deny()
    # Host repository operations are already staged in this same transaction.
    # This is a write API, not a GET: flush establishes the original receipts.
    session.flush()
    control = repository.require_worker_claim(session, claim, expected_runtime=repository.SDK_RUNTIME)
    control = repository.execution_for(session, control.organization_id, control.store_id,
                                       control.run_id, for_update=True)
    if KEY not in control.frozen_context:
        if _anchors(session, ctx.run): deny()
        return None  # Existing unrecorded runs never acquire recording retroactively.
    run = repository.get_run(session, control.organization_id, control.store_id, control.run_id)
    native = session.scalar(select(AgentPiToolReceipt).where(*_scope(AgentPiToolReceipt, run),
        AgentPiToolReceipt.id == receipt.id).execution_options(populate_existing=True))
    if (run.id != ctx.run.id or native is None
        or native.native_call_id != decision.get("native_call_id")
        or native.tool_key != decision.get("tool_key") or native.tool_version != decision.get("version")
        or native.toolset_version != decision.get("toolset_version")
        or projection.digest(decision.get("arguments")) != native.input_digest):
        deny()
    old = verified_events(session, run, control)
    binding = _binding(session, run, control, native, phase)
    stamp = _time(native.created_at if phase == "accepted" else native.updated_at)
    body = projection.project_trace(binding=binding, expected_binding=deepcopy(binding),
        phase=phase, captured_at=stamp, arguments=decision["arguments"], result=result)
    body_sha = projection.digest(body)
    for event, _, _ in old:
        if (event["receipt_id"], event["phase"]) == (native.id, phase):
            if event["projection"] != body: deny()
            return deepcopy(body)
    namespace = deepcopy(_namespace(control.frozen_context[KEY], run))
    accepted = {e["receipt_id"] for e in namespace["events"] if e["phase"] == "accepted"}
    if phase == "accepted":
        if native.state != "accepted" or len(accepted) >= namespace["max_calls"]: deny()
    elif native.id not in accepted:
        deny()
    audit = record_event(session, actor_id=run.requester_id, organization_id=run.organization_id,
        store_id=run.store_id, action=ACTION, target_type="agent_public_trace", target_id=native.id,
        idempotency_key=_audit_key(run.id, native.id, phase),
        details=dict(version=1, run_id=run.id, phase=phase,
                     binding_sha256=projection.digest(binding), projection_sha256=body_sha),
        occurred_at=native.created_at if phase == "accepted" else native.updated_at)
    event = dict(receipt_id=native.id, phase=phase, projection=body,
                 projection_sha256=body_sha, audit_id=audit.id)
    if len(_json(event)) > EVENT_BYTES: deny()
    namespace["events"].append(event)
    _namespace(namespace, run)
    frozen = deepcopy(control.frozen_context)
    frozen[KEY] = namespace
    repository._cas_execution(session, control, values={"frozen_context": frozen},
                              expected_epoch=claim.epoch, require_live=True)
    return deepcopy(body)
