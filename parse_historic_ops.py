#!/usr/bin/env python

import sys, json, operator
from datetime import datetime


obj = json.load(sys.stdin)
dt_format = "%Y-%m-%d %H:%M:%S.%f"

for op in obj["ops"]:
    remaining_time = op["duration"]
    event = op["type_data"]["events"]
    for i in range(len(event)):
        if "T" in event[i]["time"]:
            dt_format = "%Y-%m-%dT%H:%M:%S.%f"

        utc_time = datetime.strptime(event[i]["time"].split("+")[0], dt_format)
        event[i]["gmtime"] = (utc_time - datetime(1970, 1, 1)).total_seconds()
        event[i]["delta"] = 0
    print("{0} {1}".format(op["initiated_at"],op["description"]))
    print("\tAge: {0} / Duration: {1}".format(op["age"],op["duration"]))
    events=sorted(op["type_data"]["events"], key=lambda d: d["gmtime"])
    for i in range(len(events)):
        if i < len(events)-1:
            events[i]["delta"]=events[i+1]["gmtime"]-events[i]["gmtime"]
            if events[i]['event'] != "throttled":
                remaining_time-=events[i]["delta"]
        else:
            events[i]["delta"]=remaining_time
        print("\t{0:10.6f} {1:s} {2:s}".format(events[i]['delta'],events[i]['time'],events[i]['event']))
