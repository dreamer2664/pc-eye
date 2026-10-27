# SHIP_GATE.md — pc-eye's own gate (nothing reaches main without this green)

`ship`-style contract, same rules as the agency: any `$ ` line that fails = no ship. Tier-work changes
merge on a green gate; the code that touches a human's mouse is exactly the code that should not be
merged by feel.

## Checks — run from the repo root

$ python3 tests/run_all.py --all
$ python3 -c "import server,launcher;assert server.VERSION in open('README.md').read();assert server.VERSION in open('CHANGELOG.md').read();print('version+docs in sync',server.VERSION)"
$ python3 -c "import ast;s=open('server.py').read();ast.parse(s);assert 'secrets.token_hex(16)' in s;assert 'token_hex(4)' not in s;print('token policy ok')"
$ python3 -c "
import re
src=open('server.py').read()
a=set(re.findall(r'^    \"(\w+)\":', src.split('ACTIONS = {')[1].split('}')[0], re.M))
m=set(re.findall(r'\"(\w+)\":', src.split('METHOD = {')[1].split('}')[0], re.M))
assert a==m=={'click','move','type','key','drag','scroll','paste'}, (a,m)
print('action schema == dispatch table ==',len(a),'actions')"

## What a green gate does NOT prove (say this out loud, do not paper over it)

* it does not prove a click lands on the right pixel — only `/selftest`, `coord_space.consistent` and
  `exact` on the real machine do that (this is the sentence the whole project exists around);
* it does not run on Windows here. The `windows-latest` leg of `.github/workflows/ci.yml` is what does,
  and it must be green on the PR before the merge counts as verified by CI rather than by Linux alone;
* capture-side speed is counted (`evidence/capture_count.py`), not timed, on a headless box.
