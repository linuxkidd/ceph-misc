#!/usr/bin/env python

"""
By: Michael J. Kidd (linuxkidd)
Last Revision: 2026-09-26
Version: 3.0

Now using aio_stat()

Performs a Rados Gateway Gap analysis

Over the years, there have been a couple of bugs which resulted in backing
user data being deleted for Ceph RGW S3 objects.  It's rare, but has happend.

There is a shell script tool available ( that I also wrote ) but it has a few
drawbacks:
- It must wait for a complete `radosgw-admin bucket radoslist` to complete
- It must wait for a complete `rados ls` on the bucket data pool to complete
- Then it compares the listings looking for gaps in `rados ls`
- It's prone to false positives which can be tedious for mere mortals to
  verify.

-- This can take a LONG time on large clusters, and it doesn't generate any
   usable output until both lists are complete and the comparison begins.

This python version attempts to address these shortcoming in the following way:
1. It runs on a per-bucket basis and generates usable output for each bucket
   along the way.
2. When ran without any bucket constraints ( either bucket list, or list file ),
   this script maintains state synchronization using dedicated objects in the
   bucket index pool (by default).
3. Since the state is synchronized via Ceph RADOS... multiple instances can be
   running in parallel, even across different hosts!
4. This script can also be ran with the '-r' option to generate a report of
   current running hosts, and state per bucket. ( add '-j' for json output )
5. This script can verify its own results by passing the '-x' flag followed
   by the gap-list-result file from a previous run.
6. It classifies what it finds, and names the known RGW issues that can leave
   each artifact behind ( see Classification below ).

Usage can be had by passing '--help' to the script.

## Classification:
Each finding is written as one JSON object per line to the findings file
( gap-list-findings.###.jsonl, or '-J' ), with a class:
- data_loss:     a listed object's manifest names RADOS objects that are gone.
- pending_loss:  that data is still there, but queued in GC with no other
                 reference to it, so GC will delete it.
- at_risk:       a completed multipart upload is still open.  Aborting it,
                 retrying its completion, or lifecycle's
                 AbortIncompleteMultipartUpload frees the object's data.
- inconsistency: the bucket index and the objects disagree: a listed key
                 with no head, an entry that lists an older object ( '-I' ),
                 a head no entry lists ( '-O' ), or an open upload whose parts
                 have no index entries.
- leak:          RADOS objects nothing references ( '-O' ).
- latent_leak:   a tail object keeps a reference no head holds, so it will
                 outlive the object ( '-R' ).

and the candidate causes: upstream tracker issues, each with a confidence and
the evidence for it.  Causes the cluster's release cannot have are left out
( the release comes from 'ceph versions', or '--release' ).  Causes whose fix a
build carries can be named with '--fixed'; with '--fixed-since', a finding
newer than that date whose causes are all fixed is flagged 'after_fix'.

The checks, and what they cost:
- Always: the gap check itself, plus a look at the heads of objects with
  missing data, the bucket's open multipart uploads ( read from its index
  with an omap prefix filter; skip with '-U' ), and a snapshot of the GC queue
  ( skip with '-G' ).
- '-I': compares every index entry's ETag with its head's.  One xattr read per
  object, in '-T' threads.
- '-R': reads the refcount of every tail object.  One xattr read per tail
  object, in '-T' threads.
- '-O <file>': classifies the orphans in an 'rgw-orphan-list' output file,
  instead of scanning buckets.

## Tips:
- I recommend using '-vv' the first time ( or any time ) to see what is going
  on.
- Get a report of current host activity and bucket scan states by passing '-r'
- You can force a rescan by passing '-a #' with a value in seconds to consider
  the prior scan stale ( after the # seconds value ) - use 1 to force rescan
  everything.
- You can wipe out the synchronized state data by passing '-d'
- Passing any bucket constraints ( -b or -l ) ignores the synchronized state!!
  -- NOTE -- Read the above line again.
- The bucket data pool(s) and the sync state pool can be overridden with '-p'
  and '-s', respectively.  Without '-p', the data pools of every placement
  target and storage class of the zone are used.
  -- NOTE -- If you don't use the same pools on all instances of this script,
  the synchronized state will not work well ( or at all ).
- To verify the results of multiple script runs ( whether parallel on a single
  host, or across multiple hosts) by catting all their results into a single
  file, then providing that combined file with the '-x' parameter.
- You can limit the objects to only those matching a given prefix using the
  '-m' parameter.
- Findings younger than '--grace' seconds ( default 1 hour ) are skipped, as
  they may belong to requests still in flight.

## Known Issues:
- If two separate instances attempt to start processing the same bucket in
  a very narrow window ( < 50ms, but the exact value depends on a lot of
  variables ), they may both succeed in starting the process, instead of one
  winning the race to push the sync object omap update and blocking the other.
  This has no real impact aside from doubling any gap objects listed in the
  results and having two threads processing the same bucket.
- '-R' resolves a reference held by a copy in another bucket only when that
  bucket is scanned by the same process.

Enjoy!
"""

import argparse
import calendar
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import json
import logging
import os
import re
import rados
import signal
import struct
import subprocess
import sys
import time

log_levels = [ 50, 30, 20, 10 ]
fs = "\xfe"
mypid = os.getpid()
myhost = os.uname().nodename
bucket_count = 0
bucket_count_idx = 0
missing_count = 0
shard_count = 1
sync_object_name = "rgw-gap-list-sync-object"
report_every_x_object_count = 10000

# RGW object attributes and bucket index flags the classification reads
xattr_idtag = "user.rgw.idtag"
xattr_tail_tag = "user.rgw.tail_tag"
xattr_etag = "user.rgw.etag"
xattr_mp_completion_tag = "user.rgw.mp_completion_tag"
xattr_refcount = "refcount"
dirent_flag_delete_marker = 0x4

# a multipart part or its stripes, after the namespace prefix:
# <key>.<upload id>.<part number>[_<stripe>]; upload ids hold no dots
mp_name_re = re.compile(r'^(.*)\.([^.]+)\.(\d+)(?:_\d+)?$')
mp_meta_re = re.compile(r'^(.*)\.([^.]+)\.meta$')
mp_index_re = re.compile(r'^_multipart_(.*)\.([^.]+)\.(meta|\d+)$')

release_majors = { "reef": 18, "squid": 19, "tentacle": 20, "umbrella": 21, "main": 21 }

"""
The known RGW issues that leave artifacts this script can find.  'min' and
'max' bound the major versions that have the bug; 'fix' is the ceph/ceph pull
request that fixes it.
"""
issues = {
    "mp-meta-left": { "tracker": 80896, "fix": 72103,
        "what": "a completion's meta object outlived its head write (an RGW crash, a failed meta delete, or a completion lock that lapsed), and an abort or a retried completion then freed the parts" },
    "lc-abort": { "tracker": 80895, "fix": 72099,
        "what": "lifecycle's AbortIncompleteMultipartUpload freed the parts of an upload while it was being completed" },
    "abort-race": { "tracker": None, "fix": 60771, "max": 19,
        "what": "AbortMultipartUpload freed the parts of an upload while it was being completed (it takes the completion lock from tentacle on)" },
    "copy-self": { "tracker": 80900, "fix": 72101,
        "what": "a copy of an object onto itself raced an overwrite, and wrote back a manifest whose tail the overwrite had freed" },
    "ix-fail": { "tracker": 80902, "fix": 72098, "min": 21,
        "what": "a failed bucket index completion undid a write that had taken effect" },
    "stale-entry": { "tracker": 80894, "fix": 72097,
        "what": "bucket index completions applied out of order left an older entry in place" },
    "stalled-write": { "tracker": 80903, "fix": 72102,
        "what": "a write that stalled past rgw_pending_bucket_index_op_expiration lost its index entry" },
    "refused-complete": { "tracker": 80907, "fix": 72109,
        "what": "a refused conditional CompleteMultipartUpload dropped its parts' index entries" },
    "lost-complete": { "tracker": 80897, "fix": 72098,
        "what": "a CompleteMultipartUpload that lost the race for the head left its parts behind" },
    "delete-race": { "tracker": 80898, "fix": 72100, "min": 20,
        "what": "a DeleteObject racing an overwrite removed the new head and left its tail" },
    "cond-delete": { "tracker": 80898, "fix": 72100, "min": 20,
        "what": "a conditional DeleteObject racing an overwrite removed the new head and left its tail" },
    "lost-copy": { "tracker": 80899, "fix": 72098,
        "what": "a CopyObject that lost the race for the head kept its references on the source's tail" },
    "dedup": { "tracker": 80901, "fix": None, "min": 20,
        "what": "dedup raced a write, delete or copy of the object" },
}
confidence_order = { "high": 0, "medium": 1, "low": 2 }
finding_classes = [ "data_loss", "pending_loss", "at_risk", "inconsistency", "leak", "latent_leak" ]

cluster_majors = set()
fixed_prs = set()
fixed_since = None
gc_index = None
gc_min_wait = 7200
findings = None
finding_counts = {}
cause_counts = {}
filtered_counts = {}
xattr_pool = None
refs_needed = {}
refs_carried = {}
bucket_stats = {}

def signal_handler(sig, frame):
    print(f'Received {sig}, Terminating')
    sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def admin_command(*parts):
    return ["radosgw-admin", "-c", args.conf] + list(parts)

