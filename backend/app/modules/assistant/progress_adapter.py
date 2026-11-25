"""Pinned pure DSH projection, distinct from the live immutable intake entry."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import subprocess
from backend.app.modules.assistant.core_adapter import CORE_COMMIT, ROOT, ENTRY, CoreError, packed, _json

PROGRESS_ENTRY = 'apps/assistant-orchestrator/progress.mjs'
PROGRESS_MANIFEST = 'apps/assistant-orchestrator/progress-manifest.json'


@dataclass(frozen=True)
class ProgressConfig:
    node_binary: Path
    node_sha256: str
    manifest_sha256: str


class DshProgressAdapter:
    def __init__(self, config):
        if type(config) is not ProgressConfig:
            raise CoreError('assistant_progress_invalid')
        self.config = config

    def project(self, *, admission, fact):
        try:
            node = self.config.node_binary
            if not node.is_absolute() or node.is_symlink() or sha256(node.read_bytes()).hexdigest() != self.config.node_sha256:
                raise ValueError()
            manifest = ROOT / PROGRESS_MANIFEST
            if manifest.is_symlink() or sha256(manifest.read_bytes()).hexdigest() != self.config.manifest_sha256:
                raise ValueError()
            data = _json(manifest.read_bytes())
            expected = {ENTRY, PROGRESS_ENTRY} | {str(p.relative_to(ROOT)) for p in
                (ROOT / 'third_party/dsh-superagent-prototype/src/core').glob('*.ts')}
            if data['core_commit'] != CORE_COMMIT or data['entry'] != PROGRESS_ENTRY or set(data['files']) != expected:
                raise ValueError()
            for name, fingerprint in data['files'].items():
                path = ROOT / name
                if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != fingerprint:
                    raise ValueError()
            payload = packed(dict(admission=admission, fact=fact))
            if len(payload) > 1024 * 1024:
                raise ValueError()
            process = subprocess.run([str(node), '--experimental-transform-types',
                '--disable-warning=ExperimentalWarning', str(ROOT / PROGRESS_ENTRY)], input=payload,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, cwd=ROOT, env={'PATH': str(node.parent)})
            if process.returncode or not 0 < len(process.stdout) <= 1024 * 1024:
                raise ValueError()
            result = _json(process.stdout)
            original = admission['snapshot']
            ticket = result['snapshot']['tickets'][0]
            if (set(result) != {'contract_version','state','ticket_id','assignment_id','snapshot','effects'}
                    or type(result['contract_version']) is not int or result['contract_version'] != 1
                    or result['effects'] != [] or result['state'] != fact['state']
                    or result['snapshot']['scope'] != original['scope']
                    or result['snapshot']['request_id'] != original['request_id']
                    or len(result['snapshot']['tickets']) != 1
                    or result['ticket_id'] != original['tickets'][0]['id']
                    or ticket['id'] != result['ticket_id']
                    or len(ticket['assignments']) != 1
                    or result['assignment_id'] != original['tickets'][0]['assignments'][0]['id']
                    or ticket['assignments'][0]['id'] != result['assignment_id']):
                raise ValueError()
            report = ticket['assignments'][0].get('report')
            end = ticket.get('ended')
            state = fact['state']
            if state == 'needs_attention':
                if result['snapshot'] != original:
                    raise ValueError()
            else:
                if not report or report['state'] != dict(completed='ready',failed='failed',cancelled='dropped',blocked='blocked')[state]:
                    raise ValueError()
                if state in ('completed', 'cancelled'):
                    if not end or end['outcome'] != ('completed' if state == 'completed' else 'dropped'):
                        raise ValueError()
                elif end:
                    raise ValueError()
            return result
        except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
            raise CoreError('assistant_progress_unavailable') from None
