#!/usr/bin/env python

import json,sys
from datetime import datetime


obj=json.load(sys.stdin)
dt_format = "%Y-%m-%d %H:%M:%S.%f" 

for op in obj["ops"]:
    lastepoch=0
    longestop=""
    longestdt=""
    longestsec=0
    for event in op["type_data"]["events"]:
        if "T" in event["time"]:
            dt_format="%Y-%m-%dT%H:%M:%S.%f"

        utc_time = datetime.strptime(event["time"].split("+")[0], dt_format)
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