def run_admin(*parts, quiet=False):
    """Run radosgw-admin, returning its output as text, or None if it failed."""
    res = subprocess.run(admin_command(*parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        if not quiet:
            err = res.stderr.decode("utf-8", "replace").strip()[-300:]
            logger.error(f"radosgw-admin {' '.join(parts)} failed ( {res.returncode} ): {err}")
        return None
    return res.stdout.decode("utf-8", "surrogateescape")

def iter_json_array(stream, chunk_size=1048576):
    """
    Yield the elements of a JSON array read from a text stream one at a time,
    so a large listing never has to fit in memory.
    """
    decoder = json.JSONDecoder()
    buf = ''
    pos = 0
    eof = False
    started = False
    while True:
        while True:
            while pos < len(buf) and buf[pos] in ' \t\r\n,':
                pos += 1
            if pos < len(buf) or eof:
                break
            chunk = stream.read(chunk_size)
            eof = not chunk
            buf, pos = buf[pos:] + chunk, 0
        if pos >= len(buf):
            return
        if not started:
            if buf[pos] != '[':
                raise ValueError(f"expected a JSON array, found {buf[pos:pos+40]!r}")
            started = True
            pos += 1
            continue
        if buf[pos] == ']':
            return
        try:
            obj, end = decoder.raw_decode(buf, pos)
        except ValueError:
            if eof:
                raise
            chunk = stream.read(chunk_size)
            eof = not chunk
            buf, pos = buf[pos:] + chunk, 0
            continue
        yield obj
        pos = end

def stream_admin_array(*parts):
    """Yield the elements of the JSON array a radosgw-admin command prints."""
    proc = subprocess.Popen(admin_command(*parts), bufsize=1048576, shell=False,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="surrogateescape")
    try:
        for obj in iter_json_array(stream):
            yield obj
    finally:
        proc.stdout.close()
        proc.wait()

def parse_pool(spec):
    """'pool' or 'pool:namespace', as zone placement pools are written"""
    name, _, ns = spec.partition(":")
    return name, ns

def zone_pools():
    """
    The zone's pools: every placement target's data pools ( the default
    placement's STANDARD pool first ), its extra pools ( multipart meta
    objects ), and the index pool of each placement target.
    """
    out = run_admin("zone", "get")
    if out is None:
        return None
    zone = json.loads(out)
    data_pools = []
    extra_pools = []
    index_pools = {}
    placements = sorted(zone.get("placement_pools", []), key=lambda p: p.get("key") != "default-placement")
    for placement in placements:
        val = placement.get("val", {})
        index_pools[placement.get("key")] = val.get("index_pool")
        classes = val.get("storage_classes", {})
        for sc in sorted(classes, key=lambda c: c != "STANDARD"):
            pool = classes[sc].get("data_pool")
            if pool and pool not in data_pools:
                data_pools.append(pool)
        extra = val.get("data_extra_pool")
        if extra and extra not in extra_pools:
            extra_pools.append(extra)
    return data_pools, extra_pools, index_pools

class CephClusterConnection:
    """
    A context manager to handle connecting to and disconnecting from a
    Ceph RADOS cluster, ensuring resources are cleaned up properly.
    """
    def __init__(self, ceph_conf='', pool_names=[], sync_pool='', extra_pools=[], index_pools={}):
        self.ceph_conf = ceph_conf
        self.cluster = None
        self.pool_names = pool_names
        self.pool_ioctl = []
        self.extra_pools = extra_pools
        self.extra_ioctl = []
        self.index_pools = index_pools
        self.index_ioctl = {}
        self.sync_pool = sync_pool
        self.sync_ioctl = None
        self.in_flight = deque()
        self.shard_count = 1

    def open_pool(self, spec):
        name, ns = parse_pool(spec)
        ioctx = self.cluster.open_ioctx(name)
        if ns:
            ioctx.set_namespace(ns)
        return ioctx

    def __enter__(self):
        """Called when entering the 'with' block."""
        self.cluster = rados.Rados(conffile=self.ceph_conf)
        try:
            self.cluster.connect()
            logger.info("Successfully connected to the Ceph cluster.")

            try:
                self.sync_ioctl = self.cluster.open_ioctx(self.sync_pool)
            except rados.ObjectNotFound:
                logger.critical(f"Sync Pool {self.sync_pool} not present.  Exiting.")
                exit(1)

            if len(self.pool_names)>0:
                for pool_name in self.pool_names:
                    try:
                        self.pool_ioctl.append(self.open_pool(pool_name))
                    except rados.ObjectNotFound:
                        logger.critical(f"Pool {pool_name} not present, skipping.")
                    else:
                        if re.search(r"\.non-ec$",pool_name):
                            logger.info(f"Pool {pool_name}, adding namespace 'multipart'")
                            self.pool_ioctl.append(self.cluster.open_ioctx(pool_name))
                            self.pool_ioctl[len(self.pool_ioctl)-1].set_namespace('multipart')

            if len(self.pool_ioctl)==0:
                logger.critical(f"None of the listed pools exist!  Exiting! Tried: {self.pool_names}")
                exit(1)

            for pool_name in self.extra_pools:
                try:
                    self.extra_ioctl.append(self.open_pool(pool_name))
                except rados.ObjectNotFound:
                    logger.error(f"Extra pool {pool_name} not present, skipping.")
                else:
                    if re.search(r"\.non-ec$",pool_name):
                        self.extra_ioctl.append(self.cluster.open_ioctx(pool_name))
                        self.extra_ioctl[len(self.extra_ioctl)-1].set_namespace('multipart')

            return self
        except rados.Error as e:
            logger.critical(f"Failed to connect to the Ceph cluster: {e}")
            raise RuntimeError(f"Failed to connect to the Ceph cluster: {e}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'with' block, ensuring safe shutdown."""
        if self.cluster:
            self.rm_sync_state()
            for ioctx in self.pool_ioctl + self.extra_ioctl + [i for i in self.index_ioctl.values() if i]:
                try:
                    ioctx.close()
                except:
                    pass
            try:
                self.sync_ioctl.close()
            except:
                pass
            try:
                self.cluster.shutdown()
            except:
                pass
            logger.info("Connection to the Ceph cluster closed.")

    def null_cb(*args):
        return

    def aio_stat_object(self, object_name="", idx=None):
        if not self.cluster:
            logger.critical("Cluster is not connected.")
            raise RuntimeError("Cluster is not connected.")

        idxstart = 0
        idxend = len(self.pool_ioctl)

        # iterate over each pool attempting to stat the object.
        comp = []
        if idx is not None:
            if idx == 0:
                idxend = 1
            else:
                idxstart = 1

        for myidx in range(idxstart,idxend):
            try:
                comp.append(self.pool_ioctl[myidx].aio_stat(object_name,self.null_cb))
            except Exception as e:
                logger.error(f"[Exception] While attempting to stat {object_name}: {e}")


        if len(comp) > 0:
            return comp

        return None

    def index_ioctx(self, placement):
        """the ioctx of a placement target's index pool, or None"""
        if placement not in self.index_ioctl:
            spec = self.index_pools.get(placement) or self.index_pools.get("default-placement")
            ioctx = None
            if spec:
                try:
                    ioctx = self.open_pool(spec)
                except rados.Error as e:
                    logger.error(f"Index pool {spec} of placement {placement}: {e}")
            self.index_ioctl[placement] = ioctx
        return self.index_ioctl[placement]

    def find(self, oid, ioctxs=None):
        """stat an object in the given pools ( the data pools by default ): (ioctx, size, mtime) or None"""
        for ioctx in (ioctxs if ioctxs is not None else self.pool_ioctl):
            try:
                size, mtime = ioctx.stat(oid)
            except rados.ObjectNotFound:
                continue
            except rados.Error as e:
                logger.error(f"stat of {oid} failed: {e}")
                continue
            return ioctx, size, time.mktime(mtime)
        return None

    def delete_sync_objects(self):
        logger.critical(f"Deleting sync objects...")
        try:
            self.sync_ioctl.stat(sync_object_name)
        except rados.ObjectNotFound:
            pass
        else:
            bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            self.shard_count = bucket_metadata_header["shard_count"]
            logger.info(f"Deleting primary sync object: {sync_object_name}")
            self.sync_ioctl.remove_object(sync_object_name)


        for i in range(self.shard_count):
            try:
                self.sync_ioctl.stat(f"{sync_object_name}.{i}")
            except rados.ObjectNotFound:
                pass
            else:
                logger.info(f"Deleting sync object: {sync_object_name}.{i}")
                self.sync_ioctl.remove_object(f"{sync_object_name}.{i}")

        logger.critical(f"Finished deleting sync objects.")

    def populate_sync_objects(self,shard_count=1):
        self.shard_count=shard_count
        try:
            self.sync_ioctl.stat(sync_object_name)
        except rados.ObjectNotFound:
            logger.info(f"Populating sync objects...")
            logger.debug(f"Creating primary sync object: {sync_object_name}")
            sync_data = { "bucket_count": bucket_count, "shard_count": shard_count, "epoch": round(time.time(),3) }
            self.sync_ioctl.write_full(sync_object_name,json.dumps(sync_data).encode("utf-8"))
        else:
            ceph.touch_sync_state(bucket_name='', rados_count=0)
            logger.debug(f"Found primary sync object: {sync_object_name}")
            bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            running_hosts = self.get_running_hosts()
            logger.info(f'Request {shard_count} shards, existing {bucket_metadata_header["shard_count"]}')
            if shard_count <= ( bucket_metadata_header["shard_count"] * 1.5 ) or running_hosts:
                self.shard_count = bucket_metadata_header["shard_count"]
                shard_count = self.shard_count
            else:
                logger.info("No running hosts, and shard count is too low, resetting sync objects.")
                self.delete_sync_objects()
                self.populate_sync_objects(shard_count)
                return None


        for i in range(shard_count):
            try:
                self.sync_ioctl.stat(f"{sync_object_name}.{i}")
                logger.debug(f"Found sync object: {sync_object_name}.{i}")
            except rados.ObjectNotFound:
                logger.debug(f"Creating sync object: {sync_object_name}.{i}")
                self.sync_ioctl.write_full(f"{sync_object_name}.{i}",b'')

        logger.info("Finished populating sync objects...")

    def hash_bucketname(self,bucketname):
        digest = hashlib.sha256(bucketname.encode("utf-8")).digest()
        return int.from_bytes(digest,byteorder="big") % self.shard_count

    def touch_sync_state(self, bucket_name='', rados_count=0, gap_count=0):
        with rados.WriteOpCtx() as write_op:
            sync_state = { "epoch": round(time.time(),3), "current_bucket": bucket_name, "rados_count": rados_count, "gap_count": gap_count, "bucket_counter": bucket_count_idx, "total_buckets": bucket_count }
            self.sync_ioctl.set_omap(write_op,(f"{myhost}.{mypid}",),( json.dumps(sync_state), ))
            self.sync_ioctl.operate_write_op(write_op, sync_object_name)

    def rm_sync_state(self):
        with rados.WriteOpCtx() as write_op:
            try:
                self.sync_ioctl.remove_omap_keys(write_op, (f"{myhost}.{mypid}",))
                self.sync_ioctl.operate_write_op(write_op, sync_object_name)
            except:
                pass

    def start_bucket(self,bucket_name):
        shardid = self.hash_bucketname(bucket_name)
        logger.debug(f"Setting bucket start metadata to sync shard {shardid}")
        sync_metadata = { "hostname": myhost, "pid": mypid, "rados_obj_count": 0, "gap_count": 0, "start_time": round(time.time(),3), "end_time": 0 }
        with rados.WriteOpCtx() as write_op:
            # Set bucket metadata
            self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(sync_metadata), ))
            self.sync_ioctl.operate_write_op(write_op, f"{sync_object_name}.{shardid}")
        self.touch_sync_state(bucket_name,0,0)
        return True

    def get_bucket_meta(self,bucket_name):
        shardid = self.hash_bucketname(bucket_name)
        logger.info(f"Getting bucket metadata from shard {shardid}")
        with rados.ReadOpCtx() as read_op:
            omap_iter, ret = self.sync_ioctl.get_omap_vals_by_keys(read_op, (bucket_name,))
            try:
                self.sync_ioctl.operate_read_op(read_op, f"{sync_object_name}.{shardid}")
            except rados.ObjectNotFound:
                logger.debug(f"Sync Object {sync_object_name}.{shardid} not found.")
                return False
            results = list(omap_iter)
            if results:
                rkey, rval = results[0]
                logger.debug(f"Found bucket metadata: {rval}")
                return rval
            else:
                logger.debug(f"Bucket metadata not present.")
                return False

    def end_bucket(self,bucket_name,rados_count,gap_count,class_counts=None):
        shardid = self.hash_bucketname(bucket_name)
        logger.info(f"Setting bucket end metadata for {bucket_name} to sync shard {shardid}")
        bucket_meta = self.get_bucket_meta(bucket_name)
        if bucket_meta:
            bucket_meta = json.loads(bucket_meta)
            bucket_meta["end_time"] = round(time.time(),3)
            bucket_meta["gap_count"] = gap_count
            bucket_meta["rados_obj_count"] = rados_count
            bucket_meta["total_time_secs"] = round(bucket_meta["end_time"] - bucket_meta["start_time"],3)
            bucket_meta["findings"] = class_counts or {}
            logger.debug(f"Bucket meta: {bucket_meta}")
            with rados.WriteOpCtx() as write_op:
                # Set bucket metadata
                self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(bucket_meta), ))
                self.sync_ioctl.operate_write_op(write_op, f"{sync_object_name}.{shardid}")
            self.touch_sync_state(bucket_name,rados_count,gap_count)
            return True
        else:
            logger.error(f"Bucket start metadata for {bucket_name} is missing from shard {shardid}")
            return False

    def is_bucket_scanning(self,bucket_name):
        running_hosts = self.get_running_hosts(bucket_keyed=True)
        if bucket_name in running_hosts:
            return running_hosts[bucket_name]
        else:
            return False

    def get_running_hosts(self,bucket_keyed=False):
        running_hosts = {}
        with rados.ReadOpCtx() as read_op:
            try:
                bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            except rados.ObjectNotFound:
                logger.critical(f"ERROR: {sync_object_name} object not found.  Exiting.")
                exit(1)

            omap_iterator, ret = self.sync_ioctl.get_omap_vals( read_op, start_after="", filter_prefix="", max_return=100000, omap_key_type=bytes )
            if not ret==0:
                logger.critical("Failed to retrieve omap data.")
                exit(1)

            self.sync_ioctl.operate_read_op(read_op, sync_object_name)

            for key, value in omap_iterator:
                if key.decode("ascii") == f"{myhost}.{mypid}":
                    continue
                key_parts = key.decode('ascii').strip().split(".")
                rhost = key_parts[0]
                rpid = key_parts[len(key_parts)-1]
                status = json.loads(value.decode("ascii"))
                if not rhost in running_hosts and not bucket_keyed:
                    running_hosts[rhost] = {}
                if bucket_keyed:
                    status['hostname']=rhost
                    status['pid']=rpid
                    running_hosts[status['current_bucket']] = status
                else:
                    running_hosts[rhost][rpid]=json.loads(value.decode("ascii"))

        return running_hosts

    def get_buckets_state(self):
        bucket_state = {}
        with rados.ReadOpCtx() as read_op:
            for i in range(self.shard_count):
                omap_iterator, ret = self.sync_ioctl.get_omap_vals( read_op, start_after="", filter_prefix="", max_return=1000000, omap_key_type=bytes )
                if not ret==0:
                    logger.critical("Failed to retrieve omap data.")
                    exit(1)

                try:
                    self.sync_ioctl.operate_read_op(read_op, f"{sync_object_name}.{i}")
                except rados.ObjectNotFound:
                    logger.error(f"Missing Sync Object {sync_object_name}.{i}")
                else:
                    for key, value in omap_iterator:
                        bucket_name = key.decode("utf-8").strip()
                        bucket_state[bucket_name] = json.loads(value.decode("utf-8"))
        return bucket_state

    def generate_report(self):
        logger.info(f"Generating bucket metadata report")
        try:
            self.sync_ioctl.stat(sync_object_name)
        except rados.ObjectNotFound:
            logger.critical("No primary sync object found.  Exiting")
            exit(1)
        else:
            logger.debug(f"Found primary sync object: {sync_object_name}")
            bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            self.shard_count = bucket_metadata_header["shard_count"]

        running_hosts = self.get_running_hosts()
        bucket_state = self.get_buckets_state()
        if args.json:
            print(json.dumps({"active_hosts": running_hosts,"bucket_state": bucket_state}))
        else:
            if len(running_hosts):
                print("\nRunning Hosts:")
                total_processed=0
                for host,data in running_hosts.items():
                    host_processed=0
                    print(f"  {host}")
                    for pid,status in data.items():
                        dt = datetime.fromtimestamp(status['epoch']).strftime('%Y-%m-%d %H:%M:%S')
                        print(f"    PID: {pid}, Bucket: {status['current_bucket']}, Rados Count: {status['rados_count']}, Gap Count: {status['gap_count']}, Bucket Counter: {status['bucket_counter']}, Last Updated: {dt}")
                        host_processed += status['bucket_counter']
                        total_processed += status['bucket_counter']
                    print(f"  Host processed: {host_processed}")
                print(f"Total processed: {total_processed}")
            else:
                print("No active hosts.")

            if len(bucket_state):
                print("\nBucket State:")
                for bucket_name,data in bucket_state.items():
                    print(f"  {bucket_name}:: Rados Count: {data['rados_obj_count']}, ", end="")
                    if data['end_time']:
                        dt = datetime.fromtimestamp(data['end_time']).strftime('%Y-%m-%d %H:%M:%S')
                        hum = seconds_to_human(data['total_time_secs'])
                        found = ", ".join(f"{n} {c}" for c, n in data.get('findings', {}).items() if n)
                        found = f" Findings: {found}." if found else ""
                        print(f"Last Scan Completed: {dt} in {hum}, found {data['gap_count']} gaps.{found}")
                    elif data['start_time']:
                        dt = datetime.fromtimestamp(data['start_time']).strftime('%Y-%m-%d %H:%M:%S')
                        state = "never completed, process not running"
                        if data['hostname'] in running_hosts and str(data['pid']) in running_hosts[data['hostname']]:
                            state = f"active on host {data['hostname']} (pid: {data['pid']})"
                        print(f"Scan Started: {dt} ({state})")
            else:
                print("  No bucket state available.")

        exit(0)

