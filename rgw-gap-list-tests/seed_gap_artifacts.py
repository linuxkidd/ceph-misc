#!/usr/bin/env python3
"""
seed_gap_artifacts.py OUT.json [fixed]: on a vstart cluster whose radosgw
has the overwrite-race injection points and none of the fixes, leave each
race's artifact in a bucket of its own, and write what rgw-gap-list should
report for each bucket to OUT.json.

With 'fixed', the radosgw has the fixes too: the same races leave nothing,
but for a completed upload whose meta delete failed, which the completion
record makes harmless, and the head this script removes by hand.

Phase 1's artifacts need GC to have run; phase 2's must still be in GC.
"""
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import os

import boto3
from botocore.config import Config

MB = 1024 * 1024
OBJ = 8 * MB        # past the 4 MiB head, so the object has a tail
DELAY = 6
expected = []


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and r.returncode:
        raise RuntimeError(f'{cmd}: {r.stderr.decode()[-800:]}')
    return r.stdout.decode()


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


@contextmanager
def conf(who, **opts):
    try:
        for k, v in opts.items():
            sh(f'ceph config set {who} {k} {v}')
        time.sleep(2)
        yield
    finally:
        for k in opts:
            sh(f'ceph config rm {who} {k}', check=False)
        time.sleep(2)


def inject(point, delay):
    return conf('client', rgw_inject_delay_sec=delay, rgw_inject_delay_pattern=point)


class Request(threading.Thread):
    def __init__(self, name, fn):
        super().__init__(name=name, daemon=True)
        self.fn, self.result, self.error = fn, None, None

    def run(self):
        try:
            self.result = self.fn()
        except Exception as e:
            self.error = e

    def outcome(self):
        self.join()
        return self.error or self.result


def client():
    # vstart's default user, unless the environment names another
    return boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT', 'http://localhost:8000'),
                        aws_access_key_id=os.environ.get('S3_ACCESS_KEY', '0555b35654ad1656d804'),
                        aws_secret_access_key=os.environ.get(
                            'S3_SECRET_KEY', 'h7GhxuBLTrlhVUyxSPUKUV8r/2EI4ngqJxD7iBdBYLhwluN30JaT3Q=='),
                        region_name='default',
                        config=Config(read_timeout=600, retries={'total_max_attempts': 1}))


c = client()


def bucket(name):
    c.create_bucket(Bucket=name)
    return name


FIXED = len(sys.argv) > 2 and sys.argv[2] == 'fixed'
FIXED_EXPECT = {
    'gap-atrisk': ('inconsistency', 'completed_upload_open', ['mp-meta-left'], 'obj', 'scan'),
    'gap-nohead': ('inconsistency', 'listed_without_head', ['stale-entry'], 'obj', 'scan'),
}


def expect(b, cls, check, causes, key=None, via='scan'):
    """causes: any of these may rank first"""
    if FIXED:
        if any(e['bucket'] == b for e in expected):
            return
        if b in FIXED_EXPECT:
            cls, check, causes, key, via = FIXED_EXPECT[b]
            expected.append({'bucket': b, 'key': key, 'class': cls, 'check': check,
                             'causes': causes, 'via': via})
            expected.append({'bucket': b, 'clean': True, 'via': 'orphans'})
        else:
            clean(b)
        return
    expected.append({'bucket': b, 'key': key, 'class': cls, 'check': check,
                     'causes': causes, 'via': via})


def clean(b):
    for via in ('scan', 'orphans'):
        expected.append({'bucket': b, 'clean': True, 'via': via})


def put(b, k, fill, size=OBJ):
    return c.put_object(Bucket=b, Key=k, Body=fill.encode() * size)['ETag']


def mpu(b, k, fills='pq'):
    up = c.create_multipart_upload(Bucket=b, Key=k)['UploadId']
    parts = []
    for n, f in enumerate(fills, 1):
        r = c.upload_part(Bucket=b, Key=k, UploadId=up, PartNumber=n, Body=f.encode() * (5 * MB))
        parts.append({'PartNumber': n, 'ETag': r['ETag']})
    return up, parts


def complete(b, k, up, parts, **kw):
    return c.complete_multipart_upload(Bucket=b, Key=k, UploadId=up,
                                       MultipartUpload={'Parts': parts}, **kw)


def meta_left(b, k):
    """a completion whose meta object delete fails after the head write"""
    up, parts = mpu(b, k)
    with conf('client', rgw_debug_inject_mp_meta_delete_err=5):
        complete(b, k, up, parts)
    return up, parts


# ---- phase 1: artifacts that need GC to have run ----

def s_loss_retry():
    b = bucket('gap-loss-retry')
    up, parts = meta_left(b, 'obj')
    complete(b, 'obj', up, parts)   # a client's retry
    expect(b, 'data_loss', 'missing_data', ['mp-meta-left'], key='obj')


