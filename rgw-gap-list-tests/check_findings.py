#!/usr/bin/env python3
"""
check_findings.py EXPECTED.json SCAN.jsonl ORPHANS.jsonl: compare what
rgw-gap-list reported with what seed_gap_artifacts.py left behind
"""
import json
import sys


def load(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


expected = json.load(open(sys.argv[1]))
got = {'scan': load(sys.argv[2]), 'orphans': load(sys.argv[3])}
failed = 0


def matches(e, f):
    return (f['bucket'] == e['bucket'] and f['class'] == e['class'] and f['check'] == e['check']
            and (e.get('key') is None or f.get('key') == e['key']))


for e in expected:
    found = [f for f in got[e['via']] if f['bucket'] == e['bucket']]
    if e.get('clean'):
        verdict = 'PASS' if not found else 'FAIL'
        detail = 'no findings' if not found else 'unexpected: ' + '; '.join(
            f"{f['class']}/{f['check']} {f.get('key', '')}" for f in found)
    else:
        hits = [f for f in found if matches(e, f)]
        if not hits:
            verdict, detail = 'FAIL', f"no {e['class']}/{e['check']}; got " + (
                '; '.join(f"{f['class']}/{f['check']}" for f in found) or 'nothing')
        else:
            causes = hits[0].get('causes') or []
            top = causes[0]['cause'] if causes else None
            verdict = 'PASS' if top in e['causes'] else 'FAIL'
            detail = f"{e['class']}/{e['check']}, top cause {top} ({causes[0]['confidence'] if causes else '-'})"
            if verdict == 'FAIL':
                detail += f", expected one of {e['causes']}"
    failed += verdict != 'PASS'
    print(f"{verdict:5} {e['via']:7} {e['bucket']:20} {detail}")

for via, findings in got.items():
    for f in findings:
        if not any(e['via'] == via and not e.get('clean') and matches(e, f) for e in expected) \
                and not any(e['via'] == via and e.get('clean') and e['bucket'] == f['bucket'] for e in expected):
            failed += 1
            print(f"EXTRA {via:7} {f['bucket']:20} {f['class']}/{f['check']} {f.get('key', '')} "
                  f"{[c['cause'] for c in f.get('causes', [])][:3]}")

print(f"{failed} problem(s)")
sys.exit(1 if failed else 0)