# End class CephClusterConnection

def seconds_to_human(secs):
    secs = float(secs)
    days = int( secs / 86400 )
    hours = int( ( secs - ( days * 86400 ) ) / 3600 )
    minutes = int ( ( secs - ( days * 86400 ) - ( hours * 3600 ) ) / 60 )
    seconds = ( secs - ( days * 86400 ) - ( hours * 3600 ) - ( minutes * 60 ) )
    human = []
    if days:
        human.append(f"{days} d")
    if hours:
        human.append(f"{hours} h")
    if minutes:
        human.append(f"{minutes} m")
    if seconds > 0:
        human.append(f"{seconds} s")
    return " ".join(human)

def iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)) if ts else None

def parse_time(text):
    """the epoch of a '2026-09-26T10:00:00...' or '2026-09-26 10:00:00...' UTC time, or None"""
    try:
        return calendar.timegm(time.strptime(text[:19].replace("T", " "), '%Y-%m-%d %H:%M:%S'))
    except (ValueError, TypeError):
        return None

"""
Object names and attributes
"""
def parse_oid(oid):
    """
    Split an RGW data object name into (marker, kind, key, upload id).  kind
    is 'head', 'part' ( a multipart part's first stripe ), 'mp_shadow' ( its
    further stripes ), 'shadow' ( an atomic object's tail ), 'meta' ( a
    multipart upload's meta object ) or 'other'.  Bucket markers hold no
    underscore.
    """
    marker, sep, rest = oid.partition("_")
    if not sep:
        return oid, "other", None, None
    for ns, kind in (("_multipart_", "part"), ("_shadow_", "shadow")):
        if rest.startswith(ns):
            name = rest[len(ns):]
            if kind == "part":
                m = mp_meta_re.match(name)
                if m:
                    return marker, "meta", m.group(1), m.group(2)
            m = mp_name_re.match(name)
            if m:
                return marker, ("part" if kind == "part" else "mp_shadow"), m.group(1), m.group(2)
            return marker, kind, None, None
    return marker, "head", None, None

