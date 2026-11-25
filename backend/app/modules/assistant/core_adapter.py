"""Pinned, credential-free DSH core subprocess; never a model or tool runner."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess

CORE_COMMIT = '8c32a9dc5d72c033816c6a8110b37f40cf94fba4'
ROOT = Path(__file__).resolve().parents[4]
ENTRY = 'apps/assistant-orchestrator/main.mjs'
MANIFEST = 'apps/assistant-orchestrator/source-manifest.json'


class CoreError(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(packed(value)).hexdigest()


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CoreError('assistant_core_invalid')
            value[key] = item
        return value
    def invalid_constant(value):
        raise CoreError('assistant_core_invalid')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


@dataclass(frozen=True)
class CoreConfig:
    node_binary: Path
    node_sha256: str
    manifest_sha256: str


class DshCoreAdapter:
    def __init__(self, config: CoreConfig):
        if type(config) is not CoreConfig:
            raise CoreError('assistant_core_invalid')
        self.config = config

    def _verify(self):
        node = self.config.node_binary
        if not node.is_absolute() or node.is_symlink() or not node.is_file():
            raise CoreError('assistant_core_unavailable')
        if sha256(node.read_bytes()).hexdigest() != self.config.node_sha256:
            raise CoreError('assistant_core_unavailable')
        raw = (ROOT / MANIFEST).read_bytes()
        if sha256(raw).hexdigest() != self.config.manifest_sha256:
            raise CoreError('assistant_core_unavailable')
        manifest = _json(raw)
        if manifest['core_commit'] != CORE_COMMIT or manifest['entry'] != ENTRY:
            raise CoreError('assistant_core_unavailable')
        expected = {ENTRY} | {str(p.relative_to(ROOT)) for p in
            (ROOT / 'third_party/dsh-superagent-prototype/src/core').glob('*.ts')}
        if set(manifest['files']) != expected:
            raise CoreError('assistant_core_unavailable')
        for name, fingerprint in manifest['files'].items():
            path = ROOT / name
            if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != fingerprint:
                raise CoreError('assistant_core_unavailable')

    def apply(self, *, scope, request_id, text, title, at, snapshot=None):
        try:
            self._verify()
            payload = dict(contract_version=1, operation='open' if snapshot is None else 'inspect',
                scope=scope, request_id=request_id, text=text, title=title, at=at, snapshot=snapshot)
            raw = packed(payload)
            if len(raw) > 1024 * 1024:
                raise CoreError('assistant_core_invalid')
            result = subprocess.run([str(self.config.node_binary), '--experimental-transform-types',
                '--disable-warning=ExperimentalWarning', str(ROOT / ENTRY)], input=raw,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, cwd=ROOT,
                env={'PATH': str(self.config.node_binary.parent)})
            if result.returncode != 0 or not 0 < len(result.stdout) <= 1024 * 1024:
                raise CoreError('assistant_core_invalid')
            value = _json(result.stdout)
            if (set(value) != {'contract_version', 'core_commit', 'snapshot', 'effects', 'ticket_id', 'assignment_id'}
                    or type(value['contract_version']) is not int or value['contract_version'] != 1
                    or value['core_commit'] != CORE_COMMIT):
                raise CoreError('assistant_core_invalid')
            saved = value['snapshot']
            if saved['scope'] != scope or saved['request_id'] != request_id:
                raise CoreError('assistant_core_invalid')
            trigger, ticket, decision = (saved[name][0] for name in ('triggers', 'tickets', 'decisions'))
            if (any(len(saved[n]) != 1 for n in ('triggers', 'dispositions', 'threads', 'tickets', 'decisions'))
                    or trigger['text'] != text or trigger['actor'] != {'source':'v4','id':scope['owner_id']}
                    or ticket['title'] != title or ticket['id'] != value['ticket_id']
                    or ticket['mandate']['scope'] != text or ticket['mandate']['strength'] != 'explicit'
                    or decision['ticketId'] != ticket['id'] or decision['kind'] != 'accept'
                    or len(ticket['assignments']) != 1 or ticket['assignments'][0]['state'] != 'pending'
                    or ticket['assignments'][0]['id'] != value['assignment_id'] or ticket.get('ended')):
                raise CoreError('assistant_core_invalid')
            if snapshot is not None:
                if digest(saved) != digest(snapshot) or value['effects'] != []:
                    raise CoreError('assistant_core_invalid')
            else:
                if len(value['effects']) != 1:
                    raise CoreError('assistant_core_invalid')
                effect = value['effects'][0]
                if (set(effect) != {'id', 'kind', 'ticket_id', 'assignment_id'} or effect['kind'] != 'start'
                        or effect['ticket_id'] != ticket['id'] or effect['assignment_id'] != value['assignment_id']):
                    raise CoreError('assistant_core_invalid')
            return value
        except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
            raise CoreError('assistant_core_unavailable') from None
