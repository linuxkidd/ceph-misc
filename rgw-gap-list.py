#!/usr/bin/env python

"""
By: Michael J. Kidd (linuxkidd)
Last Revision: 2026-04-24
Version: 0.9

Performs a Rados Gateway Gap analysis
(wat?)

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
   bucket index pool.
3. Since the state is synchronized via Ceph RADOS... multiple instances can be
   running in parallel, even across different hosts!
4. This script can also be ran with the '-r' option to generate a report of 
   current running hosts, and state per bucket. ( add '-j' for json output )
5. This script can verify its own results by passing the '-x' flag followed
   by the gap-list-result file from a previous run.

Usage can be had by passing '--help' to the script.

Tips:
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
  the synchronized state will not work well ( or at all ).
- To verify the results of multiple script runs ( whether parallel on a single
  host, or across multiple hosts) by catting all their results into a single
  file, then providing that combined file with the '-x' parameter.

Enjoy!
"""

import argparse
from datetime import datetime
import hashlib
import io
import json
import logging
import os
import rados
import signal
import subprocess
import sys
import time

log_levels = [ 50, 30, 20, 10 ]
bucket_list_command = ["radosgw-admin", "bucket", "list"]
fs = "\xfe"
bucket_radoslist_command = ['radosgw-admin', 'bucket', 'radoslist', f'--rgw-obj-fs={fs}']
mypid = os.getpid()
myhost = os.uname().nodename
bucket_count = 0
bucket_count_idx = 0
missing_count = 0
shard_count = 1
sync_object_name = "rgw-gap-list-sync-object"
report_every_x_object_count = 10000

def signal_handler(sig, frame):
    print(f'Received {sig}, Terminating')
    sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

class CephClusterConnection:
    """
    A context manager to handle connecting to and disconnecting from a 
    Ceph RADOS cluster, ensuring resources are cleaned up properly.
    """
    def __init__(self, ceph_conf='', pool_names=[], sync_pool=''):
        self.ceph_conf = ceph_conf
        self.cluster = None
        self.pool_names = pool_names
        self.pool_ioctl = []
        self.sync_pool = sync_pool
        self.sync_ioctl = None

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

            if(len(self.pool_names)>0):
                for pool_name in self.pool_names:
                    try:
                        self.pool_ioctl.append(self.cluster.open_ioctx(pool_name))
                    except rados.ObjectNotFound:
                        logger.critical(f"Pool {pool_name} not present.  Exiting.")
                        exit(1)

            return self
        except rados.Error as e:
            logger.critical(f"Failed to connect to the Ceph cluster: {e}")
            raise

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

    def stat_object(self, object_name=""):
        """
        Retrieves the size and last modified time of an object.
        """
        if not self.cluster:
            logger.critical("Cluster is not connected.")
            raise RuntimeError("Cluster is not connected.")

        # iterate over each pool attempting to stat the object.
        for ioctx in self.pool_ioctl:
            try:
                size, mtime = ioctx.stat(object_name)
                logger.debug(f"[FOUND] Found object named {object_name}")
                return True

            except rados.ObjectNotFound:
                continue

            except Exception as e:
                logger.error(f"[Exception] While attempting to stat {object_name}: {e}")
                return False

        return False

    def delete_sync_objects(self):
        logger.critical(f"Deleting sync objects...")
        shard_count = 1
        try:
            self.sync_ioctl.stat(sync_object_name)
        except rados.ObjectNotFound:
            pass
        else:
            bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            shard_count = bucket_metadata_header["shard_count"]
            logger.info(f"Deleting primary sync object: {sync_object_name}")
            self.sync_ioctl.remove_object(sync_object_name)


        for i in range(shard_count):
            try:
                self.sync_ioctl.stat(f"{sync_object_name}.{i}")
            except rados.ObjectNotFound:
                pass
            else:
                logger.info(f"Deleting sync object: {sync_object_name}.{i}")
                self.sync_ioctl.remove_object(f"{sync_object_name}.{i}")

        logger.critical(f"Finished deleting sync objects.")

    def populate_sync_objects(self,shard_count=1):
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
            if len(running_hosts):
                shard_count = bucket_metadata_header["shard_count"]
            else:
                logger.info("No running hosts, resetting sync objects.")
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
        return int.from_bytes(digest,byteorder="big") % shard_count

    def touch_sync_state(self, bucket_name='', rados_count=0 ):
        with rados.WriteOpCtx() as write_op:
            sync_state = { "epoch": round(time.time(),3), "current_bucket": bucket_name, "rados_count": rados_count, "bucket_counter": bucket_count_idx, "total_buckets": bucket_count }
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
        sync_metadata = { "hostname": myhost, "pid": mypid, "rados_obj_count": 0, "start_time": round(time.time(),3), "end_time": 0 }
        with rados.WriteOpCtx() as write_op:
            # Set bucket metadata
            self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(sync_metadata), ))
            self.sync_ioctl.operate_write_op(write_op, f"{sync_object_name}.{shardid}")
        self.touch_sync_state(bucket_name,0)
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

    def end_bucket(self,bucket_name,rados_count):
        shardid = self.hash_bucketname(bucket_name)
        logger.info(f"Setting bucket end metadata to sync shard {shardid}")
        bucket_meta = self.get_bucket_meta(bucket_name)
        if bucket_meta:
            bucket_meta = json.loads(bucket_meta)
            bucket_meta["end_time"] = round(time.time(),3)
            bucket_meta["rados_obj_count"] = rados_count
            bucket_meta["total_time_secs"] = round(bucket_meta["end_time"] - bucket_meta["start_time"],3)
            logger.debug(f"Bucket meta: {bucket_meta}")
            with rados.WriteOpCtx() as write_op:
                # Set bucket metadata
                self.sync_ioctl.set_omap(write_op,(bucket_name,),( json.dumps(bucket_meta), ))
                self.sync_ioctl.operate_write_op(write_op, f"{sync_object_name}.{shardid}")
            self.touch_sync_state(bucket_name,rados_count)
            return True
        else:
            logger.error(f"Bucket start metadata is missing from shard {shardid}")
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

    def get_buckets_state(self,shard_count=0):
        bucket_state = {}
        with rados.ReadOpCtx() as read_op:
            for i in range(shard_count):
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
            ceph.touch_sync_state(bucket_name='', rados_count=0)
            logger.debug(f"Found primary sync object: {sync_object_name}")
            bucket_metadata_header = json.loads(self.sync_ioctl.read(sync_object_name).decode("ascii"))
            shard_count = bucket_metadata_header["shard_count"]

        running_hosts = self.get_running_hosts()
        bucket_state = self.get_buckets_state(shard_count)
        if args.json:
            print(json.dumps({"active_hosts": running_hosts,"bucket_state": bucket_state}))
        else:
            if len(running_hosts):
                print("\nRunning Hosts:")
                for host,data in running_hosts.items():
                    print(f"  {host}")
                    for pid,status in data.items():
                        dt = datetime.fromtimestamp(status['epoch']).strftime('%Y-%m-%d %H:%M:%S')
                        print(f"    PID: {pid}, Bucket: {status['current_bucket']}, Rados Count: {status['rados_count']}, Bucket Counter: {status['bucket_counter']}, Last Updated: {dt}")
            else:
                print("No active hosts.")

            if len(bucket_state):
                print("\nBucket State:")
                for bucket_name,data in bucket_state.items():
                    print(f"  {bucket_name}:: Rados Count: {data['rados_obj_count']}, ", end="")
                    if data['end_time']:
                        dt = datetime.fromtimestamp(data['end_time']).strftime('%Y-%m-%d %H:%M:%S')
                        hum = seconds_to_human(data['total_time_secs'])
                        print(f"Last Scan Completed: {dt} in {hum}")
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