def split_key(keystr):
    """radoslist names a version as 'name[instance]'"""
    m = re.match(r'^(.*)\[([^\[\]]*)\]$', keystr)
    if m:
        return m.group(1), m.group(2)
    return keystr, ""

def key_oid(name, instance=""):
    """the part of a head's object name after its bucket marker and '_'"""
    if instance and instance != "null":
        return f"_:{instance}_{name}"
    if name.startswith("_"):
        return "_" + name
    return name

def get_xattr(ioctx, oid, name):
    """an xattr's value as text without its trailing NUL, or None"""
    try:
        return ioctx.get_xattr(oid, name).rstrip(b"\0").decode("utf-8", "surrogateescape")
    except rados.Error:
        return None

def head_info(oid):
    """what the classification needs from a head object, or None if it is gone"""
    found = ceph.find(oid)
    if not found:
        return None
    ioctx, size, mtime = found
    return { "mtime": mtime, "size": size,
             "idtag": get_xattr(ioctx, oid, xattr_idtag),
             "tail_tag": get_xattr(ioctx, oid, xattr_tail_tag),
             "etag": get_xattr(ioctx, oid, xattr_etag) }

def decode_refcount(bl):
    """cls_refcount's obj_refcount: ( {tag: bool}, {retired tags} ), tags without NUL"""
    def tag(pos):
        n, = struct.unpack_from("<I", bl, pos)
        return bl[pos+4:pos+4+n].rstrip(b"\0").decode("utf-8", "surrogateescape"), pos + 4 + n
    version, _, length = struct.unpack_from("<BBI", bl, 0)
    end = 6 + length
    count, = struct.unpack_from("<I", bl, 6)
    pos = 10
    refs = {}
    for _ in range(count):
        t, pos = tag(pos)
        refs[t] = bool(bl[pos])
        pos += 1
    retired = set()
    if version >= 2 and pos < end:
        count, = struct.unpack_from("<I", bl, pos)
        pos += 4
        for _ in range(count):
            t, pos = tag(pos)
            retired.add(t)
    return refs, retired

def read_refcount(oid, ioctx=None):
    """an object's references: ( refs, retired ), or None if it has no refcount attribute"""
    for ctx in ([ioctx] if ioctx is not None else ceph.pool_ioctl):
        try:
            return decode_refcount(ctx.get_xattr(oid, xattr_refcount))
        except rados.ObjectNotFound:
            continue
        except rados.Error:
            return None
        except struct.error:
            logger.error(f"Cannot decode the refcount of {oid}")
            return None
    return None

def survives_gc(tags, refcount):
    """
    Whether an object survives GC putting each tag, as cls_refcount does: a
    tag drops its own reference, or else the implicit one of the object's
    first writer.
    """
    if refcount is None:
        refs, retired = { "": True }, set()
    else:
        refs, retired = dict(refcount[0]), set(refcount[1])
    for t in tags:
        if t in retired:
            continue
        if t in refs:
            del refs[t]
        elif "" in refs:
            del refs[""]
        else:
            continue
        retired.add(t)
        if not refs:
            return False
    return True

"""
Findings
"""
def applies(issue):
    """whether any release the cluster runs can have the issue"""
    if not cluster_majors:
        return True
    lo = issue.get("min", 0)
    hi = issue.get("max", 999)
    return any(lo <= m <= hi for m in cluster_majors)

def cause(name, confidence, why):
    return (name, confidence, why)

def emit(cls, check, bucket, key=None, oids=None, causes=(), evidence=None, hint=None, when=None, **extra):
    """write one finding, with its candidate causes ranked and filtered by release"""
    rec = { "class": cls, "check": check, "bucket": bucket }
    if key is not None:
        rec["key"] = key
    rec.update(extra)
    if oids:
        rec["oids"] = oids[:100]
        if len(oids) > 100:
            rec["oid_count"] = len(oids)
    if when:
        rec["time"] = iso(when)
    ranked = []
    seen = set()
    for name, confidence, why in sorted(causes, key=lambda c: confidence_order[c[1]]):
        issue = issues[name]
        if name in seen or not applies(issue):
            continue
        seen.add(name)
        c = { "cause": name, "confidence": confidence, "what": issue["what"] }
        if issue["tracker"]:
            c["tracker"] = f"https://tracker.ceph.com/issues/{issue['tracker']}"
        if issue["fix"]:
            c["fix"] = f"https://github.com/ceph/ceph/pull/{issue['fix']}"
            if issue["fix"] in fixed_prs:
                c["fixed_here"] = True
        if why:
            c["evidence"] = why
        ranked.append(c)
    rec["causes"] = ranked
    if fixed_since and when and when > fixed_since and ranked and all(c.get("fixed_here") for c in ranked):
        rec["after_fix"] = True
    if evidence:
        rec["evidence"] = evidence
    if hint:
        rec["hint"] = hint
    findings.write(json.dumps(rec) + "\n")
    findings.flush()
    finding_counts[cls] = finding_counts.get(cls, 0) + 1
    if ranked:
        top = ranked[0]["cause"]
        cause_counts[top] = cause_counts.get(top, 0) + 1
    where = f"s3://{bucket}/{key}" if key is not None else bucket
    likely = f", likely {ranked[0]['cause']} ({ranked[0]['confidence']})" if ranked else ""
    logger.error(f"[{cls.upper()}] {where}: {check}{likely}")
    return rec

def filtered(reason):
    filtered_counts[reason] = filtered_counts.get(reason, 0) + 1

def young(ts):
    return ts is not None and ts > time.time() - args.grace

"""
Per bucket state
"""
class BucketScan:
    def __init__(self, name):
        self.name = name
        self.start = time.time()
        self.stats = None
        self.marker = None
        self.uploads = {}       # upload id -> { key, meta, parts } from the index
        self.named = set()      # open uploads whose parts a listed head names
        self.lc_mp = None
        self.class_counts = {}
        self.refcount_jobs = []

    def load(self, refresh=False):
        """the bucket's stats: from the run's preloaded stats, or its own 'bucket stats'"""
        self.stats = None if refresh else bucket_stats.get(self.name)
        if self.stats is None:
            out = run_admin("bucket", "stats", f"--bucket={self.name}")
            if out is None:
                return False
            self.stats = json.loads(out)
        self.marker = self.stats.get("marker")
        return True

    def count(self, cls):
        self.class_counts[cls] = self.class_counts.get(cls, 0) + 1

    def has_mp_expiration(self):
        """whether the bucket's lifecycle has an AbortIncompleteMultipartUpload rule"""
        if self.lc_mp is None:
            out = run_admin("lc", "get", f"--bucket={self.name}", quiet=True)
            self.lc_mp = False
            if out:
                try:
                    lc = json.loads(out)
                except ValueError:
                    lc = {}
                for rule in lc.get("rule_map", []):
                    mp = rule.get("rule", {}).get("mp_expiration", {})
                    if isinstance(mp, dict) and (mp.get("days") or mp.get("date")):
                        self.lc_mp = True
        return self.lc_mp

def index_objects(stats):
    """the names of a bucket's current index shard objects"""
    base = f".dir.{stats['id']}"
    shards = int(stats.get("num_shards", 0))
    gen = int(stats.get("index_generation", 0))
    if shards == 0:
        return [base]
    if gen:
        return [f"{base}.{gen}.{i}" for i in range(shards)]
    return [f"{base}.{i}" for i in range(shards)]

def list_open_uploads(scan, retry=True):
    """
    Read the bucket's open multipart uploads and their parts' entries from
    its index: only keys in the multipart namespace, filtered by the OSDs.
    """
    if str(scan.stats.get("index_type", "Normal")) != "Normal":
        return
    placement = str(scan.stats.get("placement_rule", "")).split("/")[0] or "default-placement"
    ioctx = ceph.index_ioctx(placement)
    if ioctx is None:
        return
    for oid in index_objects(scan.stats):
        start = ""
        while True:
            keys = []
            with rados.ReadOpCtx() as read_op:
                it, ret = ioctx.get_omap_vals(read_op, start, "_multipart_", 1000, omap_key_type=bytes)
                try:
                    ioctx.operate_read_op(read_op, oid)
                except rados.ObjectNotFound:
                    if retry and scan.load(refresh=True):
                        # resharded since the stats were read
                        scan.uploads = {}
                        return list_open_uploads(scan, retry=False)
                    logger.error(f"Index object {oid} of {scan.name} not found")
                    break
                keys = [k.decode("utf-8", "surrogateescape") for k, v in it]
            for k in keys:
                m = mp_index_re.match(k)
                if not m:
                    continue
                u = scan.uploads.setdefault(m.group(2), { "key": m.group(1), "meta": False, "parts": set() })
                if m.group(3) == "meta":
                    u["meta"] = True
                else:
                    u["parts"].add(int(m.group(3)))
            if len(keys) < 1000:
                break
            start = keys[-1]
            try:
                start.encode("utf-8")
            except UnicodeEncodeError:
                logger.error(f"Cannot page past index key {start!r} of {oid}")
                break
    open_count = sum(1 for u in scan.uploads.values() if u["meta"])
    if open_count:
        logger.info(f"{scan.name} has {open_count} open multipart upload(s)")

def read_meta(scan, key, upload):
    """an open upload's meta object: its part numbers, mtime and completion record, or None"""
    oid = f"{scan.marker}__multipart_{key}.{upload}.meta"
    found = ceph.find(oid, ceph.extra_ioctl + ceph.pool_ioctl)
    if not found:
        return None
    ioctx, size, mtime = found
    parts = set()
    start = ""
    while True:
        with rados.ReadOpCtx() as read_op:
            it, ret = ioctx.get_omap_vals(read_op, start, "part.", 1000)
            ioctx.operate_read_op(read_op, oid)
            keys = [k for k, v in it]
        for k in keys:
            try:
                parts.add(int(k[len("part."):]))
            except ValueError:
                pass
        if len(keys) < 1000:
            break
        start = keys[-1]
    return { "oid": oid, "mtime": mtime, "parts": parts,
             "record": get_xattr(ioctx, oid, xattr_mp_completion_tag) }

