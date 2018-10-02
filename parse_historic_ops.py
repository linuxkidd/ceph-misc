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
        if(event["event"] not in timespent):
            timespent[event["event"]]={ 'delta':(gmtime-lastepoch), 'lastepoch': gmtime, 'lastdt':event["time"] }
        else:
            timespent[event["event"]]['delta']+=gmtime-timespent[event["event"]]["lastepoch"]
            timespent[event["event"]]["lastepoch"]=gmtime
            timespent[event["event"]]["lastdt"]=event["time"]
        lastepoch=gmtime
    print("{0} {1}".format(firstdt,op["description"]))
    for event, edata in reversed(sorted(timespent.iteritems(), key=lambda (k,v): (v["delta"],k))):
        print("\t{0:8.4f} {1:s} {2:s}".format(edata["delta"],edata["lastdt"],event))