def s_loss_abort():
    b = bucket('gap-loss-abort')
    up, parts = meta_left(b, 'obj')
    c.abort_multipart_upload(Bucket=b, Key='obj', UploadId=up)
    expect(b, 'data_loss', 'missing_data', ['mp-meta-left'], key='obj')


def s_copyself():
    b = bucket('gap-copyself')
    put(b, 'obj', 'a')
    with inject('copy_obj_before_write_meta', DELAY):
        copy = Request('copy', lambda: c.copy_object(
            Bucket=b, Key='obj', CopySource={'Bucket': b, 'Key': 'obj'},
            MetadataDirective='REPLACE', Metadata={'copied': 'yes'}))
        copy.start()
        time.sleep(DELAY / 3)
        put(b, 'obj', 'b')
        log(f'copy onto itself: {copy.outcome()}')
    expect(b, 'data_loss', 'missing_data', ['copy-self'], key='obj')
    expect(b, 'leak', 'orphan_tail', ['delete-race', 'cond-delete', 'copy-self'], via='orphans')


def s_leak_complete():
    b = bucket('gap-leak-complete')
    put(b, 'obj', 'a')
    up, parts = mpu(b, 'obj')
    with inject('write_meta_before_head_write', DELAY):
        writer = Request('put', lambda: put(b, 'obj', 'b'))
        writer.start()
        time.sleep(DELAY / 3)
        comp = Request('complete', lambda: complete(b, 'obj', up, parts))
        comp.start()
        writer.outcome()
        log(f'losing completion: {comp.outcome()}')
    expect(b, 'leak', 'orphan_parts', ['lost-complete'], via='orphans')


def s_leak_delete(cond):
    b = bucket('gap-leak-conddel' if cond else 'gap-leak-delete')
    old = put(b, 'obj', 'a')
    kw = {'IfMatch': old} if cond else {}
    with inject('delete_obj_before_head_delete', DELAY):
        delete = Request('delete', lambda: c.delete_object(Bucket=b, Key='obj', **kw))
        delete.start()
        time.sleep(DELAY / 3)
        put(b, 'obj', 'b')
        log(f'delete racing put ( cond={cond} ): {delete.outcome()}')
    expect(b, 'leak', 'orphan_tail', ['delete-race', 'cond-delete', 'copy-self'], via='orphans')


def losing_copy(b):
    put(b, 'src', 's')
    put(b, 'dst', 'a')
    with inject('write_meta_before_head_write', DELAY):
        writer = Request('put', lambda: put(b, 'dst', 'b'))
        writer.start()
        time.sleep(DELAY / 3)
        copy = Request('copy', lambda: c.copy_object(Bucket=b, Key='dst',
                                                     CopySource={'Bucket': b, 'Key': 'src'}))
        copy.start()
        writer.outcome()
        log(f'losing copy: {copy.outcome()}')


def s_leak_copy():
    b = bucket('gap-leak-copy')
    losing_copy(b)
    c.delete_object(Bucket=b, Key='src')
    expect(b, 'leak', 'orphan_tail', ['lost-copy'], via='orphans')


