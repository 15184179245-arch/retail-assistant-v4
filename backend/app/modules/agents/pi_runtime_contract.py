"""Exact supported Pi release identities shared by Host assembly and admission.

This table describes compatibility, never authority. Manifest hashes, current
ACL, campaign approval, budgets and transport binding remain separate gates.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PiRuntimeContract:
    version: int
    wire_protocol: str
    launch_profile: str
    family: str
    platform: str

    @property
    def online(self):
        return self.version in (2, 3)

    @property
    def streaming(self):
        return self.version == 3

    @property
    def tool_profile(self):
        return "pi-" + self.family + "-v1"


# Preserve the existing twenty identities plus explicit online2 workbench.
# Stream and stream-result are readonly
# only; no prefix matching or future-version admission is permitted.
_CONTRACTS = {}
for _platform in ("linux", "darwin"):
    for _family in ("readonly", "memory", "discovery", "source"):
        for _version, _suffix in ((1, "v1"), (2, "online-v1")):
            _profile = f"host-pi-{_family}-node24-{_platform}-{_suffix}"
            _CONTRACTS[_profile] = PiRuntimeContract(_version, f"v4.pi-{_family}/{_version}", _profile, _family, _platform)
    _profile = f"host-pi-workbench-node24-{_platform}-online-v1"
    _CONTRACTS[_profile] = PiRuntimeContract(2, "v4.pi-workbench/2", _profile, "workbench", _platform)
    for _suffix in ("online-stream-v1", "online-stream-result-v1"):
        _profile = f"host-pi-readonly-node24-{_platform}-{_suffix}"
        _CONTRACTS[_profile] = PiRuntimeContract(3, "v4.pi-readonly/3", _profile, "readonly", _platform)


def contract_for_profile(profile):
    if type(profile) is not str or profile not in _CONTRACTS:
        raise ValueError("pi_runtime_contract_invalid")
    return _CONTRACTS[profile]


def contract_for_record(record):
    if type(record) is not dict:
        raise ValueError("pi_runtime_contract_invalid")
    contract = contract_for_profile(record.get("launch_profile"))
    if (type(record.get("contract_version")) is not int
            or record["contract_version"] != contract.version
            or record.get("wire_protocol") != contract.wire_protocol):
        raise ValueError("pi_runtime_contract_invalid")
    return contract


def profiles_for_platform(base_profile):
    platform = contract_for_profile(base_profile).platform
    return tuple(key for key, value in _CONTRACTS.items() if value.platform == platform)
