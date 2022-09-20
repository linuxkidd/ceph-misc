#!/usr/bin/env python

import sys, json, operator
from datetime import datetime


obj=json.load(sys.stdin)

for op in obj["ops"]:
    remaining_time=op["duration"]
    event=op["type_data"]["events"]
    for i in range(len(event)):
        utc_time = datetime.strptime(event[i]["time"], "%Y-%m-%d %H:%M:%S.%f")
        event[i]["gmtime"] = (utc_time - datetime(1970, 1, 1)).total_seconds()
        event[i]["delta"] = 0
    print("{0} {1}".format(op["initiated_at"],op["description"]))
    print("\tAge: {0} / Duration: {1}".format(op["age"],op["duration"]))
    events=sorted(op["type_data"]["events"], key=lambda d: d["gmtime"])
    for i in range(len(events)):
        if i > 0:
            event[i-1]["delta"]=event[i]["gmtime"]-event[i-1]["gmtime"]
            remaining_time-=event[i-1]["delta"]
        if i == len(op["type_data"]["events"])-1:
            event[i]["delta"]=remaining_time
        print("\t{0:10.6f} {1:s} {2:s}".format(events[i]['delta'],events[i]['time'],events[i]['event']))