def s_lc_abort():
    b = bucket('gap-lc-abort')
    with conf('client', rgw_lc_debug_interval=10):
        c.put_bucket_lifecycle_configuration(Bucket=b, LifecycleConfiguration={'Rules': [
            {'ID': 'abort', 'Status': 'Enabled', 'Filter': {'Prefix': ''},
             'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 1}}]})
        up, parts = mpu(b, 'obj')
        time.sleep(15)   # past one debug "day"
        with inject('complete_mp_after_head_write', 40):
            comp = Request('complete', lambda: complete(b, 'obj', up, parts))
            comp.start()
            time.sleep(8)
            log('lc process: ' + sh(f'radosgw-admin lc process --bucket {b} 2>&1', check=False)[-200:])
            log(f'completion held across lifecycle: {comp.outcome()}')
    expect(b, 'data_loss', 'missing_data', ['lc-abort', 'mp-meta-left'], key='obj')


# ---- phase 2: artifacts that must still be in GC, and the rest ----

def s_atrisk():
    b = bucket('gap-atrisk')
    meta_left(b, 'obj')
    expect(b, 'at_risk', 'completed_upload_open', ['mp-meta-left'], key='obj')


def s_pending(retry):
    b = bucket('gap-pending-retry' if retry else 'gap-pending-abort')
    up, parts = meta_left(b, 'obj')
    if retry:
        complete(b, 'obj', up, parts)
    else:
        c.abort_multipart_upload(Bucket=b, Key='obj', UploadId=up)
    expect(b, 'pending_loss', 'queued_for_gc', ['mp-meta-left'], key='obj')


def s_latent_copy():
    b = bucket('gap-latent-copy')
    losing_copy(b)
    expect(b, 'latent_leak', 'unheld_reference', ['lost-copy'])


def s_refused():
    b = bucket('gap-refused')
    up, parts = mpu(b, 'obj')
    with inject('write_meta_before_head_write', DELAY):
        writer = Request('put', lambda: put(b, 'obj', 'b'))
        writer.start()
        time.sleep(DELAY / 3)
        comp = Request('complete', lambda: complete(b, 'obj', up, parts, IfNoneMatch='*'))
        comp.start()
        writer.outcome()
        log(f'refused completion: {comp.outcome()}')
    expect(b, 'inconsistency', 'part_entries_missing', ['refused-complete'], key='obj')


def stalled_put(b, key):
    with conf('osd', rgw_pending_bucket_index_op_expiration=5):
        with inject('write_meta_before_head_write', 20):
            writer = Request('put', lambda: put(b, key, 'n'))
            writer.start()
            time.sleep(8)
            listed = [(o['Key'], o['ETag']) for o in c.list_objects_v2(Bucket=b).get('Contents', [])]
            log(f'listing during the stalled PUT of {key}: {listed}')
            log(f'stalled put: {writer.outcome()}')
    listed = [(o['Key'], o['ETag']) for o in c.list_objects_v2(Bucket=b).get('Contents', [])]
    log(f'listing after it: {listed}')


def s_stale():
    b = bucket('gap-stale')
    put(b, 'obj', 'a')
    stalled_put(b, 'obj')
    expect(b, 'inconsistency', 'stale_entry', ['stalled-write', 'stale-entry'], key='obj')


def s_unlisted():
    b = bucket('gap-unlisted')
    put(b, 'other', 'a', size=1024)
    stalled_put(b, 'new')
    expect(b, 'inconsistency', 'unlisted_head', ['stalled-write'], via='orphans')


def s_nohead():
    b = bucket('gap-nohead')
    put(b, 'obj', 'a', size=1024)
    marker = json.loads(sh(f'radosgw-admin bucket stats --bucket {b}'))['marker']
    sh(f"rados -p {os.environ.get('DATA_POOL', 'default.rgw.buckets.data')} rm {marker}_obj")
    expect(b, 'inconsistency', 'listed_without_head', ['stale-entry'], key='obj')


def s_clean():
    b = bucket('gap-clean')
    put(b, 'small', 'a', size=1024)
    put(b, 'large', 'b')
    up, parts = mpu(b, 'mp')
    complete(b, 'mp', up, parts)
    c.copy_object(Bucket=b, Key='large-copy', CopySource={'Bucket': b, 'Key': 'large'})
    c.copy_object(Bucket=b, Key='mp-copy', CopySource={'Bucket': b, 'Key': 'mp'})
    c.copy_object(Bucket=b, Key='large', CopySource={'Bucket': b, 'Key': 'large'},
                  MetadataDirective='REPLACE', Metadata={'x': 'y'})
    mpu(b, 'in-progress')
    clean(b)
    x = bucket('gap-clean-cross')
    c.copy_object(Bucket=x, Key='from-clean', CopySource={'Bucket': b, 'Key': 'large'})
    clean(x)
    v = bucket('gap-clean-versioned')
    c.put_bucket_versioning(Bucket=v, VersioningConfiguration={'Status': 'Enabled'})
    put(v, 'obj', 'a')
    put(v, 'obj', 'b')
    c.delete_object(Bucket=v, Key='obj')
    put(v, 'gone', 'c', size=1024)
    c.delete_object(Bucket=v, Key='gone')
    clean(v)
    lc = bucket('gap-clean-lc')
    c.put_bucket_lifecycle_configuration(Bucket=lc, LifecycleConfiguration={'Rules': [
        {'ID': 'abort', 'Status': 'Enabled', 'Filter': {'Prefix': ''},
         'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7}}]})
    up, parts = mpu(lc, 'mp')
    complete(lc, 'mp', up, parts)
    clean(lc)


def main():
    out = sys.argv[1]
    phase1 = [s_loss_retry, s_loss_abort, s_copyself, s_leak_complete,
              lambda: s_leak_delete(False), lambda: s_leak_delete(True), s_leak_copy, s_lc_abort]
    phase2 = [s_atrisk, lambda: s_pending(True), lambda: s_pending(False), s_latent_copy,
              s_refused, s_stale, s_unlisted, s_nohead, s_clean]
    for s in phase1:
        log(f'seeding {getattr(s, "__name__", s)}')
        s()
    log('gc process --include-all')
    sh('radosgw-admin gc process --include-all')
    for s in phase2:
        log(f'seeding {getattr(s, "__name__", s)}')
        s()
    with open(out, 'w') as f:
        json.dump(expected, f, indent=1)
    log(f'wrote {len(expected)} expectations to {out}')


main()
