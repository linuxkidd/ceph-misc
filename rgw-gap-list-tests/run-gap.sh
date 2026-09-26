#!/bin/bash
# run-gap.sh [fixed]: on a fresh vstart cluster, leave the artifact of each
# known RGW race in a bucket of its own, run rgw-gap-list.py over every
# bucket and over rgw-orphan-list's output, and check its findings.
#
#   CEPH_BUILD=~/ceph/build [PYTHON=python3] ./run-gap.sh [fixed]
#
# The radosgw must have the test injection points of ceph/ceph#72096.  With
# 'fixed', it has the fixes too, and the races should leave almost nothing.
# Stops any vstart cluster of that build first, and leaves the new one up.
# Results and logs go to ./gap-run[-fixed]; the summary is summary.txt there.
set -u
B=${CEPH_BUILD:?set CEPH_BUILD to a ceph build directory}
T=$(cd "$(dirname "$0")" && pwd)
TOOL=${TOOL:-$T/../rgw-gap-list.py}
PY=${PYTHON:-python3}           # with boto3
OUT=$PWD/gap-run${1:+-$1}
rm -rf "$OUT"; mkdir -p "$OUT"
cd "$B" || exit 1
git -C .. log --format='%h %s' -1 > "$OUT/summary.txt" 2>/dev/null
../src/stop.sh >/dev/null 2>&1
rm -f ceph.conf keyring; rm -rf dev out
MON=1 OSD=1 MDS=0 MGR=1 RGW=1 ../src/vstart.sh -n -d > "$OUT/vstart.log" 2>&1 || { echo "vstart failed" >> "$OUT/summary.txt"; exit 1; }
for i in $(seq 60); do curl -s -o /dev/null http://localhost:8000 && break; sleep 2; done
export PATH=$B/bin:$PATH PYTHONPATH=$B/lib/cython_modules/lib.3 LD_LIBRARY_PATH=$B/lib

CEPH_CONF=$B/ceph.conf $PY "$T/seed_gap_artifacts.py" "$OUT/expected.json" "${1:-}" > "$OUT/seed.log" 2>&1
echo "seed rc=$?" >> "$OUT/summary.txt"

cd "$OUT"
CEPH_CONF=$B/ceph.conf $PY "$TOOL" -c "$B/ceph.conf" --grace 0 -I -R -vvv \
  -J "$OUT/scan.jsonl" -o "$OUT/gaps.txt" > "$OUT/scan.log" 2>&1
echo "scan rc=$?" >> "$OUT/summary.txt"

# rgw-orphan-list passes $CEPH_CONF to rados as arguments; it finds ./ceph.conf instead
(cd "$B" && env -u CEPH_CONF bash ../src/rgw/rgw-orphan-list default.rgw.buckets.data "$OUT" > "$OUT/orphan-list.log" 2>&1)
echo "orphan-list rc=$?" >> "$OUT/summary.txt"
mv "$B"/orphan-list-*.out "$OUT/orphans.txt" 2>/dev/null
rm -f "$B"/rados-*.intermediate "$B"/radosgw-admin-*.intermediate "$B"/lspools-*.error "$B"/rados-*.error "$B"/radosgw-admin-*.error "$B"/rados-*.issues
CEPH_CONF=$B/ceph.conf $PY "$TOOL" -c "$B/ceph.conf" --grace 0 -vvv -O "$OUT/orphans.txt" \
  -J "$OUT/orphans.jsonl" -o "$OUT/gaps-orphans.txt" > "$OUT/orphans.log" 2>&1
echo "orphans rc=$?" >> "$OUT/summary.txt"

$PY "$T/check_findings.py" "$OUT/expected.json" "$OUT/scan.jsonl" "$OUT/orphans.jsonl" >> "$OUT/summary.txt" 2>&1
echo "check rc=$?" >> "$OUT/summary.txt"
cat "$OUT/summary.txt"
