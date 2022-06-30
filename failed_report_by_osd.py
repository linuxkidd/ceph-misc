#!/usr/bin/env python3

import sys

count = {}

for line in sys.stdin:
    lineidx=0
    rfbidx = line.find(' reported failed by ')
    while True:
        osdidx = line.find('osd.',lineidx)
        if osdidx == -1:
            break
        if osdidx < rfbidx:
            pos=0
            end = line.find(' ',osdidx)
        else:
            pos=1
            end = len(line)-1
        if line[osdidx:end] not in count:
            count[line[osdidx:end]]=[ 0, 0 ]
        count[line[osdidx:end]][pos]+=1
        lineidx=end

print("{0:10s} = {1:10s}, {2:10s}".format("osd.id","reported","reporting"))
for osdid in count:
    print("{0:10s} = {1:10d}, {2:10d}".format(osdid, count[osdid][0], count[osdid][1]))
