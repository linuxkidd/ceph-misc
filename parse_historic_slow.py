#!/usr/bin/env python

import sys
import simplejson as json
from datetime import datetime


obj=json.load(sys.stdin)

for op in obj["Ops"]:
    lastepoch=0
    longestop=""
    longestdt=""
    longestsec=0
    for event in op["type_data"]["events"]:
        utc_time = datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S.%f")
        gmtime = (utc_time - datetime(1970, 1, 1)).total_seconds()
        if(lastepoch>0):
            delta=gmtime-lastepoch
        else:
            delta=0
        lastepoch=gmtime
        if(delta>longestsec):
            longestsec=delta
            longestdt=event["time"]
            longestop=event["event"]
    print("{0:8.4f},{1:s},{2:s},{3:s}".format(longestsec,longestdt,longestop,op["description"]))