"""
The gap check, one S3 object at a time
"""
def new_group(scan, bucket, key):
    return { "scan": scan, "bucket": bucket, "key": key, "pending": 0, "closed": False,
             "oids": [], "present": [], "missing": [], "errors": [],
             "uploads": set(), "open_uploads": set(), "gc": {} }

def note_oid(scan, group, oid):
    group["oids"].append(oid)
    marker, kind, key, upload = parse_oid(oid)
    # radoslist lists an open upload's meta object too; only parts name an upload
    if upload and kind in ("part", "mp_shadow"):
        group["uploads"].add(upload)
        u = scan.uploads.get(upload)
        if u and u["meta"]:
            group["open_uploads"].add(upload)
            scan.named.add(upload)
    if gc_index and oid in gc_index:
        group["gc"][oid] = gc_index[oid]

def close_group(group):
    if group is None:
        return
    group["closed"] = True
    if group["pending"] == 0:
        finalize_group(group)

def op_done(op_obj, state):
    group = op_obj.get("group")
    if group is None:
        return
    group[state].append(op_obj["objname"])
    group["pending"] -= 1
    if group["closed"] and group["pending"] == 0:
        finalize_group(group)

def check_aio_result(op_obj):
    results = []
    for comp in op_obj['comp']:
        comp.wait_for_complete()
        results.append(comp.get_return_value())

    if results.count(-2) == len(op_obj['comp']):
        if op_obj['poolidx'] == 0 and len(ceph.pool_ioctl) > 1:
            logger.info(f"{op_obj['objname']} not found in default pool, checking remaining pools.")
            op_obj['comp'] = ceph.aio_stat_object(op_obj['objname'],1)
            op_obj['poolidx'] = 1
            return op_obj
        else:
            outfile.write(f"{op_obj['bucket']} MISSING {op_obj['objname']}\n")
            logger.error(f"[NOT FOUND] {op_obj['bucket']} MISSING {op_obj['objname']}")
            op_done(op_obj, "missing")
            return 1

    if 0 not in results:
        logger.error(f"[ERROR] {op_obj['bucket']} stat of {op_obj['objname']} failed: {results}")
        op_done(op_obj, "errors")
        return None

    op_done(op_obj, "present")
    return None

def finalize_group(group):
    scan = group["scan"]
    heads = [o for o in group["oids"] if parse_oid(o)[1] == "head"]
    head_oid = heads[0] if heads else None
    if group["missing"]:
        classify_missing(scan, group, head_oid)
    elif group["gc"]:
        check_pending_loss(scan, group, head_oid)
    for upload in sorted(group["open_uploads"]):
        check_open_upload(scan, group, head_oid, upload)
    if args.refcount:
        tails = [o for o in group["present"] if parse_oid(o)[1] != "head"]
        if tails:
            scan.refcount_jobs.append(xattr_pool.submit(refcount_job, head_oid, tails))

def gc_tags(group):
    return sorted({ t for entries in group["gc"].values() for t, _ in entries })

def multipart_causes(scan, group, keep_tail, by_abort, abort_lag=None, requeued=False):
    """
    abort_lag: seconds from the head write to the abort that queued the parts
    for GC, when GC still holds them.  An abort within minutes raced the
    completion; a later one found an upload the completion had left open.
    requeued: GC holds the parts the head names under another head's tag,
    as when a retried completion replaces the head it wrote before.
    """
    uploads = ", ".join(sorted(group["uploads"]))
    lc = scan.has_mp_expiration()
    raced = abort_lag is not None and abs(abort_lag) < 600
    left = (by_abort and not raced) or group["open_uploads"] or (requeued and not keep_tail)
    lag = f"; the abort came {int(abort_lag)} s after the head write" if abort_lag is not None else ""
    why = f"look in the RGW ops or access log for AbortMultipartUpload, or another CompleteMultipartUpload, of upload {uploads} after the object's mtime{lag}"
    if requeued and not keep_tail:
        why = f"a write of this key queued for GC the parts its head names, as a retried completion of upload {uploads} does"
    causes = [
        cause("mp-meta-left", "high" if left else "medium", why),
        cause("lc-abort", ("high" if raced else "medium") if lc else "low",
              ("the bucket has an AbortIncompleteMultipartUpload rule" if lc else "the bucket has no AbortIncompleteMultipartUpload rule now") + lag),
        cause("abort-race", "high" if raced else "medium", f"look for AbortMultipartUpload of upload {uploads} while it was being completed{lag}"),
        cause("ix-fail", "low", None),
        cause("dedup", "low", None),
    ]
    if keep_tail:
        causes.append(cause("copy-self", "high", "the head's tail tag differs from its ID tag: it was rewritten keeping an older tail, as a copy onto itself does"))
    return causes

def atomic_causes(keep_tail):
    if keep_tail:
        return [ cause("copy-self", "high", "the head's tail tag differs from its ID tag: it was rewritten keeping an older tail, as a copy onto itself does"),
                 cause("dedup", "medium", None), cause("ix-fail", "low", None) ]
    return [ cause("ix-fail", "medium", None), cause("dedup", "medium", None),
             cause("copy-self", "low", None) ]

def index_entry(bucket, name, instance):
    """a key's bucket index entry, from 'bi list', or None"""
    out = run_admin("bi", "list", f"--bucket={bucket}", f"--object={name}", quiet=True)
    if not out:
        return None
    try:
        entries = json.loads(out)
    except ValueError:
        return None
    for e in entries:
        if e.get("type") not in ("plain", "instance"):
            continue
        ent = e.get("entry", {})
        if ent.get("name") == name and ent.get("instance", "") == instance:
            return ent
    return None

def classify_missing(scan, group, head_oid):
    name, instance = split_key(group["key"])
    missing = group["missing"]
    if head_oid is None or head_oid in missing:
        entry = index_entry(scan.name, name, instance)
        if entry is None:
            filtered("deleted during the scan")
            return
        if int(entry.get("flags", 0)) & dirent_flag_delete_marker:
            filtered("delete marker")
            return
        if entry.get("pending_map"):
            filtered("index op in flight")
            return
        meta = entry.get("meta", {})
        when = parse_time(meta.get("mtime", ""))
        if young(when):
            filtered("younger than the grace period")
            return
        emit("inconsistency", "listed_without_head", group["bucket"], group["key"], oids=[head_oid] if head_oid else None,
             causes=[ cause("stale-entry", "medium", "a delete, put, delete sequence whose completions arrived out of order leaves a listed key with no head") ],
             evidence={ "entry_etag": meta.get("etag"), "entry_mtime": meta.get("mtime"), "entry_tag": entry.get("tag") },
             hint="ListObjects lists this key and GET answers 404", when=when)
        scan.count("inconsistency")
        return

    info = head_info(head_oid)
    if info is None:
        filtered("deleted during the scan")
        return
    if info["mtime"] >= scan.start - 1:
        filtered("rewritten during the scan")
        return
    if young(info["mtime"]):
        filtered("younger than the grace period")
        return
    kinds = { parse_oid(o)[1] for o in missing }
    keep_tail = bool(info["tail_tag"] and info["idtag"] and info["tail_tag"] != info["idtag"])
    tags = gc_tags(group)
    by_abort = any(t in group["uploads"] for t in tags)
    if kinds & { "part", "mp_shadow" }:
        causes = multipart_causes(scan, group, keep_tail, by_abort)
    else:
        causes = atomic_causes(keep_tail)
    evidence = { "missing": len(missing), "of": len(group["oids"]),
                 "head_idtag": info["idtag"], "head_tail_tag": info["tail_tag"] }
    if group["uploads"]:
        evidence["upload_ids"] = sorted(group["uploads"])
    if tags:
        evidence["gc_tags"] = tags
    emit("data_loss", "missing_data", group["bucket"], group["key"], oids=missing, causes=causes,
         evidence=evidence, when=info["mtime"],
         hint="GET of this object fails where the missing objects start")
    scan.count("data_loss")

def check_pending_loss(scan, group, head_oid):
    doomed = []
    times = []
    for oid, entries in group["gc"].items():
        if not survives_gc([t for t, _ in entries], read_refcount(oid)):
            doomed.append(oid)
            times += [parse_time(when) for _, when in entries]
    if not doomed:
        return
    info = head_info(head_oid) if head_oid else None
    if info is None or info["mtime"] >= scan.start - 1:
        filtered("rewritten during the scan")
        return
    keep_tail = bool(info["tail_tag"] and info["idtag"] and info["tail_tag"] != info["idtag"])
    tags = gc_tags(group)
    by_abort = any(t in group["uploads"] for t in tags)
    kinds = { parse_oid(o)[1] for o in doomed }
    due = [t for t in times if t]
    if kinds & { "part", "mp_shadow" }:
        # a GC entry is due rgw_gc_obj_min_wait after it was queued
        lag = min(due) - gc_min_wait - info["mtime"] if (by_abort and due) else None
        causes = multipart_causes(scan, group, keep_tail, by_abort, lag, requeued=not by_abort)
    else:
        causes = atomic_causes(keep_tail)
    emit("pending_loss", "queued_for_gc", group["bucket"], group["key"], oids=doomed, causes=causes,
         evidence={ "gc_tags": tags, "gc_due": iso(min(due)) if due else None, "queued_by_abort": by_abort,
                    "head_idtag": info["idtag"], "head_tail_tag": info["tail_tag"] },
         when=info["mtime"],
         hint="GC deletes these once its entries are due; escalate before then")
    scan.count("pending_loss")

