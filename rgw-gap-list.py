#!/usr/bin/env python

"""
By: Michael J. Kidd (linuxkidd)
Last Revision: 2026-09-30
Version: 3.0

Now storing results in RADOS

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
   bucket index pool in the 'rgw-gap-list' namespace (by default).
3. Since the state is synchronized via Ceph RADOS... multiple instances can be
   running in parallel, even across different hosts!
4. This script can also be ran with the '-r' option to generate a report of
   current running hosts, and state per bucket.
5. This script can verify its own results by passing the '-x' flag.

Usage can be had by passing '--help' to the script.

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
  and '-s', respectively.
  -- NOTE -- If you don't use the same pools on all instances of this script,
  the synchronized state will not work.
- You can verify the RADOS stored results with the '-x' parameter.
- You can limit the objects to only those matching a given prefix using the
  '-m' parameter.
- The '-g', '-r' and '-x' parameters support json output by adding '-j' flag.

## Known Issues:
- If two separate instances attempt to start processing the same bucket in
  a very narrow window ( < 50ms, but the exact value depends on a lot of
  variables ), they may both succeed in starting the process, instead of one
  winning the race to push the sync object omap update and blocking the other.
  This has no real impact aside from doubling any gap objects listed in the
  results and having two threads processing the same bucket.

Enjoy!
"""

import argparse
from collections import deque
from datetime import datetime
import hashlib
import io
import json
import logging
import os
import re
import rados
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import List, Dict, Optional, Union

LOG_LEVELS = [ 50, 30, 20, 10 ]
MYPID = os.getpid()
MYHOST = os.uname().nodename

def signal_handler(sig: int, frame: Optional[FrameType]) -> None:
    print(f'Received {sig}, Terminating')
    sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

