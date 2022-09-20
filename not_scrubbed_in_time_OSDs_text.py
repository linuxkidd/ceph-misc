#!/usr/bin/env python3

import argparse,re,sys
from datetime import datetime
from collections import Counter

parser = argparse.ArgumentParser()
parser.add_argument("-f", "--file",  default = "", required=True, help="Filename of the 'ceph pg dump --format=json' output")
parser.add_argument("-d", "--deep",  default = 7, type=int, help="Deep Scrub Interval ( days ) - Default: 7")
parser.add_argument("-s", "--scrub", default = 1, type=int, help="Scrub Interval ( days ) - Default: 1")
args = parser.parse_args()

scrub_interval=args.scrub*86400
deep_scrub_interval=args.deep*86400

scrub_osds = {}
deep_scrub_osds = {}
nowtime = None
scrub_pgs = []
deep_scrub_pgs = []

try:
    f = open(args.file,'r')
except:
    print("Failed to open {}.  Does it exist?".format(args.file))
    exit(1)

lines = f.readlines()
for line in lines:
    line = line.rstrip()
    if nowtime is None and re.match('^stamp ', line):
        line_split=line.split()
        nowtime=datetime.strptime(' '.join(line_split[1:3]), '%Y-%m-%d %H:%M:%S.%f')
    elif re.match('^[0-9][0-9]*\.[0-9a-f][0-9a-f]* ',line):
        line_split = line.split()
        last_scrub_delta = ( nowtime - datetime.strptime(' '.join(line_split[21:23]), '%Y-%m-%d %H:%M:%S.%f') ).total_seconds()
        last_deep_scrub_delta = ( nowtime - datetime.strptime(' '.join(line_split[24:26]), '%Y-%m-%d %H:%M:%S.%f') ).total_seconds()

        if last_scrub_delta >= scrub_interval:
            scrub_pgs.append(line_split[0])
            osd_csv = re.sub('[^0-9,]','',line_split[16])
            osd_split = osd_csv.split(',')
            for osd in osd_split:
                try:
                    scrub_osds[str(osd)] += 1
                except:
                    scrub_osds[str(osd)] = 1
 
        if last_deep_scrub_delta >= deep_scrub_interval:
            deep_scrub_pgs.append(line_split[0])
            osd_csv = re.sub('[^0-9,]','',line_split[16])
            osd_split = osd_csv.split(',')
            for osd in osd_split:
                try:
                    deep_scrub_osds[str(osd)] += 1
                except:
                    deep_scrub_osds[str(osd)] = 1


if len(scrub_osds) > 0:
    print('{} Not Scrubbed PGs:\n{}\n\nTop OSDs: '.format(len(scrub_pgs)," ".join(scrub_pgs)))
    for osdid in sorted(scrub_osds, key=scrub_osds.get)[-10:]:
        print(f'osd.{osdid},{scrub_osds[osdid]}')
    print()

if len(deep_scrub_osds) > 0:
    print('{} Not Deep Scrubbed PGs:\n{}\n\nTop OSDs: '.format(len(deep_scrub_pgs)," ".join(deep_scrub_pgs)))
    for osdid in sorted(deep_scrub_osds, key=deep_scrub_osds.get)[-10:]:
        print(f'osd.{osdid},{deep_scrub_osds[osdid]}')
    print()

f.close()
