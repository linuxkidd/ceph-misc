#!/usr/bin/env python

import sys, json, operator
from datetime import datetime


obj=json.load(sys.stdin)

for op in obj["Ops"]:
    timespent={}
    lastepoch=0
    longestop=""
    longestdt=""
    longestsec=0
    firstdt=""
    for event in op["type_data"]["events"]:
        if(firstdt==""):
            firstdt=event["time"]
        utc_time = datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S.%f")
        gmtime = (utc_time - datetime(1970, 1, 1)).total_seconds()
        if(lastepoch>0):
            delta=gmtime-lastepoch
        else:
            delta=0
        if(event["event"] not in timespent):
            timespent[event["event"]]=0
        timespent[event["event"]]+=delta
        lastepoch=gmtime
    sortedtime=sorted(timespent.items(), key=lambda kv: kv[1])
    print("{0} {1}".format(firstdt,op["description"]))
    for event, etime in reversed(sorted(timespent.iteritems(), key=lambda (k,v): (v,k))):
        print("\t{0:8.4f} {1:s}".format(timespent[event],event))