def check_open_upload(scan, group, head_oid, upload):
    """a listed head names the parts of an upload that is still open"""
    info = head_info(head_oid) if head_oid else None
    if info is None:
        return
    if young(info["mtime"]):
        filtered("younger than the grace period")
        return
    u = scan.uploads[upload]
    meta = read_meta(scan, u["key"], upload)
    if meta is None:
        filtered("upload closed during the scan")
        return
    evidence = { "upload_id": upload, "meta_oid": meta["oid"], "meta_mtime": iso(meta["mtime"]),
                 "head_idtag": info["idtag"], "completion_record": meta["record"] }
    if meta["record"] and meta["record"] == info["idtag"]:
        emit("inconsistency", "completed_upload_open", group["bucket"], group["key"], upload_id=upload,
             causes=[ cause("mp-meta-left", "high", "the upload's completion record matches the head") ],
             evidence=evidence, when=info["mtime"],
             hint="this build records completions, so an abort of the upload deletes only its meta object")
        scan.count("inconsistency")
        return
    emit("at_risk", "completed_upload_open", group["bucket"], group["key"], upload_id=upload,
         causes=[ cause("mp-meta-left", "high", "the completion wrote the head, and the upload's meta object was never deleted"),
                  cause("ix-fail", "medium", None) ],
         evidence=evidence, when=info["mtime"],
         hint=f"do not abort upload {upload} or retry its completion, and keep lifecycle's AbortIncompleteMultipartUpload from reaching it: each frees this object's data")
    scan.count("at_risk")

def finalize_uploads(scan):
    """open uploads no listed head names: their parts should all be in the index"""
    for upload, u in sorted(scan.uploads.items()):
        if not u["meta"] or upload in scan.named:
            continue
        meta = read_meta(scan, u["key"], upload)
        if meta is None:
            continue
        if young(meta["mtime"]):
            filtered("younger than the grace period")
            continue
        unindexed = sorted(meta["parts"] - u["parts"])
        if not unindexed:
            continue
        emit("inconsistency", "part_entries_missing", scan.name, u["key"], upload_id=upload,
             causes=[ cause("refused-complete", "high", "the upload's meta object lists parts that have no bucket index entries") ],
             evidence={ "parts": len(meta["parts"]), "unindexed_parts": unindexed[:100], "meta_mtime": iso(meta["mtime"]) },
             when=meta["mtime"],
             hint="bucket stats undercount these parts until the upload is completed or aborted; no data is lost")
        scan.count("inconsistency")

"""
-I: index entries that list an older object than their head holds
"""
def entry_job(head_oid, entry):
    return entry, head_info(head_oid)

def check_index(scan):
    seen = set()
    window = deque()

    def settle(future):
        entry, info = future.result()
        if info is None or info["mtime"] >= scan.start - 1 or young(info["mtime"]):
            return
        meta = entry.get("meta", {})
        if info["etag"] is None or info["etag"] == meta.get("etag"):
            return
        key = entry["name"] + (f"[{entry['instance']}]" if entry.get("instance") else "")
        emit("inconsistency", "stale_entry", scan.name, key,
             causes=[ cause("stalled-write", "medium", "a write that stalled past the pending-op expiry leaves the index listing the object it replaced"),
                      cause("stale-entry", "medium", "completions applied out of order leave the index listing an older object") ],
             evidence={ "entry_etag": meta.get("etag"), "entry_mtime": meta.get("mtime"), "entry_tag": entry.get("tag"),
                        "head_etag": info["etag"], "head_idtag": info["idtag"], "head_mtime": iso(info["mtime"]) },
             when=info["mtime"],
             hint="ListObjects reports the older object's ETag and size; re-link the key from its head (radosgw-admin object reindex, where available)")
        scan.count("inconsistency")

    for e in stream_admin_array("bi", "list", f"--bucket={scan.name}"):
        if e.get("type") not in ("plain", "instance"):
            continue
        entry = e.get("entry", {})
        name = entry.get("name", "")
        instance = entry.get("instance", "")
        if (name.startswith("_multipart_") or not entry.get("exists") or entry.get("pending_map")
                or int(entry.get("flags", 0)) & dirent_flag_delete_marker or (name, instance) in seen):
            continue
        seen.add((name, instance))
        window.append(xattr_pool.submit(entry_job, f"{scan.marker}_{key_oid(name, instance)}", entry))
        while len(window) >= args.threads * 8:
            settle(window.popleft())
    while window:
        settle(window.popleft())

"""
-R: tail references no head holds
"""
def refcount_job(head_oid, tails):
    """the non-implicit references on a head's tail objects, and the tags that head carries"""
    needed = {}
    for oid in tails:
        refcount = read_refcount(oid)
        if refcount:
            tags = { t for t in refcount[0] if t }
            if tags:
                needed[oid] = tags
    carried = set()
    if needed and head_oid:
        info = head_info(head_oid)
        if info:
            carried = { t for t in (info["idtag"], info["tail_tag"]) if t }
    return needed, carried

def collect_refcounts(scan):
    for future in scan.refcount_jobs:
        needed, carried = future.result()
        for oid, tags in needed.items():
            refs_needed.setdefault(oid, (scan.name, set()))[1].update(tags)
            refs_carried.setdefault(oid, set()).update(carried)
    scan.refcount_jobs = []

def resolve_refcounts():
    for oid, (bucket, tags) in sorted(refs_needed.items()):
        stale = sorted(tags - refs_carried.get(oid, set()))
        if not stale:
            continue
        emit("latent_leak", "unheld_reference", bucket, oids=[oid],
             causes=[ cause("lost-copy", "high", "a copy that lost its race took this reference, and no head carries its tag"),
                      cause("dedup", "medium", "dedup takes references with the target's tail tag") ],
             evidence={ "unheld_tags": stale },
             hint="once the objects that name this tail are deleted, it is never freed; a copy in a bucket this run did not scan may still hold the reference")

