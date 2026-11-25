"""Credential-free immutable event fold through the pinned upstream DSH core."""
from hashlib import sha256
import subprocess
from backend.app.modules.assistant.core_adapter import CORE_COMMIT, ROOT, ENTRY, CoreConfig, CoreError, packed, _json
ENTRY_TURNS='apps/assistant-orchestrator/turns.mjs'
MANIFEST_TURNS='apps/assistant-orchestrator/turns-manifest.json'


class DshTurnsAdapter:
    def __init__(self, config):
        if type(config) is not CoreConfig:raise CoreError('assistant_turns_invalid')
        self.config=config

    def fold(self, *, admission, root_run_id, history):
        try:
            node=self.config.node_binary;manifest=ROOT/MANIFEST_TURNS
            if (not node.is_absolute() or node.is_symlink() or sha256(node.read_bytes()).hexdigest()!=self.config.node_sha256
                    or manifest.is_symlink() or sha256(manifest.read_bytes()).hexdigest()!=self.config.manifest_sha256):raise ValueError()
            data=_json(manifest.read_bytes())
            expected={ENTRY,ENTRY_TURNS}|{str(p.relative_to(ROOT)) for p in (ROOT/'third_party/dsh-superagent-prototype/src/core').glob('*.ts')}
            if data['entry']!=ENTRY_TURNS or data['core_commit']!=CORE_COMMIT or set(data['files'])!=expected:raise ValueError()
            for name,h in data['files'].items():
                p=ROOT/name
                if p.is_symlink() or sha256(p.read_bytes()).hexdigest()!=h:raise ValueError()
            payload=packed(dict(admission=admission,root_run_id=root_run_id,history=history))
            if len(payload)>1024*1024:raise ValueError()
            run=subprocess.run([str(node),'--experimental-transform-types','--disable-warning=ExperimentalWarning',str(ROOT/ENTRY_TURNS)],
                input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5,cwd=ROOT,env={'PATH':str(node.parent)})
            if run.returncode or not 0<len(run.stdout)<=1024*1024:raise ValueError()
            value=_json(run.stdout)
            if (set(value)!={'contract_version','root_work_id','turn_id','run_id','request_id','sequence','state','ticket_id','assignment_id','effects','snapshot'}
                    or type(value['contract_version']) is not int or value['contract_version']!=1
                    or value['root_work_id']!=admission['scope']['task_id']
                    or value['snapshot']['scope']!=admission['scope']
                    or value['snapshot']['request_id']!=admission['request_id']):raise ValueError()
            turns=[x for x in history if x['kind']=='followup'];last=turns[-1] if turns else None
            if (type(value['sequence']) is not int or value['sequence']!=1+len(turns)
                    or value['turn_id']!=(last['turn_id'] if last else admission['scope']['task_id'])
                    or value['run_id']!=(last['run_id'] if last else root_run_id)
                    or value['request_id']!=(last['request_id'] if last else admission['request_id'])):raise ValueError()
            ticket=value['snapshot']['tickets'][0]
            if (len(value['snapshot']['tickets'])!=1 or ticket['id']!=value['ticket_id']
                    or len(ticket['assignments'])!=value['sequence'] or ticket['assignments'][-1]['id']!=value['assignment_id']):raise ValueError()
            pending=not history or history[-1]['kind']=='followup'
            if pending:
                if (value['state']!='pending' or len(value['effects'])!=1 or value['effects'][0]['kind']!='start'
                        or value['effects'][0]['ticket_id']!=value['ticket_id']
                        or value['effects'][0]['assignment_id']!=value['assignment_id']):raise ValueError()
            elif value['state']!=history[-1]['state'] or value['effects']!=[]:raise ValueError()
            return value
        except (OSError,ValueError,TypeError,KeyError,IndexError,AttributeError,subprocess.SubprocessError):
            raise CoreError('assistant_turns_unavailable') from None
