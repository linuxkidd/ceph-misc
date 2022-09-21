#!/usr/bin/env python3

import argparse,json,sys
from datetime import datetime
from collections import Counter

parser = argparse.ArgumentParser()
parser.add_argument("-f", "--file",  default = "", required=True, help="Filename of the 'ceph pg dump --format=json' output")
parser.add_argument("-d", "--deep",  default = 7, type=int, help="Deep Scrub Interval ( days ) - Default: 7")
parser.add_argument("-s", "--scrub", default = 1, type=int, help="Scrub Interval ( days ) - Default: 1")
parser.add_argument("-e", "--deepwarnratio",  default = 0.75, type=float, help="Deep Scrub Warn Ratio - Default: 0.75")
parser.add_argument("-t", "--scrubwarnratio", default = 0.5, type=float, help="Scrub Warn Ratio - Default: 0.5")
args = parser.parse_args()

scrub_interval=args.scrub * 86400 * ( 1 + args.scrubwarnratio )
deep_scrub_interval=args.deep * 86400 * ( 1 + args.deepwarnratio )

scrub_osds = {}
deep_scrub_osds = {}
scrub_pgs = []
deep_scrub_pgs = []

try:
    f = open(args.file)
except:
    print("Failed to open {}.  Does it exist?".format(args.file))
    exit(1)
print("JSON Load Start: {0}".format(datetime.now()),flush=True)
obj=json.load(f)
f.close()
print("JSON Load Complete: {0}".format(datetime.now()),flush=True)

nowtime = datetime.strptime(obj['pg_map']['stamp'], "%Y-%m-%d %H:%M:%S.%f")
for pg in obj['pg_map']['pg_stats']:
    last_scrub_delta = ( nowtime - datetime.strptime(pg["last_scrub_stamp"], "%Y-%m-%d %H:%M:%S.%f") ).total_seconds()
    last_deep_scrub_delta = ( nowtime - datetime.strptime(pg["last_deep_scrub_stamp"], "%Y-%m-%d %H:%M:%S.%f") ).total_seconds()
    if last_scrub_delta >= scrub_interval:
        scrub_pgs.append(pg['pgid'])
        for osd in pg['acting']:
            try:
                scrub_osds[str(osd)]+=1
            except:
                scrub_osds[str(osd)]=1
    if last_deep_scrub_delta >= deep_scrub_interval:
        deep_scrub_pgs.append(pg['pgid'])
        for osd in pg['acting']:
            try:
                deep_scrub_osds[str(osd)]+=1
            except:
                deep_scrub_osds[str(osd)]=1

if len(scrub_osds) > 0:
    print("{} PGs not Scrubbed in time:\n{}\n\nTop OSDs: ".format(len(scrub_pgs),' '.join(scrub_pgs)))
    for osdid in sorted(scrub_osds, key=scrub_osds.get)[-10:]:
        print(f"osd.{osdid},{scrub_osds[osdid]}")
    print()

if len(deep_scrub_osds) > 0:
    print("{} PGs not Deep Scrubbed in time:\n{}\n\nTop OSDs: ".format(len(deep_scrub_pgs),' '.join(deep_scrub_pgs)))
    for osdid in sorted(deep_scrub_osds, key=deep_scrub_osds.get)[-10:]:
        print(f"osd.{osdid},{deep_scrub_osds[osdid]}")
    print()

