# rgw-gap-list.py tests

- `test_rgw_gap_list.py`: offline tests of the script's parsing and
  classification helpers.  They need no cluster, and run on Python 3.6 and
  later:

  ```
  python3 test_rgw_gap_list.py ../rgw-gap-list.py
  ```

- `run-gap.sh`: an end to end test on a vstart cluster.  It leaves the
  artifact of each known RGW race in a bucket of its own
  (`seed_gap_artifacts.py`), runs `rgw-gap-list.py` over every bucket and over
  `rgw-orphan-list`'s output, and checks that each artifact is found, with
  the expected class and most likely cause, and that the clean buckets have
  no findings (`check_findings.py`).

  ```
  CEPH_BUILD=~/ceph/build PYTHON=~/venv/bin/python ./run-gap.sh
  ```

  The races are arranged with test injection points in radosgw, so the build
  must have them: ceph/ceph#72096, or a branch with it.  Build the `vstart`
  and `ceph-diff-sorted` targets.  `PYTHON` needs boto3.

  With `fixed`, the build has the fixes too ( ceph/ceph#72097 to #72103 and
  #72109 ): the races should then leave nothing but a completed upload whose
  meta object remains, which the completion record makes harmless, and the
  head the test removes by hand.

  The script stops any vstart cluster of that build first, and leaves the
  new one up.  Results go to `./gap-run[-fixed]`.
