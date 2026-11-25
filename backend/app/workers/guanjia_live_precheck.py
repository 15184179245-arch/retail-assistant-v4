"""Dedicated main-thread L2c3 entry: check|once, fixed protected material only.

Results retain the inner offline_host_kernel/HOLD_REAL_READ/disclosure=false.
No material hashes or IDs establish real employee confirmation or production
approval. No HTTP endpoint, background scheduler, retry or grant issuer exists.
"""
import json
import os
import sys

from backend.app.modules.integrations.guanjia import live_host as live
from backend.app.modules.integrations.guanjia import live_loader as loader


def _failure(command, code):
    # A loader failure can hide an earlier consumed nonce. Never assert zero
    # across the unknown grant/permit; this process did not start the kernel.
    return dict(version='1', command=command, environment='protected_offline_entry',
                code=code, kernel_started=False, kernel_receipt=None,
                call_bounds=None, call_bounds_scope='unavailable',
                disclosure_ready=False, real_source_status='HOLD_REAL_READ')


def _wrapped(command, inner, code=None):
    return dict(version='1', command=command, environment='protected_offline_entry',
        code=inner['code'] if code is None else code, kernel_started=True, kernel_receipt=inner,
        call_bounds=inner['call_bounds'], call_bounds_scope=inner['call_bounds_scope'],
        disclosure_ready=False, real_source_status='HOLD_REAL_READ')


def run(command):
    if type(command) is not str or command not in ('check', 'once'):
        return _failure('invalid', 'invalid_arguments')
    inner = None
    try:
        loader._process_gate()
        with loader.load_protected_runtime() as loaded:
            loaded.validate()
            # The legacy real Agent helper reads this single setting. In this
            # dedicated process only the protected material selects it; ambient
            # env cannot select a different Agent. DB authority is still checked.
            old_agent = os.environ.get('AGENT_SUBJECT_ID')
            os.environ['AGENT_SUBJECT_ID'] = loaded.runtime.permit.agent_subject_id
            try:
                inner = live.run(command, loaded.operation, runtime=loaded.runtime)
            finally:
                if old_agent is None:
                    os.environ.pop('AGENT_SUBJECT_ID', None)
                else:
                    os.environ['AGENT_SUBJECT_ID'] = old_agent
            # Keep the exact inner receipt even on commit/permission failures.
            return _wrapped(command, inner)
    except BaseException:
        if inner is not None:
            return _wrapped(command, inner, 'protected_entry_cleanup_failed')
        return _failure(command, 'protected_material_unavailable')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Deliberately no argparse error echo of untrusted secret-bearing arguments.
    if type(argv) is not list or len(argv) != 1 or argv[0] not in ('check', 'once'):
        result = _failure('invalid', 'invalid_arguments')
        status = 2
    else:
        result = run(argv[0])
        status = 0 if result['code'] in ('checked_offline_host', 'structure_captured') else 6
    print(json.dumps(result, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