def process_bucket(bucket_name):
    global bucket_count
    global bucket_count_idx
    global missing_count
    bucket_count_idx+=1
    logger.info(f"Checking {bucket_name} via sync state")
    is_scanning = ceph.is_bucket_scanning(bucket_name)
    if is_scanning:
        logger.info(f"Bucket {bucket_name} is actively being scanned on {is_scanning['hostname']} ({is_scanning['pid']})")
        return None

    bucket_meta = ceph.get_bucket_meta(bucket_name)
    if bucket_meta:
        bucket_meta = json.loads(bucket_meta)
        if time.time() - bucket_meta["end_time"] > int(args.maxage):
            dt = datetime.fromtimestamp(bucket_meta["end_time"]).strftime('%Y-%m-%d %H:%M:%S')
            hum = seconds_to_human(args.maxage)
            logger.info(f"Bucket {bucket_name} end time ( {dt} ) is more than {hum} old.  Processing again.")
        else:
            dt = datetime.fromtimestamp(bucket_meta["start_time"]).strftime('%Y-%m-%d %H:%M:%S')
            hum = seconds_to_human(args.maxage)
            logger.info(f"Bucket {bucket_name} start time ( {dt} ) is less than {hum} old.  Skipping.")
            return None

    logger.info(f"Processing {bucket_name}")
    brl = subprocess.Popen(bucket_radoslist_command + [f"--bucket={bucket_name}"], bufsize=1048576, shell=False, \
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    linecount=0
    starttime=round(time.time(),3)
    laststatus=round(time.time(),3)
    if bucket_count:
        ceph.start_bucket(bucket_name)
    for brl_line in io.TextIOWrapper(brl.stdout, encoding="utf-8"):
        object_data = brl_line.strip().split(fs)
        linecount+=1
        if linecount % report_every_x_object_count == 0:
            nowtime = round(time.time(),3)
            deltaStart = nowtime - starttime
            deltaLast  = nowtime - laststatus
            laststatus = nowtime
            logger.info(f"[Status] Processed {linecount} rados objects in {deltaStart:.3f} seconds ( last 10k in {deltaLast:.3f} seconds ) for {bucket_name}.")
            ceph.touch_sync_state(bucket_name=bucket_name, rados_count=linecount)
        if not ceph.stat_object(object_data[0]):
            missing_count+=1
            outfile.write(f"s3://{object_data[1]}/{object_data[2]} MISSING {object_data[0]}\n")
            logger.error(f"[NOT FOUND] Object not found: {object_data[0]} for s3://{object_data[1]}/{object_data[2]}")

    if bucket_count:
        ceph.end_bucket(bucket_name,linecount)
    nowtime = round(time.time(),3)
    delta = nowtime - starttime
    logger.info(f"[Status] Processed {linecount} rados objects in {delta:.3f} seconds for {bucket_name}.")
    return None

def verify_results():
    import re
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
    mcount=0
    fcount=0
    with open(args.verify) as vlist:
        for line in vlist:
            robj = re.sub(r'^.* MISSING ', '', line.strip())
            if not ceph.stat_object(robj):
                mcount+=1
                outfile.write(re.sub(f' MISSING ',' STILL MISSING ',line))
            else:
                fcount+=1
    found = "."
    if fcount:
        found = ", but {fcount} were found!"
    logger.critical(f"Verified {mcount} rados objects still missing{found}")
    return None

def process_list():
    global bucket_count
    if not args.bucketlist=='':
        bucket_list = args.bucketlist.split(" ")
        bc = len(bucket_list)
        logger.info(f"Starting processing of {bc} bucket(s)")
        for bucket in args.bucketlist.split(" "):
            process_bucket(bucket)
        return None

    if not args.listfile=='':
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
    bl = subprocess.Popen(bucket_list_command, bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    jql = subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bc  = subprocess.Popen(["wc","-l"], stdin=jql.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bucket_count = int(bc.stdout.readline().decode("ascii").strip())
    logger.info(f"Starting processing of {bucket_count} bucket(s)")

    shard_count = int(bucket_count/400) + 1
    ceph.populate_sync_objects(shard_count)

    bl = subprocess.Popen(bucket_list_command, bufsize=1048576, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    jql = subprocess.Popen(["jq","-cr",".[]"],stdin=bl.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sortl = subprocess.Popen(["sort","--random-sort"],stdin=jql.stdout,stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    for sortl_line in io.TextIOWrapper(sortl.stdout, encoding="utf-8"):
        bucket = sortl_line.strip()
        process_bucket(bucket)

    return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-run / Multi-host capable rgw-gap-list tool")
    parser.add_argument("-a", "--maxage",  default = 7*86400, help="Maximum age (in seconds) of last scan before rescan is forced.  Default 7 days.")
    parser.add_argument("-b", "--bucketlist",  default = '', help="Optional: Bucket(s) to operate on, default is all buckets, quoted space separated list is supported.")
    parser.add_argument("-c", "--conf", default = '/etc/ceph/ceph.conf', help="Ceph conf file to use, default '/etc/ceph/ceph.conf'")
    parser.add_argument("-d", "--delete",  default = False, action="store_true", help="Remove all sync objects and Exit. Used to clear all syncronized bucket status data.")
    parser.add_argument("-l", "--listfile", default = '', help="Optional: Bucket list file, should be one bucket name per line.")
    parser.add_argument("-o", "--outfile", default = f'gap-list-results.{mypid}', help="Optional: results file name, default: gap-list-results.###")
    parser.add_argument("-p", "--pool", default = 'default.rgw.buckets.data', help="Bucket Data Pool(s), default 'default.rgw.buckets.data', quoted space separated list is supported.")
    parser.add_argument("-s", "--syncpool", default = 'default.rgw.buckets.index', help="Synchronization / Queuing pool for the script ot use, default 'default.rgw.buckets.index'.")
    parser.add_argument("-r", "--report",  default = False, action="store_true", help="Generate bucket scrub metadata report.")
    parser.add_argument("-j", "--json",  default = False, action="store_true", help="Use JSON format for bucket scrub metadata report. Only considered with -r")
    parser.add_argument("-v", "--verbosity", default = 0, action="count", help="Optional: Verbosity level, multiple -v's are supported for higher verbosity, example: -vvv")
    parser.add_argument("-x", "--verify", default = '', help="Used to veryify the results file from a prior run, supply the prior run gap-list-results file.")
    args = parser.parse_args()
    debug_level = min([len(log_levels),args.verbosity])

    logging.basicConfig(
        level=log_levels[debug_level],
        format=f'%(asctime)s {myhost} %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )

    logger = logging.getLogger('rgw-gap-list')

    if args.report:
        with CephClusterConnection(ceph_conf=args.conf, pool_names=args.pool.split(" "), sync_pool=args.syncpool) as ceph:
            ceph.generate_report()
            exit()

    if args.outfile == f'gap-list-results.{mypid}' and args.verify:
        args.outfile = f'gap-list-verify-results.{mypid}'

    with open(args.outfile,"w") as outfile:
        with CephClusterConnection(ceph_conf=args.conf, pool_names=args.pool.split(" "), sync_pool=args.syncpool) as ceph:
            if args.delete:
                ceph.delete_sync_objects()
            elif args.verify:
                verify_results()
                logger.critical(f"There were {missing_count} missing rados objects. Results are in {args.outfile}.")
            else:
                process_list()
                logger.critical(f"There were {missing_count} missing rados objects. Results are in {args.outfile}")
