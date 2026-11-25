"""Private parent/child entry. Input selects a committed call, never a source."""
import json
import sys
from backend.app.modules.integrations.guanjia.work_source import run


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        # Never echo command arguments; a malformed caller might include secrets.
        result = {'version': 1, 'code': 'invalid_arguments', 'call_bounds': None,
                  'disclosure_ready': False, 'result': None}
    else:
        result = run(sys.stdin.buffer.read(16385))
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    return 0 if result['code'] == 'prepared' else 6


if __name__ == '__main__':
    raise SystemExit(main())
