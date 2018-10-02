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
    subopstart=0
    firstdt=""
    for event in op["type_data"]["events"]:
        if(firstdt==""):
            firstdt=event["time"]
        utc_time = datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S.%f")
        gmtime = (utc_time - datetime(1970, 1, 1)).total_seconds()
        if(event["event"] not in timespent):
            if(lastepoch==0):
                lastepoch=gmtime
            timespent[event["event"]]={ 'delta':(gmtime-lastepoch), 'lastepoch': gmtime, 'lastdt':event["time"] }
            if(event["event"][:23]=="waiting for subops from"):
                subopstart=gmtime
            elif(event["event"][:22]=="sub_op_commit_rec from"):
                timespent[event["event"]]={ 'delta':(gmtime-subopstart), 'lastepoch': gmtime, 'lastdt':event["time"] }

        else:
            timespent[event["event"]]['delta']+=gmtime-timespent[event["event"]]["lastepoch"]
            timespent[event["event"]]["lastepoch"]=gmtime
            timespent[event["event"]]["lastdt"]=event["time"]
        lastepoch=gmtime
    print("{0} {1}".format(firstdt,op["description"]))
    for event, edata in sorted(timespent.iteritems(), key=lambda (k,v): (v["lastepoch"],k)):
        print("\t{0:8.4f} {1:s} {2:s}".format(edata["delta"],edata["lastdt"],event))
