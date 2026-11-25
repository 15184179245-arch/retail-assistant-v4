"""Fixed private category worker; stdin cannot select code or database paths."""
import json
import sys
from backend.app.modules.integrations.guanjia.category_process import run


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else argv
    result=run(sys.stdin.buffer.read(16385)) if not arguments else dict(version=1,code='unavailable',result=None)
    print(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    return 0 if result['code']=='committed' else 6


if __name__=='__main__':
    raise SystemExit(main())