"""
Buckets
"""
def process_bucket(bucket_name):
    global bucket_count
    global bucket_count_idx
    global missing_count
    bucket_meta = None
    gap_count=0

    if bucket_count:
        logger.info(f"Checking {bucket_name} via sync state")
        is_scanning = ceph.is_bucket_scanning(bucket_name)
        if is_scanning:
            logger.info(f"Bucket {bucket_name} is actively being scanned on {is_scanning['hostname']} ({is_scanning['pid']})")
            return None
        bucket_meta = ceph.get_bucket_meta(bucket_name)

    if bucket_meta:
        bucket_meta = json.loads(bucket_meta)
        dt = datetime.fromtimestamp(bucket_meta["end_time"]).strftime('%Y-%m-%d %H:%M:%S')
        hum = seconds_to_human(args.maxage)
        if time.time() - bucket_meta["end_time"] > int(args.maxage):
            logger.info(f"Bucket {bucket_name} end time ( {dt} ) is more than {hum} old.  Processing again.")
        else:
            logger.info(f"Bucket {bucket_name} end time ( {dt} ) is less than {hum} old.  Skipping.")
            return None

    logger.info(f"Processing {bucket_name}")
    bucket_count_idx += 1
    scan = BucketScan(bucket_name)
    if not scan.load():
        logger.error(f"Cannot read the stats of {bucket_name}, skipping its upload and index checks")
    elif not args.no_uploads:
        list_open_uploads(scan)

    brl = subprocess.Popen(admin_command("bucket", "radoslist", f"--rgw-obj-fs={fs}", f"--bucket={bucket_name}"),
                           bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    line_count = 0
    processed_count = 0
    starttime = round(time.time(),3)
    laststatus = round(time.time(),3)
    if bucket_count:
        ceph.start_bucket(bucket_name)

    group = None
    for brl_line in io.TextIOWrapper(brl.stdout, encoding="utf-8", errors="surrogateescape"):
        object_data = brl_line.strip().split(fs)
        if len(object_data) < 3:
            continue
        if len(args.match.strip()):
            matchpattern = r"^\b"+re.escape(args.match.strip())+r"\b"
            if not re.match(matchpattern,object_data[2]):
                continue

        line_count += 1
        if line_count % report_every_x_object_count == 0:
            nowtime = round(time.time(),3)
            deltaStart = nowtime - starttime
            deltaLast  = nowtime - laststatus
            laststatus = nowtime
            logger.info(f"[Status] Submitted {line_count} rados objects in {deltaStart:.3f} seconds ( last 10k in {deltaLast:.3f} seconds ) for {bucket_name}.")
            ceph.touch_sync_state(bucket_name=bucket_name, rados_count=line_count, gap_count=gap_count)

        # radoslist names all of one S3 object's RADOS objects together
        if group is None or group["bucket"] != object_data[1] or group["key"] != object_data[2]:
            close_group(group)
            group = new_group(scan, object_data[1], object_data[2])
        note_oid(scan, group, object_data[0])
        group["pending"] += 1
        ceph.in_flight.append({"comp": ceph.aio_stat_object(object_data[0],0), "objname": object_data[0], "bucket": f"s3://{object_data[1]}/{object_data[2]}", "poolidx": 0, "group": group})

        while len(ceph.in_flight) >= args.inflight:
            processed_count += 1
            res = check_aio_result(ceph.in_flight.popleft())
            if res is not None:
                if type(res) is dict:
                    ceph.in_flight.append(res)
                elif type(res) is int:
                    missing_count += 1
                    gap_count += 1

    close_group(group)
    while len(ceph.in_flight):
        res = check_aio_result(ceph.in_flight.popleft())
        if res is not None:
            if type(res) is dict:
                ceph.in_flight.append(res)
            elif type(res) is int:
                missing_count += 1
                gap_count += 1

    if scan.stats:
        finalize_uploads(scan)
        if args.check_index:
            check_index(scan)
    if args.refcount:
        collect_refcounts(scan)

    if bucket_count:
        ceph.end_bucket(bucket_name,line_count,gap_count,scan.class_counts)
    nowtime = round(time.time(),3)
    delta = nowtime - starttime
    logger.info(f"[Status] Processed {line_count} rados objects in {delta:.3f} seconds for {bucket_name}.")
    return None

"""
-O: orphans from rgw-orphan-list
"""
def load_bucket_stats():
    """every bucket's stats, from one 'bucket stats' call, keyed as 'bucket list' names them"""
    for st in stream_admin_array("bucket", "stats"):
        if isinstance(st, dict) and st.get("bucket"):
            name = f"{st['tenant']}/{st['bucket']}" if st.get("tenant") else st["bucket"]
            bucket_stats[name] = st
    logger.info(f"Read the stats of {len(bucket_stats)} buckets")

def head_key(rest):
    """a head object's (name, instance), from its name after the bucket marker and '_'"""
    if rest.startswith("_:"):
        instance, _, name = rest[2:].partition("_")
        return name, instance
    if rest.startswith("__"):
        return rest[1:], ""
    return rest, ""

def tail_prefixes(bucket, name, instance):
    """the name prefixes of an object's tail objects, from its manifest"""
    parts = ["object", "stat", f"--bucket={bucket}", f"--object={name}"]
    if instance:
        parts.append(f"--object-version={instance}")
    out = run_admin(*parts, quiet=True)
    if not out:
        return []
    try:
        manifest = json.loads(out).get("manifest", {})
    except ValueError:
        return []
    prefix = manifest.get("prefix")
    marker = manifest.get("tail_placement", {}).get("bucket", {}).get("marker")
    if not prefix or not marker:
        return []
    if manifest.get("rules") and any(r.get("val", {}).get("part_size") for r in manifest["rules"]):
        # a multipart object: <prefix>.<part>[_<stripe>]
        return [f"{marker}__multipart_{prefix}.", f"{marker}__shadow_{prefix}."]
    return [f"{marker}__shadow_{prefix}"]

def classify_orphans():
    """
    Classify rgw-orphan-list's output: one finding per unlisted head ( with
    its tail ), per upload's leaked parts, and per leaked tail.
    """
    load_bucket_stats()
    markers = { st["marker"]: st for st in bucket_stats.values() if st.get("marker") }
    oids = []
    with open(args.orphans) as olist:
        for line in olist:
            oid = line.strip().split("\t")[-1]
            if oid:
                oids.append(oid)
    logger.info(f"Classifying {len(oids)} orphan(s) from {args.orphans}")

    heads = []
    groups = {}
    upload_open = {}

    def usable(oid):
        """the orphan's (ioctx, size, mtime), unless it is gone, young or queued for GC"""
        found = ceph.find(oid, ceph.pool_ioctl + ceph.extra_ioctl)
        if not found:
            filtered("orphan gone")
            return None
        if young(found[2]):
            filtered("younger than the grace period")
            return None
        if gc_index and oid in gc_index:
            filtered("queued for GC")
            return None
        return found

    def add(gkey, oid, found, **info):
        g = groups.setdefault(gkey, { "oids": [], "size": 0, "mtime": None, "refs": set() })
        g.update(info)
        g["oids"].append(oid)
        g["size"] += found[1]
        g["mtime"] = min(g["mtime"] or found[2], found[2])
        refcount = read_refcount(oid, found[0])
        if refcount:
            g["refs"].update(t for t in refcount[0] if t)

    # heads first, so that the tail of an unlisted head is reported with it
    for oid in oids:
        marker, kind, key, upload = parse_oid(oid)
        if kind != "head" or marker not in markers:
            continue
        found = usable(oid)
        if not found:
            continue
        idtag = get_xattr(found[0], oid, xattr_idtag)
        if idtag is None:
            add((marker, "orphan_other", ""), oid, found)
            continue
        bucket = markers[marker]["bucket"]
        name, instance = head_key(oid[len(marker) + 1:])
        heads.append({ "oid": oid, "bucket": bucket, "key": name + (f"[{instance}]" if instance else ""),
                       "idtag": idtag, "size": found[1], "mtime": found[2], "tails": [],
                       "prefixes": tail_prefixes(bucket, name, instance) })

    for oid in oids:
        marker, kind, key, upload = parse_oid(oid)
        if kind == "head" and marker in markers:
            continue
        if kind == "meta":
            filtered("open upload's meta object")
            continue
        owner = next((h for h in heads if any(oid.startswith(p) for p in h["prefixes"])), None)
        if owner:
            owner["tails"].append(oid)
            continue
        found = usable(oid)
        if not found:
            continue
        if marker not in markers:
            add((marker, "orphan_of_removed_bucket", ""), oid, found)
        elif kind in ("part", "mp_shadow") and upload:
            if upload not in upload_open:
                upload_open[upload] = bool(ceph.find(f"{marker}__multipart_{key}.{upload}.meta",
                                                     ceph.extra_ioctl + ceph.pool_ioctl))
            if upload_open[upload]:
                filtered("part of an open upload")
                continue
            add((marker, "orphan_parts", f"{key}.{upload}"), oid, found, upload_id=upload, key=key)
        elif kind == "shadow":
            rest = oid[len(marker) + len("__shadow_"):]
            add((marker, "orphan_tail", rest[:rest.rfind("_") + 1]), oid, found)
        else:
            add((marker, "orphan_other", ""), oid, found)

    for h in heads:
        emit("inconsistency", "unlisted_head", h["bucket"], h["key"], oids=[h["oid"]] + h["tails"],
             causes=[ cause("stalled-write", "high", "a new key's write stalled past the pending-op expiry, and a listing dropped its entry") ],
             evidence={ "head_idtag": h["idtag"], "tail_objects": len(h["tails"]) }, when=h["mtime"],
             hint="GET by key reads this object, but no listing shows it; re-link it with radosgw-admin object reindex, or rgw-restore-bucket-index")

    for (marker, check, prefix), g in sorted(groups.items()):
        st = markers.get(marker)
        bucket = st["bucket"] if st else f"<marker {marker}>"
        evidence = { "objects": len(g["oids"]), "bytes": g["size"] }
        causes = []
        hint = None
        if g["refs"]:
            evidence["references"] = sorted(g["refs"])
            causes = [ cause("lost-copy", "high", "the objects keep a copy's reference, and nothing names them"),
                       cause("dedup", "medium", "dedup takes references like a copy") ]
        elif check == "orphan_parts":
            evidence["upload_id"] = g["upload_id"]
            causes = [ cause("lost-complete", "high", "the parts of an upload that has no meta object, and that no head names"),
                       cause("dedup", "low", None) ]
        elif check == "orphan_tail":
            causes = [ cause("delete-race", "medium", "a delete that raced an overwrite leaves the new object's tail"),
                       cause("cond-delete", "medium", "a conditional delete that raced an overwrite leaves the new object's tail"),
                       cause("copy-self", "medium", "a copy onto itself that raced an overwrite leaves the overwrite's tail"),
                       cause("dedup", "low", None) ]
        elif check == "orphan_of_removed_bucket":
            hint = "no bucket has this marker; the objects outlived their bucket"
        emit("leak", check, bucket, g.get("key"), oids=g["oids"], causes=causes, evidence=evidence,
             when=g["mtime"], hint=hint)

"""
Run setup and summary
"""
def detect_majors():
    """the major versions of the cluster's RGWs and OSDs, from 'ceph versions'"""
    if args.release:
        rel = args.release.strip().lower()
        if rel.isdigit():
            return { int(rel) }
        if rel in release_majors:
            return { release_majors[rel] }
        logger.critical(f"Unknown release {args.release}; use one of {', '.join(release_majors)} or a major version")
        exit(1)
    try:
        ret, out, err = ceph.cluster.mon_command(json.dumps({ "prefix": "versions", "format": "json" }), b'')
        versions = json.loads(out)
    except Exception as e:
        logger.error(f"Cannot read 'ceph versions' ( {e} ); considering every known issue")
        return set()
    majors = set()
    for daemon in ("rgw", "osd"):
        for vstr in versions.get(daemon, {}):
            m = re.search(r"ceph version (\d+)\.", vstr)
            if m:
                majors.add(int(m.group(1)))
    return majors

def load_gc():
    global gc_index
    global gc_min_wait
    try:
        gc_min_wait = int(ceph.cluster.conf_get("rgw_gc_obj_min_wait"))
    except Exception:
        pass
    gc_index = {}
    entries = 0
    for entry in stream_admin_array("gc", "list", "--include-all"):
        entries += 1
        tag = entry.get("tag", "").rstrip("\0")
        when = entry.get("time", "")
        for obj in entry.get("objs", []):
            gc_index.setdefault(obj.get("oid"), []).append((tag, when))
    logger.info(f"Read {entries} GC entries naming {len(gc_index)} objects")

def summarize():
    if finding_counts:
        logger.critical("Findings: " + ", ".join(f"{finding_counts[c]} {c}" for c in finding_classes if c in finding_counts))
        if cause_counts:
            logger.critical("Most likely causes: " + ", ".join(f"{n} {c}" for c, n in sorted(cause_counts.items(), key=lambda i: -i[1])))
        logger.critical(f"Findings are in {args.findings}")
    else:
        logger.info("No findings.")
    if filtered_counts:
        logger.info("Skipped: " + ", ".join(f"{n} {r}" for r, n in sorted(filtered_counts.items())))

def verify_results():
    global missing_count
    if not os.path.exists(args.verify):
        logger.critical(f"[CRITICAL] Previous results file {args.verify} not present.")
        return None

    if os.path.getsize(args.verify) == 0:
        logger.critical(f"[CRITICAL] Previous results file {args.verify} is empty.")
        return None

    wl = subprocess.Popen(["wc","-l",args.verify],stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    wl_line = wl.stdout.readline().decode("ascii").strip()
    rados_count = wl_line.split(" ")[0]
    logger.info(f"Starting verify of {rados_count} rados object(s) from {args.verify}")
    missing_count = 0
    found_count = 0
    with open(args.verify) as vlist:
        for line in vlist:
            robj = re.sub(r'^.* MISSING ', '', line.strip())
            ceph.in_flight.append({"comp": ceph.aio_stat_object(robj), "line": line.strip() })
            while len(ceph.in_flight) >= args.inflight:
                oldest_op = ceph.in_flight.popleft()
                results = []
                for comp in oldest_op['comp']:
                    comp.wait_for_complete()
                    results.append(comp.get_return_value())

                if results.count(-2) == len(oldest_op['comp']):
                    missing_count += 1
                    outfile.write(re.sub(' MISSING ',' STILL MISSING ',oldest_op['line']) + "\n")
                else:
                    found_count += 1

        while len(ceph.in_flight):
            oldest_op = ceph.in_flight.popleft()
            results = []
            for comp in oldest_op['comp']:
                comp.wait_for_complete()
                results.append(comp.get_return_value())

            if results.count(-2) == len(oldest_op['comp']):
                missing_count += 1
                outfile.write(re.sub(' MISSING ',' STILL MISSING ',oldest_op['line']) + "\n")
            else:
                found_count += 1

    found = "."
    if found_count:
        found = f", but {found_count} were found!"
    logger.critical(f"Verified {missing_count} rados objects still missing{found}")
    return None

def process_list():
    global bucket_count
    if args.bucketlist:
        bucket_list = args.bucketlist.split(" ")
        bc = len(bucket_list)
        logger.info(f"Starting processing of {bc} bucket(s)")
        for bucket in args.bucketlist.split(" "):
            process_bucket(bucket)
        return None

    if args.listfile:
        if not os.path.exists(args.listfile):
            logger.critical(f"[CRITICAL] Bucket list file {args.listfile} not present.")
            return None

        if os.path.getsize(args.listfile) == 0:
            logger.critical(f"[CRITICAL] Bucket list file {args.listfile} is empty.")
            return None
        wl = subprocess.Popen(["wc","-l",args.listfile],stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        wl_line = wl.stdout.readline().decode("ascii").strip()
        bc = wl_line.split(" ")[0]
        logger.info(f"Starting processing of {bc} bucket(s) from {args.listfile}")
        with open(args.listfile) as blist:
            for line in blist:
                process_bucket(line.strip())

        return None

    # If we get here, we're processing -all- buckets
    # Get a count of the buckets to determine sync object count
    bl = subprocess.Popen(admin_command("bucket", "list"), bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    jql = subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bc  = subprocess.Popen(["wc","-l"], stdin=jql.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bucket_count = int(bc.stdout.readline().decode("ascii").strip())
    logger.info(f"Starting processing of {bucket_count} bucket(s)")

    shard_count = int(bucket_count/400) + 1
    ceph.populate_sync_objects(shard_count)
    load_bucket_stats()

    if args.norandom: # Do not randomize the bucket list, optional.
        bl = subprocess.Popen(admin_command("bucket", "list"), bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        for bl_line in io.TextIOWrapper(bl.stdout, encoding="utf-8"):
            bl_line = bl_line.strip()
            if re.match(r'^"',bl_line):
                """
                The raw output of bucket list is a json array.  We need to only process lines that start with
                double quotes, and then we need to remove the double quotes and ending comma (if present), but
                NOT remove any other characters in between.
                """
                bucket = re.sub(r'^"','',bl_line)
                bucket = re.sub(r',$','',bucket)
                bucket = re.sub(r'"$','',bucket)

                process_bucket(bucket)

    else: # Randomize the bucket list, this is the default.
        bl = subprocess.Popen(admin_command("bucket", "list"), bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        jql = subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        sortl = subprocess.Popen(["sort","--random-sort"],stdin=jql.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        for sortl_line in io.TextIOWrapper(sortl.stdout, encoding="utf-8"):
            bucket = sortl_line.strip()
            process_bucket(bucket)

    return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-run / Multi-host capable rgw-gap-list tool")
    parser.add_argument("-a", "--maxage",  default = 7*86400, type=int, help="Maximum age (in seconds) of last scan before rescan is forced.  Default 7 days.")
    parser.add_argument("-b", "--bucketlist",  default = '', help="Optional: Bucket(s) to operate on, default is all buckets, quoted space separated list is supported.")
    parser.add_argument("-c", "--conf", default = '/etc/ceph/ceph.conf', help="Ceph conf file to use, default '/etc/ceph/ceph.conf'")
    parser.add_argument("-d", "--delete",  default = False, action="store_true", help="Remove all sync objects and Exit. Used to clear all syncronized bucket status data.")
    parser.add_argument("-i", "--inflight",  default = 10000, type=int, help="Maximum number of in-flight ops to allow without a response.  Default: 10000")
    parser.add_argument("-l", "--listfile", default = '', help="Optional: Bucket list file, should be one bucket name per line.")
    parser.add_argument("-m", "--match", default = '', help="Specify a prefix match for the object names.  Only objects matching this prefix will be checked for gaps.")
    parser.add_argument("-n", "--norandom", default = False, action="store_true", help="By default, the script randomizes the list of buckets before processing.  On large bucket count environments, this may cause significant delay before start of processing due to the way the randomizing occurs.  Set '-n' to Not Randomize the list to remove this delay.")
    parser.add_argument("-o", "--outfile", default = f'gap-list-results.{mypid}', help="Optional: results file name, default: gap-list-results.###")
    parser.add_argument("-p", "--pool", default = '', help="Bucket Data Pool(s), quoted space separated list is supported ( 'pool:namespace' too ).  Default: the data and extra pools of every placement target of the zone.")
    parser.add_argument("-s", "--syncpool", default = 'default.rgw.buckets.index', help="Synchronization / Queuing pool for the script ot use, default 'default.rgw.buckets.index'.")
    parser.add_argument("-r", "--report",  default = False, action="store_true", help="Generate bucket scrub metadata report.")
    parser.add_argument("-j", "--json",  default = False, action="store_true", help="Use JSON format for bucket scrub metadata report. Only considered with -r")
    parser.add_argument("-v", "--verbosity", default = 0, action="count", help="Optional: Verbosity level, multiple -v's are supported for higher verbosity, example: -vvv")
    parser.add_argument("-x", "--verify", default = '', help="Used to veryify the results file from a prior run, supply the prior run gap-list-results file.")
    parser.add_argument("-J", "--findings", default = f'gap-list-findings.{mypid}.jsonl', help="Classified findings file, one JSON object per line.  Default: gap-list-findings.###.jsonl")
    parser.add_argument("-G", "--no-gc", default = False, action="store_true", help="Do not read the GC queue.  Without it, data queued for deletion is not found, and GC entries are no evidence.")
    parser.add_argument("-U", "--no-uploads", default = False, action="store_true", help="Do not read each bucket's open multipart uploads from its index.")
    parser.add_argument("-I", "--check-index", default = False, action="store_true", help="Also compare every index entry's ETag with its head's, to find entries that list an older object.  One xattr read per object.")
    parser.add_argument("-R", "--refcount", default = False, action="store_true", help="Also read the refcount of every tail object, to find references no head holds.  One xattr read per tail object.")
    parser.add_argument("-O", "--orphans", default = '', help="Classify the orphans listed in this rgw-orphan-list output file, instead of scanning buckets.")
    parser.add_argument("-T", "--threads", default = 32, type=int, help="Threads for the xattr reads of -I and -R.  Default: 32")
    parser.add_argument("--grace", default = 3600, type=int, help="Skip findings younger than this many seconds, which may belong to requests in flight.  Default: 3600")
    parser.add_argument("--release", default = '', help="The cluster's release, as a name ( reef, squid, tentacle ) or major version, instead of 'ceph versions'.")
    parser.add_argument("--fixed", default = '', help="Comma separated ceph/ceph pull request numbers of fixes this build carries.")
    parser.add_argument("--fixed-since", default = '', help="The date ( YYYY-MM-DD ) the --fixed fixes were deployed.  Findings newer than that, whose causes are all fixed, are flagged 'after_fix'.")
    args = parser.parse_args()
    debug_level = min([len(log_levels)-1,args.verbosity])

    logging.basicConfig(
        level=log_levels[debug_level],
        format=f'%(asctime)s {myhost}.{mypid} %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )

    logger = logging.getLogger('rgw-gap-list')

    fixed_prs = { int(p.strip().lstrip("#")) for p in args.fixed.split(",") if p.strip() }
    if args.fixed_since:
        fixed_since = parse_time(args.fixed_since + " 00:00:00")

    extra_pools = []
    index_pools = {}
    pools = zone_pools()
    if pools:
        data_pools, extra_pools, index_pools = pools
    if args.pool:
        pool_names = args.pool.split(" ")
    elif pools:
        pool_names = data_pools + [p for p in extra_pools if p not in data_pools]
        logger.info(f"Using the zone's pools: {pool_names}")
    else:
        pool_names = ['default.rgw.buckets.data', 'default.rgw.buckets.non-ec']
        logger.error(f"Cannot read the zone; using {pool_names}")

    if args.report:
        with CephClusterConnection(ceph_conf=args.conf, pool_names=pool_names, sync_pool=args.syncpool) as ceph:
            ceph.generate_report()
            exit()
    elif args.delete:
        with CephClusterConnection(ceph_conf=args.conf, pool_names=pool_names, sync_pool=args.syncpool) as ceph:
            ceph.delete_sync_objects()
            exit()

    if args.verify and args.outfile == f'gap-list-results.{mypid}':
        args.outfile = f'gap-list-verify-results.{mypid}'

    with open(args.outfile,"w") as outfile, open(args.findings,"w") as findings:
        with CephClusterConnection(ceph_conf=args.conf, pool_names=pool_names, sync_pool=args.syncpool,
                                   extra_pools=extra_pools, index_pools=index_pools) as ceph:
            if args.verify:
                verify_results()
            else:
                cluster_majors = detect_majors()
                logger.info(f"Considering the issues of major version(s) {sorted(cluster_majors) or 'any'}")
                if not args.no_gc:
                    load_gc()
                xattr_pool = ThreadPoolExecutor(max_workers=args.threads)
                if args.orphans:
                    classify_orphans()
                else:
                    process_list()
                    if args.refcount:
                        resolve_refcounts()
                xattr_pool.shutdown()
                summarize()

    if missing_count:
        logger.critical(f"There were {missing_count} missing rados objects. Results are in {args.outfile}")
    else:
        logger.info(f"There were no missing rados objects. Removing results file {args.outfile}")
        os.remove(args.outfile)
    if not finding_counts:
        os.remove(args.findings)
