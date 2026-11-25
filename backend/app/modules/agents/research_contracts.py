"""G1 inert, bounded evidence contracts. None of these records grants execution."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re

MAX_MANIFEST = 1024 * 1024
MAX_SOURCE = 65536
MAX_CORPUS = 4 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ResearchError(ValueError):
    pass


def deny():
    raise ResearchError("agent_research_unavailable") from None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return sha256(canonical(value)).hexdigest()


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        deny()


def text(value, limit=500, empty=False):
    if (type(value) is not str or len(value) > limit or (not empty and not value.strip())
            or "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        deny()
    return value


def identifier(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        deny()
    return value


def fingerprint(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        deny()
    return value


def integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        deny()
    return value


def instant(value):
    text(value, 64)
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            deny()
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        deny()


def strings(value, *, maximum=32, limit=500):
    if type(value) is not list or len(value) > maximum:
        deny()
    for item in value:
        text(item, limit)
    if len(value) != len(set(value)):
        deny()


def inert(value, *, max_bytes=16384, schema=False):
    """A bounded JSON tree, NOT a schema interpreter or reference resolver."""
    remaining = 2048
    def walk(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8:
            deny()
        if type(item) is dict:
            for key, child in item.items():
                text(key, 128)
                if schema and key in ("$ref", "$dynamicRef", "$recursiveRef", "$code"):
                    deny()
                walk(child, depth + 1)
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif type(item) is str:
            text(item, 8192, empty=True)
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            deny()
    walk(value, 0)
    if len(canonical(value)) > max_bytes:
        deny()
    return value


def decode(raw, limit=MAX_MANIFEST):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        deny()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                deny()
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: deny())
        return result
    except (ValueError, UnicodeError, RecursionError):
        deny()


def manifest(value):
    exact(value, ("version", "corpus_id", "revision", "sources", "candidates"))
    integer(value["version"], 1, 1)
    identifier(value["corpus_id"])
    integer(value["revision"], 1, 2**31 - 1)
    if type(value["sources"]) is not list or not 1 <= len(value["sources"]) <= 128:
        deny()
    if type(value["candidates"]) is not list or len(value["candidates"]) > 512:
        deny()
    ids = set()
    for source in value["sources"]:
        exact(source, ("artifact_id", "title", "domain", "source_kind", "content_sha256",
                       "source_version", "observed_at", "expires_at", "verification_level",
                       "limitations"))
        identifier(source["artifact_id"])
        if source["artifact_id"] in ids:
            deny()
        ids.add(source["artifact_id"])
        for name in ("title", "domain", "source_version"):
            text(source[name], 200)
        if source["source_kind"] not in ("official_document", "frontend_trace", "reviewed_excerpt", "synthetic"):
            deny()
        if source["verification_level"] not in ("unverified", "static_trace", "sample_observed"):
            deny()
        fingerprint(source["content_sha256"])
        if instant(source["observed_at"]) >= instant(source["expires_at"]):
            deny()
        strings(source["limitations"])
    candidate_ids = set()
    for candidate in value["candidates"]:
        exact(candidate, ("id", "version", "domain", "purpose", "record_grain",
                          "read_write_semantics", "source_refs", "request_schema", "result_schema",
                          "facts", "required_permissions", "unknowns"))
        identifier(candidate["id"])
        integer(candidate["version"], 1, 1)
        if candidate["id"] in candidate_ids:
            deny()
        candidate_ids.add(candidate["id"])
        for name in ("domain", "purpose", "record_grain"):
            text(candidate[name])
        if candidate["read_write_semantics"] not in ("read", "write", "unknown"):
            deny()
        refs = candidate["source_refs"]
        if type(refs) is not list or not 1 <= len(refs) <= 16:
            deny()
        seen = set()
        for ref in refs:
            exact(ref, ("artifact_id", "start", "end"))
            if ref["artifact_id"] not in ids:
                deny()
            integer(ref["start"], 0, MAX_SOURCE)
            integer(ref["end"], ref["start"] + 1, MAX_SOURCE)
            key = (ref["artifact_id"], ref["start"], ref["end"])
            if key in seen:
                deny()
            seen.add(key)
        for name in ("request_schema", "result_schema", "facts"):
            if type(candidate[name]) is not dict:
                deny()
            inert(candidate[name], schema=True)
        strings(candidate["unknowns"])
        strings(candidate["required_permissions"], maximum=32, limit=128)
    return value


def evidence(source, corpus_digest, start, end):
    """Host-derived locator. A verification label is still reference-only."""
    return {**source, "corpus_digest": corpus_digest,
            "locator": {"unit": "unicode_codepoint", "start": start, "end": end},
            "authority": "reference_only"}


def parameters(value, schema):
    """Validate only a conservative primitive subset; never evaluate a schema."""
    if type(value) is not dict:
        deny()
    inert(value, max_bytes=8192)
    prohibited = re.compile(r"(?i)(password|secret|token|cookie|authorization|headers?|"
                            r"body|url|uri|host|path|endpoint|method|tenantid|tenancyname|organizationid)\Z")
    def reject_outlets(item):
        if type(item) is dict:
            for key, child in item.items():
                if prohibited.fullmatch(key):
                    deny()
                reject_outlets(child)
        elif type(item) is list:
            for child in item:
                reject_outlets(child)
        elif type(item) is str and (re.search(r"(?i)(https?://|file://|bearer\s)", item)):
            deny()
    reject_outlets(value)
    properties = schema.get("properties")
    if schema.get("type") != "object" or type(properties) is not dict:
        if value:
            deny()
        return ["parameter_contract_unknown"]
    if any(key not in properties for key in value):
        deny()
    unknowns = []
    required = schema.get("required", [])
    if type(required) is not list or any(type(key) is not str for key in required):
        deny()
    if any(key not in value for key in required):
        unknowns.append("required_parameters_missing")
    def primitive(item, contract):
        if type(contract) is not dict:
            deny()
        kind = contract.get("type")
        if kind == "string":
            text(item, 1000, empty=True)
        elif kind == "integer":
            integer(item, -(2**53 - 1), 2**53 - 1)
        elif kind == "number":
            if type(item) not in (int, float) or not math.isfinite(item):
                deny()
        elif kind == "boolean":
            if type(item) is not bool:
                deny()
        elif kind == "array":
            if type(item) is not list or len(item) > 100:
                deny()
            if contract.get("items", {}).get("type") not in ("string", "integer", "number", "boolean"):
                return False
            for child in item:
                primitive(child, contract["items"])
        else:
            return False
        return True
    for key, item in value.items():
        if not primitive(item, properties[key]):
            unknowns.append("parameter_schema_subset_unverified")
    # Bounds/enums/expressions outside this small subset are not evaluated here.
    unknowns.append("full_request_contract_requires_host_validation")
    return sorted(set(unknowns))