class CephClusterConnection:
    """
    A context manager to handle connecting to and disconnecting from a
    Ceph RADOS cluster, ensuring resources are cleaned up properly.
    """
    def __init__(self, ceph_conf: str = '/etc/ceph/ceph.conf', pool_names: List = [], sync_pool: str = '') -> None:
        self.ceph_conf = ceph_conf
        self.cluster = None
        self.pool_names = pool_names
        self.pool_ioctl = []
        self.sync_pool = sync_pool
        self.sync_ioctl = None
        self.in_flight = deque()
        self.shard_count = 1
        self.results = {}
        self.bucket_gap_results_obj_count = 0
        self.bucket_gap_count = 0
        self.gap_header_data = None
        self.total_bucket_count = 0
        self.processed_bucket_count = 0
        self.report_every_x_object_count = 10000
        self.namespace = "rgw-gap-list"
        self.max_inflight = 15000
        self.match = ""
        self.max_age = 7 * 86400
        self.json = False
        self.skipped_bucket_count = 0

        self.FIELD_SEPARATOR = "\xfe"
        self.BUCKET_LIST_COMMAND = ["radosgw-admin", "bucket", "list"]
        self.BUCKET_RADOSLIST_COMMAND = ['radosgw-admin', 'bucket', 'radoslist', f'--rgw-obj-fs={self.FIELD_SEPARATOR}']
        self.SYNC_OBJECT_NAME = "rgw-gap-list-sync-object"
        self.RESULTS_OBJECT_NAME = "rgw-gap-list-results-object"

    def __enter__(self):
        """Called when entering the 'with' block."""
        self.cluster = rados.Rados(conffile=self.ceph_conf)
        try:
            self.cluster.connect()
            logger.info("Successfully connected to the Ceph cluster.")
        except rados.Error as e:
            logger.critical(f"Failed to connect to the Ceph cluster: {e}")
            raise RuntimeError(f"Failed to connect to the Ceph cluster: {e}")

        logger.info(f"Opening ioctl for sync pool {self.sync_pool}")
        try:
            self.sync_ioctl = self.cluster.open_ioctx(self.sync_pool)
        except rados.ObjectNotFound:
            logger.critical(f"Sync Pool {self.sync_pool} not present.  Exiting.")
            exit(1)
        else:
            if len(self.namespace) > 0:
                self.sync_ioctl.set_namespace(self.namespace)

        for pool_name in self.pool_names:
            logger.info(f"Opening ioctl for pool {pool_name}")
            try:
                self.pool_ioctl.append(self.cluster.open_ioctx(pool_name))
            except rados.ObjectNotFound:
                logger.error(f"Pool {pool_name} not present, skipping.")
            else:
                if re.search(r"\.non-ec$",pool_name):
                    logger.info(f"Pool {pool_name}, adding namespace 'multipart'")
                    self.pool_ioctl.append(self.cluster.open_ioctx(pool_name))
                    self.pool_ioctl[len(self.pool_ioctl)-1].set_namespace('multipart')

        if len(self.pool_ioctl)==0:
            logger.critical(f"None of the listed pools exist!  Exiting!")
            exit(1)

        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'with' block, ensuring safe shutdown."""
        if self.cluster:
            self.rm_sync_state()
            for ioctx in self.pool_ioctl:
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

    def aio_stat_object(self, object_name: str = "", idx: Optional[int] = None) -> Optional[List]:
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

    def delete_sync_objects(self) -> None:
        logger.critical("Deleting sync objects...")
        try:
            self.sync_ioctl.stat(self.SYNC_OBJECT_NAME)
        except rados.ObjectNotFound:
            pass
        else:
            bucket_metadata_header = json.loads(self.sync_ioctl.read(self.SYNC_OBJECT_NAME).decode("ascii"))
            self.shard_count = bucket_metadata_header["shard_count"]
            logger.info(f"Deleting primary sync object: {self.SYNC_OBJECT_NAME}")
            self.sync_ioctl.remove_object(self.SYNC_OBJECT_NAME)


        for i in range(self.shard_count):
            try:
                self.sync_ioctl.stat(f"{self.SYNC_OBJECT_NAME}.{i}")
            except rados.ObjectNotFound:
                pass
            else:
                logger.info(f"Deleting sync object: {self.SYNC_OBJECT_NAME}.{i}")
                self.sync_ioctl.remove_object(f"{self.SYNC_OBJECT_NAME}.{i}")

        logger.critical("Finished deleting sync objects.")

    def delete_gap_objects(self,bucket_list: Optional[List] = None) -> None:
        if bucket_list:
            logger.debug(f"Deleting gap results for bucket(s) {bucket_list}")
        else:
            logger.critical("Deleting gap restults object(s)...")

        try:
            self.sync_ioctl.stat(self.RESULTS_OBJECT_NAME)
        except rados.ObjectNotFound:
            logger.critical("No primary results object found.")
            return None

        logger.debug(f"Found primary results object: {self.RESULTS_OBJECT_NAME}")

        running_hosts = self.get_running_hosts()
        if len(running_hosts) and not bucket_list:
            logger.critical("There are active running processes. Exiting!")
            exit(1)

        remove_primary = False
        if not bucket_list:
            bucket_list = list(self.read_gap_header(cache=True))
            remove_primary = True

        for bucket_name in bucket_list:
            idx = 0
            while True:
                idx += 1
                results_object = f"{self.RESULTS_OBJECT_NAME}.{bucket_name}.{idx}"
                try:
                    self.sync_ioctl.stat(results_object)
                except rados.ObjectNotFound:
                    break
                else:
                    logger.info(f"Deleting sync object: {results_object}")
                    self.sync_ioctl.remove_object(results_object)

        if remove_primary:
            try:
                self.sync_ioctl.stat(self.RESULTS_OBJECT_NAME)
            except:
                pass
            else:
                logger.info(f"Deleting primary results object: {self.RESULTS_OBJECT_NAME}")
                self.sync_ioctl.remove_object(self.RESULTS_OBJECT_NAME)
        else:
            logger.info(f"Deleting bucket keys from {self.RESULTS_OBJECT_NAME}")
            with rados.WriteOpCtx() as op:
                self.sync_ioctl.remove_omap_keys(op, tuple(bucket_list))
                try:
                    self.sync_ioctl.operate_write_op(op, self.RESULTS_OBJECT_NAME)
                except rados.ObjectNotFound:
                    logger.info(f"Primary results object not found: {self.RESULTS_OBJECT_NAME}")
                    pass


    def populate_sync_objects(self,shard_count: int = 1, bucket_count: int = 0) -> None:
        self.shard_count=shard_count
        try:
            self.sync_ioctl.stat(self.SYNC_OBJECT_NAME)
        except rados.ObjectNotFound:
            logger.info(f"Populating sync objects...")
            logger.debug(f"Creating primary sync object: {self.SYNC_OBJECT_NAME}")
            sync_data = { "bucket_count": bucket_count, "shard_count": shard_count, "epoch": round(time.time(),3) }
            self.sync_ioctl.write_full(self.SYNC_OBJECT_NAME,json.dumps(sync_data).encode("utf-8"))
            self.touch_sync_state(bucket_name='', rados_obj_count=0)
        else:
            logger.debug(f"Found primary sync object: {self.SYNC_OBJECT_NAME}")
            self.touch_sync_state(bucket_name='', rados_obj_count=0)
            bucket_metadata_header = json.loads(self.sync_ioctl.read(self.SYNC_OBJECT_NAME).decode("ascii"))
            running_hosts = self.get_running_hosts()
            logger.debug(f'Request {shard_count} shards, existing {bucket_metadata_header["shard_count"]}')
            if shard_count <= ( bucket_metadata_header["shard_count"] * 1.5 ) or running_hosts:
                shard_count = self.shard_count = bucket_metadata_header["shard_count"]
            else:
                logger.info("No running hosts, and shard count is too low, resetting sync objects.")
                self.delete_sync_objects()
                self.populate_sync_objects(shard_count, bucket_count)
                return

        for i in range(shard_count):
            try:
                self.sync_ioctl.stat(f"{self.SYNC_OBJECT_NAME}.{i}")
                logger.debug(f"Found sync object: {self.SYNC_OBJECT_NAME}.{i}")
            except rados.ObjectNotFound:
                logger.debug(f"Creating sync object: {self.SYNC_OBJECT_NAME}.{i}")
                self.sync_ioctl.write_full(f"{self.SYNC_OBJECT_NAME}.{i}",b'')

        logger.info("Finished populating sync objects...")

    def hash_bucketname(self,bucketname: str) -> int:
        digest = hashlib.sha256(bucketname.encode("utf-8")).digest()
        return int.from_bytes(digest,byteorder="big") % self.shard_count

    def write_result_object(self, bucket_name: str = '', final: bool = False) -> None:
        if len(self.results):
            self.bucket_gap_results_obj_count += 1  # Increment first, so 0 means no objects in the bucket status omap.
            results_stored_size = len(json.dumps(self.results).encode("utf-8"))
            results_object = f"{self.RESULTS_OBJECT_NAME}.{bucket_name}.{self.bucket_gap_results_obj_count}"
            logger.info(f"Writing result object {results_object} of {results_stored_size} bytes")
            try:
                self.sync_ioctl.write_full(results_object,json.dumps(self.results).encode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to write results to {results_object}: {e}")
                logger.critical(f"Dumping result here due to failure to write {results_object}: {json.dumps(self.results)}")
            else:
                bucket_statistics = { "results_obj_count": self.bucket_gap_results_obj_count, "gap_count": self.bucket_gap_count, "latest_scan": round(time.time(),3) }
                with rados.WriteOpCtx() as write_op:
                    self.sync_ioctl.set_omap(write_op,(bucket_name, ),( json.dumps(bucket_statistics), ))
                    self.sync_ioctl.operate_write_op(write_op, f"{self.RESULTS_OBJECT_NAME}")

            self.results={}
            if final:
                self.bucket_gap_results_obj_count = 0
        else:
            self.bucket_gap_results_obj_count = 0

    def add_result_entry(self, bucket_name: str = '', object_name: str = '', rados_object: str = '', final: bool = False) -> None:
        if not final:
            logger.debug(f"Adding gap for s3://{bucket_name}/{object_name} :: {rados_object}")
            if object_name not in self.results:
                self.results[object_name]={ "epoch": round(time.time(), 3), "missing_rados_objects": [ ] }
            self.results[object_name]["missing_rados_objects"].append(rados_object)
            self.bucket_gap_count += 1
        results_stored_size = len(json.dumps(self.results).encode("utf-8"))
        if results_stored_size >= 1<<22 or final: # 4mb
            self.write_result_object(bucket_name, final)

    def touch_sync_state(self, bucket_name: str = '', rados_obj_count: int = 0, gap_count: int = 0) -> None:
        with rados.WriteOpCtx() as write_op:
            sync_state = { "epoch": round(time.time(),3), "current_bucket": bucket_name, "rados_obj_count": rados_obj_count, "gap_count": gap_count, "bucket_counter": self.processed_bucket_count, "total_buckets": self.total_bucket_count, "bucket_gap_results_obj_count": self.bucket_gap_results_obj_count }
            self.sync_ioctl.set_omap(write_op,(f"{MYHOST}.{MYPID}",),( json.dumps(sync_state), ))
            self.sync_ioctl.operate_write_op(write_op, self.SYNC_OBJECT_NAME)

    def rm_sync_state(self) -> None:
        with rados.WriteOpCtx() as write_op:
            try:
                self.sync_ioctl.remove_omap_keys(write_op, (f"{MYHOST}.{MYPID}",))
                self.sync_ioctl.operate_write_op(write_op, self.SYNC_OBJECT_NAME)
            except:
                pass

    def start_bucket(self,bucket_name,match: str = '') -> None:
        self.delete_gap_objects(bucket_name)
        shardid = self.hash_bucketname(bucket_name)
        logger.debug(f"Setting bucket start metadata to sync shard {shardid}")
        sync_metadata = { "hostname": MYHOST, "pid": MYPID, "rados_obj_count": 0, "gap_count": 0, "start_time": round(time.time(),3), "end_time": 0, "match": match }
        with rados.WriteOpCtx() as write_op:
            # Set bucket metadata
            self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(sync_metadata), ))
            self.sync_ioctl.operate_write_op(write_op, f"{self.SYNC_OBJECT_NAME}.{shardid}")
        self.touch_sync_state(bucket_name,0,0)


    def get_bucket_meta(self,bucket_name: str) -> Optional[bool]:
        shardid = self.hash_bucketname(bucket_name)
        logger.info(f"Getting bucket metadata from shard {shardid}")
        with rados.ReadOpCtx() as read_op:
            omap_iter, ret = self.sync_ioctl.get_omap_vals_by_keys(read_op, (bucket_name,))
            try:
                self.sync_ioctl.operate_read_op(read_op, f"{self.SYNC_OBJECT_NAME}.{shardid}")
            except rados.ObjectNotFound:
                logger.debug(f"Sync Object {self.SYNC_OBJECT_NAME}.{shardid} not found.")
                return False
            results = list(omap_iter)
            if results:
                rkey, rval = results[0]
                logger.debug(f"Found bucket metadata: {rval}")
                return rval
            else:
                logger.debug(f"Bucket metadata not present.")
                return False

    def end_bucket(self,bucket_name: str,rados_obj_count: int) -> None:
        shardid = self.hash_bucketname(bucket_name)
        logger.info(f"Setting bucket end metadata for {bucket_name} to sync shard {shardid}")
        bucket_meta = self.get_bucket_meta(bucket_name)
        if bucket_meta:
            bucket_meta = json.loads(bucket_meta)
            bucket_meta.update( { "end_time": round(time.time(),3), "gap_count": self.bucket_gap_count, "rados_obj_count": rados_obj_count,
                                "total_time_secs": round(bucket_meta["end_time"] - bucket_meta["start_time"],3) })
            logger.debug(f"Bucket meta: {bucket_meta}")
            with rados.WriteOpCtx() as write_op:
                # Set bucket metadata
                self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(bucket_meta), ))
                self.sync_ioctl.operate_write_op(write_op, f"{self.SYNC_OBJECT_NAME}.{shardid}")
            self.touch_sync_state(bucket_name,rados_obj_count,self.bucket_gap_count)
        else:
            logger.error(f"Bucket start metadata for {bucket_name} is missing from shard {shardid}")

        self.bucket_gap_count = 0


    def is_bucket_scanning(self, bucket_name: str) -> bool:
        running_hosts = self.get_running_hosts(bucket_keyed=True)
        if bucket_name in running_hosts:
            return running_hosts[bucket_name]
        else:
            return False

    def get_running_hosts(self, bucket_keyed: bool = False) -> Dict:
        running_hosts = {}
        running_hosts_raw = self.read_all_omap_vals(self.SYNC_OBJECT_NAME)

        for key, value in running_hosts_raw.items():
            if key == f"{MYHOST}.{MYPID}":
                continue
            key_parts = key.strip().split(".")
            rhost = key_parts[0]
            rpid = key_parts[len(key_parts)-1]
            status = value
            if not rhost in running_hosts and not bucket_keyed:
                running_hosts[rhost] = {}
            if bucket_keyed:
                status.update( {'hostname': rhost, 'pid': rpid } )
                running_hosts[status['current_bucket']] = status
            else:
                running_hosts[rhost][rpid] = value

        return running_hosts

    def read_all_omap_vals(self,object_name: str) -> Dict:
        kvdata = {}
        last_omap_key = ""
        batch_size = 5000

        with rados.ReadOpCtx() as op:
            while True:
                omap_iterator, ret = self.sync_ioctl.get_omap_vals(
                    op,
                    start_after=last_omap_key,
                    filter_prefix="",
                    max_return=batch_size
                )

                if not ret==0:
                    logger.critical("Failed to setup omap data read.")
                    exit(1)

                try:
                    self.sync_ioctl.operate_read_op(op, object_name)
                except rados.ObjectNotFound:
                    logger.error(f"Missing Object {object_name}")
                    break

                omap_batch = list(omap_iterator)

                if not omap_batch:
                    break

                for k,v in omap_batch:
                    kvdata[k] = json.loads(v)

                last_omap_key = omap_batch[-1][0]

                # If we received fewer keys than max_return, we've reached the end
                if len(omap_batch) < batch_size:
                    break

        return kvdata

    def get_buckets_state(self) -> Dict:
        buckets_state = {}
        for i in range(self.shard_count):
            buckets_state |= self.read_all_omap_vals(f"{self.SYNC_OBJECT_NAME}.{i}")

        return buckets_state

    def read_gap_header(self, cache: bool = False) -> Dict:
        if not cache:
            self.gap_header_data = {}
            return self.read_all_omap_vals(self.RESULTS_OBJECT_NAME)

        if not self.gap_header_data:
            self.gap_header_data = self.read_all_omap_vals(self.RESULTS_OBJECT_NAME)

        return self.gap_header_data

    def read_gap_results(self, bucket_name: str, cache: bool = False) -> Dict:
        bucket_gap_results = {}

        if not cache or not self.gap_header_data:
            self.gap_header_data = self.read_gap_header(cache)

        if bucket_name in self.gap_header_data:
            for i in range(1,self.gap_header_data[bucket_name]["results_obj_count"]+1):
                results_object = f"{self.RESULTS_OBJECT_NAME}.{bucket_name}.{i}"
                bucket_gap_results |= json.loads(self.sync_ioctl.read(results_object).decode("ascii"))

        if not cache:
            self.gap_header_data = None

        return bucket_gap_results

    def generate_gap_list(self,verify: bool = False, bucket_list: List = [], exclude_bucket_list: List = []) -> None:
        logger.info("Generating gap list report")
        gap_results = {}
        found_count = 0
        missing_count = 0

        try:
            self.sync_ioctl.stat(self.SYNC_OBJECT_NAME)
        except rados.ObjectNotFound:
            logger.critical(f"No primary sync object found - {self.SYNC_OBJECT_NAME}.  Exiting")
            exit(1)

        logger.debug(f"Found primary sync object: {self.SYNC_OBJECT_NAME}")

        try:
            self.sync_ioctl.stat(self.RESULTS_OBJECT_NAME)
        except rados.ObjectNotFound:
            logger.critical(f"No primary results object found - {self.RESULTS_OBJECT_NAME}.  Exiting")
            exit(1)

        logger.debug(f"Found results sync object: {self.RESULTS_OBJECT_NAME}")
        running_hosts = self.get_running_hosts()

        if len(bucket_list) == 0:
            bucket_list = list(self.read_gap_header(cache = True))
 
        for bucket_name in bucket_list:
            if bucket_name in exclude_bucket_list:
                ceph.skipped_bucket_count += 1
                continue
            gap_results[bucket_name] = self.read_gap_results(bucket_name,cache = True)
            if verify:
                for object_name in list(gap_results[bucket_name].keys()):
                    object_results = gap_results[bucket_name][object_name]
                    for rados_object in object_results["missing_rados_objects"]:
                        logger.debug(f"Verifying {rados_object}")
                        self.in_flight.append({"comp": self.aio_stat_object(rados_object), "bucket_name": bucket_name, "rados_object": rados_object, "object_name": object_name })

                    while len(self.in_flight) >= self.max_inflight:
                        oldest_op = self.in_flight.popleft()
                        results = []
                        for comp in oldest_op['comp']:
                            comp.wait_for_complete()
                            results.append(comp.get_return_value())

                        if results.count(0) != len(oldest_op['comp']):
                            continue
                        else:
                            logger.debug(f"Found {oldest_op['rados_object']}")
                            found_count += 1
                            gap_results[oldest_op['bucket_name']][oldest_op['object_name']]['missing_rados_objects'].remove(oldest_op['rados_object'])
                            if len(gap_results[oldest_op['bucket_name']][oldest_op['object_name']]['missing_rados_objects']) == 0:
                                del gap_results[oldest_op['bucket_name']][oldest_op['object_name']]
                            if len(gap_results[oldest_op['bucket_name']]) == 0:
                                del gap_results[oldest_op['bucket_name']]

                while len(self.in_flight):
                    oldest_op = self.in_flight.popleft()
                    results = []
                    for comp in oldest_op['comp']:
                        comp.wait_for_complete()
                        results.append(comp.get_return_value())

                    if results.count(0) != len(oldest_op['comp']):
                        continue
                    else:
                        logger.debug(f"Found {oldest_op['rados_object']}")
                        found_count += 1
                        gap_results[oldest_op['bucket_name']][oldest_op['object_name']]['missing_rados_objects'].remove(oldest_op['rados_object'])
                        if len(gap_results[oldest_op['bucket_name']][oldest_op['object_name']]['missing_rados_objects']) == 0:
                            del gap_results[oldest_op['bucket_name']][oldest_op['object_name']]
                        if len(gap_results[oldest_op['bucket_name']]) == 0:
                            del gap_results[oldest_op['bucket_name']]


        if self.json:
            dump_object = {"active_processes": True if len(running_hosts) else False, "verified": verify }
            if verify:
                dump_object['found_count'] = found_count
            print(json.dumps(dump_object | { "gap_results": gap_results }))
        else:
            missing_text = "MISSING"
            if verify:
                missing_text = "STILL MISSING"
            for bucket_name,bucket_data in gap_results.items():
                for object_name,object_data in bucket_data.items():
                    for rados_object in object_data['missing_rados_objects']:
                        print(f"s3://{bucket_name}/{object_name} {missing_text} {rados_object}")
                        missing_count += 1

        if not self.json:
            verified="Verified " if verify else ""
            found=f", but found {found_count} rados objects" if found_count else ""
            print(f"{verified}Missing {missing_count} rados objects{found}")

        if len(running_hosts) and not self.json:
            print("WARNING: There are active gap list processes, results may be incomplete.")


    def generate_report(self) -> None:
        logger.info("Generating bucket metadata report")
        try:
            self.sync_ioctl.stat(self.SYNC_OBJECT_NAME)
        except rados.ObjectNotFound:
            logger.critical("No primary sync object found.  Exiting")
            exit(1)

        logger.debug(f"Found primary sync object: {self.SYNC_OBJECT_NAME}")
        bucket_metadata_header = json.loads(self.sync_ioctl.read(self.SYNC_OBJECT_NAME).decode("ascii"))
        self.shard_count = bucket_metadata_header["shard_count"]
        self.total_bucket_count = bucket_metadata_header["bucket_count"]

        running_hosts = self.get_running_hosts()
        bucket_state = self.get_buckets_state()
        if self.json:
            print(json.dumps({"active_hosts": running_hosts,"bucket_state": bucket_state}))
        else:
            if len(running_hosts):
                print("\nRunning Hosts:")
                total_processed=0
                for host,data in running_hosts.items():
                    host_processed=0
                    print(f"  {host} ( {len(data.items())} processes )")
                    for pid,status in data.items():
                        dt = datetime.fromtimestamp(status['epoch']).strftime('%Y-%m-%d %H:%M:%S')
                        print(f"    PID: {pid}, Bucket: {status['current_bucket']}, Rados Count: {status['rados_obj_count']}, Gap Count: {status['gap_count']}, Bucket Counter: {status['bucket_counter']}, Last Updated: {dt}")
                        host_processed += status['bucket_counter']
                        total_processed += status['bucket_counter']
                    print(f"  Host processed: {host_processed}")
                print(f"Total processed: {total_processed} of {self.total_bucket_count}")
            else:
                print("No active hosts.")

            if len(bucket_state):
                print("\nBucket State:")
                for bucket_name,data in bucket_state.items():
                    print(f"  {bucket_name}:: Rados Count: {data['rados_obj_count']}, ", end="")
                    if data['end_time']:
                        dt = datetime.fromtimestamp(data['end_time']).strftime('%Y-%m-%d %H:%M:%S')
                        hum = seconds_to_human(data['total_time_secs'])
                        scope = f" (prefix: '{data['match']}')" if data.get('match') else ""
                        print(f"Last Scan Completed: {dt} in {hum}, found {data['gap_count']} gaps{scope}.")
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

def seconds_to_human(secs: float) -> str:
    secs = float(secs)
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    minutes = int((secs % 3600) // 60)
    seconds = secs % 60
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} m")
    if seconds > 0:
        parts.append(f"{seconds} s")
    return " ".join(parts)

def check_aio_result(op_obj: Dict) -> Union[Dict, int, None]:
    results = []
    for comp in op_obj['comp']:
        comp.wait_for_complete()
        results.append(comp.get_return_value())

    if results.count(0) != len(op_obj['comp']):
        if op_obj['poolidx'] == 0:
            logger.info(f"{op_obj['rados_object']} not found in default pool, checking remaining pools.")
            op_obj['comp'] = ceph.aio_stat_object(op_obj['rados_object'],1)
            op_obj['poolidx'] = 1
            return op_obj
        else:
            ceph.add_result_entry(bucket_name=op_obj['bucket'], object_name=op_obj['user_object'], rados_object=op_obj['rados_object'])
            logger.debug(f"[NOT FOUND] s3://{op_obj['bucket']}/{op_obj['user_object']} MISSING {op_obj['rados_object']}")
            return 1

    return None

def output_status(bucket_name: str = '', rados_obj_count: int = 0, delta_start: int = 0, delta_last: int = 0):
        logger.info(f"[Status] Submitted {rados_obj_count} rados objects in {delta_start:.3f} seconds ( last 10k in {delta_last:.3f} seconds ) for {bucket_name}.")
        ceph.touch_sync_state(bucket_name=bucket_name, rados_obj_count=rados_obj_count, gap_count=ceph.bucket_gap_count)

def process_bucket(bucket_name: str, force_scan = False) -> None:
    bucket_meta = None

    if ceph.total_bucket_count:
        logger.info(f"Checking {bucket_name} via sync state")
        is_scanning = ceph.is_bucket_scanning(bucket_name)
        if is_scanning:
            logger.info(f"Bucket {bucket_name} is actively being scanned on {is_scanning['hostname']} ({is_scanning['pid']})")
            return None
        bucket_meta = ceph.get_bucket_meta(bucket_name)

    if bucket_meta:
        bucket_meta = json.loads(bucket_meta)
        dt = datetime.fromtimestamp(bucket_meta["end_time"]).strftime('%Y-%m-%d %H:%M:%S')
        hum = seconds_to_human(ceph.max_age)
        scanned_match = bucket_meta.get("match", "")
        if time.time() - bucket_meta["end_time"] > int(ceph.max_age):
            logger.info(f"Bucket {bucket_name} end time ( {dt} ) is more than {hum} old.  Processing again.")
        elif not ceph.match.startswith(scanned_match):
            logger.info(f"Bucket {bucket_name} last scan ( {dt} ) only covered prefix '{scanned_match}'.  Processing again.")
        else:
            logger.info(f"Bucket {bucket_name} end time ( {dt} ) is less than {hum} old.  Skipping.")
            return None

    logger.info(f"Processing {bucket_name}")
    ceph.processed_bucket_count += 1

    bucket_rados_obj_count = 0
    processed_count = 0
    starttime = laststatus = round(time.time(),3)
    if ceph.total_bucket_count:
        ceph.start_bucket(bucket_name,ceph.match)

    with subprocess.Popen(ceph.BUCKET_RADOSLIST_COMMAND + [f"--bucket={bucket_name}"], bufsize=1048576, shell=False, \
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as brl:
        for brl_line in io.TextIOWrapper(brl.stdout, encoding="utf-8"):
            object_data = brl_line.strip().split(ceph.FIELD_SEPARATOR)
            if ceph.match and not object_data[2].startswith(ceph.match):
                continue

            bucket_rados_obj_count += 1
            if bucket_rados_obj_count % ceph.report_every_x_object_count == 0:
                nowtime = round(time.time(),3)
                output_status(bucket_name, bucket_rados_obj_count, nowtime - starttime, nowtime - laststatus)
                laststatus = nowtime


            ceph.in_flight.append({"comp": ceph.aio_stat_object(object_data[0],0), "rados_object": object_data[0], "bucket": bucket_name, "user_object": object_data[2], "poolidx": 0})

            while len(ceph.in_flight) >= ceph.max_inflight:
                processed_count += 1
                res = check_aio_result(ceph.in_flight.popleft())
                if type(res) is dict:
                    ceph.in_flight.append(res)

    while len(ceph.in_flight):
        res = check_aio_result(ceph.in_flight.popleft())
        if type(res) is dict:
            ceph.in_flight.append(res)

    ceph.write_result_object(bucket_name, final = True)

    nowtime = round(time.time(),3)
    output_status(bucket_name, bucket_rados_obj_count, nowtime - starttime, nowtime - laststatus)

    if ceph.total_bucket_count:
        ceph.end_bucket(bucket_name,bucket_rados_obj_count)

def process_list(bucket_list: List = [], exclude_bucket_list: List = []) -> None:
    if len(bucket_list):
        logger.info(f"Starting processing of {len(bucket_list)} bucket(s)")
        ceph.populate_sync_objects(1, len(bucket_list))

        for bucket in bucket_list:
            if bucket not in exclude_bucket_list:
                process_bucket(bucket)
            else:
                ceph.skipped_bucket_count += 1
                logger.debug(f"Found {bucket} in exclude_bucket_list, skipping.")
        return None

    # If we get here, we're processing -all- buckets
    # Get a count of the buckets to determine sync object count
    with subprocess.Popen(ceph.BUCKET_LIST_COMMAND, bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as bl, \
        subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as jql, \
        subprocess.Popen(["wc","-l"], stdin=jql.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as bc:

        bl.stdout.close()
        jql.stdout.close()

        bc_out, _ = bc.communicate()
        ceph.total_bucket_count = int(bc_out.decode("ascii").strip())

    logger.info(f"Starting processing of {ceph.total_bucket_count} bucket(s)")

    ceph.shard_count = int(ceph.total_bucket_count/400) + 1
    ceph.populate_sync_objects(ceph.shard_count, ceph.total_bucket_count)

    if args.norandom: # Do not randomize the bucket list, optional.
        with subprocess.Popen(ceph.BUCKET_LIST_COMMAND, bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as bl:
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

                    if bucket not in exclude_bucket_list:
                        process_bucket(bucket)
                    else:
                        ceph.skipped_bucket_count += 1
                        logger.debug(f"Found {bucket} in exclude_bucket_list, skipping.")

    else: # Randomize the bucket list, this is the default.
        with subprocess.Popen(ceph.BUCKET_LIST_COMMAND, bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as bl, \
            subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as jql, \
            subprocess.Popen(["sort","--random-sort"],stdin=jql.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as sortl:

            bl.stdout.close()
            jql.stdout.close()

            for sortl_line in io.TextIOWrapper(sortl.stdout, encoding="utf-8"):
                bucket = sortl_line.strip()
                if bucket not in exclude_bucket_list:
                    process_bucket(bucket)
                else:
                    ceph.skipped_bucket_count += 1
                    logger.debug(f"Found {bucket} in exclude_bucket_list, skipping.")

    return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-run / Multi-host capable rgw-gap-list tool")
    parser.add_argument("-a", "--maxage",  default = 7*86400, type=int, help="Maximum age (in seconds) of last scan before rescan is forced.  Default 7 days.")
    parser.add_argument("-b", "--bucketlist",  default = '', help="Optional: Bucket(s) to operate on, default is all buckets, quoted space separated list is supported. Supercedes -l.")
    parser.add_argument("-e", "--excludelist",  default = '', help="Optional: Bucket(s) to skip, default is process all buckets, quoted space separated list is supported. Supercedes -f.")
    parser.add_argument("-f", "--excludefile", default = '', help="Optional: File with list of bucket(s) to skip, should be one bucket name per line.")
    parser.add_argument("-c", "--conf", default = '/etc/ceph/ceph.conf', help="Ceph conf file to use, default '/etc/ceph/ceph.conf'")
    parser.add_argument("-d", "--delete",  default = False, action="store_true", help="Remove all sync objects and Exit. Used to clear all syncronized bucket status data.")
    parser.add_argument("-g", "--gaps",  default = False, action="store_true", help="Dump the gap results from RADOS object contents.  All other options are ignore ( except -j )")
    parser.add_argument("-i", "--inflight",  default = 15000, type=int, help="Maximum number of in-flight ops to allow without a response.  Default: 15000")
    parser.add_argument("-l", "--listfile", default = '', help="Optional: Bucket list file, should be one bucket name per line.")
    parser.add_argument("-m", "--match", default = '', help="Specify a prefix match for the object names.  Only objects matching this prefix will be checked for gaps.")
    parser.add_argument("-n", "--norandom", default = False, action="store_true", help="By default, the script randomizes the list of buckets before processing.  On large bucket count environments, this may cause significant delay before start of processing due to the way the randomizing occurs.  Set '-n' to Not Randomize the list to remove this delay.")
    parser.add_argument("--namespace", default = f'rgw-gap-list', help="What namespace to use for sync / results objects. Default: rgw-gap-list")
    parser.add_argument("-p", "--pool", default = 'default.rgw.buckets.data default.rgw.buckets.non-ec', help="Bucket Data Pool(s), default 'default.rgw.buckets.data default.rgw.buckets.non-ec', quoted space separated list is supported.")
    parser.add_argument("-s", "--syncpool", default = 'default.rgw.buckets.index', help="Synchronization / Queuing pool for the script ot use, default 'default.rgw.buckets.index'.")
    parser.add_argument("-r", "--report",  default = False, action="store_true", help="Generate bucket scrub metadata report.")
    parser.add_argument("-j", "--json",  default = False, action="store_true", help="Use JSON format for bucket scrub metadata report. Only considered with -g, -r and -x")
    parser.add_argument("-v", "--verbosity", default = 0, action="count", help="Optional: Verbosity level, multiple -v's are supported for higher verbosity, example: -vvv")
    parser.add_argument("-x", "--verify", default = False, action="store_true", help="Used to verify the results from a prior run.")
    args = parser.parse_args()

    debug_level = min([len(LOG_LEVELS)-1,args.verbosity])

    logging.basicConfig(
        level=LOG_LEVELS[debug_level],
        format=f'%(asctime)s {MYHOST}.{MYPID} %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )

    logger = logging.getLogger('rgw-gap-list')

    bucket_list = []
    exclude_bucket_list = []
    if args.excludelist:
        exclude_bucket_list = [ bn for bn in args.excludelist.split(" ") if re.match(r"^[a-z0-9][a-z0-9.-]{1,253}[a-z0-9]$",bn) ]
        if len(exclude_bucket_list) == 0:
            logger.critical("The provided exclude bucket list did not contain any valid bucket names.  Please confirm proper s3 bucket names are present.")
            exit(1)
    elif args.excludefile:
        with open(args.excludefile) as elist:
            exclude_bucket_list = [ line.strip() for line in elist if re.match(r"^[a-z0-9][a-z0-9.-]{1,253}[a-z0-9]$",line.strip()) ]
        if len(exclude_bucket_list) == 0:
            logger.critical("The provided exclude bucket list file did not contain any valid bucket names.  Please confirm proper s3 bucket names are present.")
            exit(1)

    if args.bucketlist:
        bucket_list = [ bn for bn in args.bucketlist.split(" ") if re.match(r"^[a-z0-9][a-z0-9.-]{1,253}[a-z0-9]$",bn) ]
        if len(bucket_list) == 0:
            logger.critical("The provided bucket list did not contain any valid bucket names.  Please confirm proper s3 bucket names are present.")
            exit(1)
    elif args.listfile:
        with open(args.listfile) as blist:
            bucket_list = [ line.strip() for line in blist if re.match(r"^[a-z0-9][a-z0-9.-]{1,253}[a-z0-9]$",line.strip()) ]
        if len(bucket_list) == 0:
            logger.critical("The provided bucket list file did not contain any valid bucket names.  Please confirm proper s3 bucket names are present.")
            exit(1)

    with CephClusterConnection(ceph_conf=args.conf, pool_names=args.pool.split(" "), sync_pool=args.syncpool) as ceph:
        ceph.namespace = args.namespace.strip()
        ceph.max_inflight = max(args.inflight,1)
        ceph.match = args.match
        ceph.max_age = max(0,int(args.maxage))
        ceph.json = args.json
        if args.gaps:
            ceph.generate_gap_list(bucket_list = bucket_list, exclude_bucket_list = exclude_bucket_list)
        elif args.report:
            ceph.generate_report()
        elif args.delete:
            ceph.delete_gap_objects()
            ceph.delete_sync_objects()
        elif args.verify:
            ceph.generate_gap_list(bucket_list = bucket_list, exclude_bucket_list = exclude_bucket_list, verify=True)
        else:
            process_list(bucket_list = bucket_list, exclude_bucket_list = exclude_bucket_list)
