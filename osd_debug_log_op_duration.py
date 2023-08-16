#!/usr/bin/env python3

import re,sys
from datetime import datetime

dt_format="%Y-%m-%dT%H:%M:%S.%f"
op_track={}
threadidx = 3

for line in sys.stdin:
    line_parts = line.split(" ")
    dt = datetime.strptime(line_parts[0][:26], dt_format)
    epoch = (dt - datetime(1970,1,1)).total_seconds()
    epoch += float(line_parts[0][26:29])*0.000001
    try:
        opidx = line_parts.index('dequeue_op')+1
    except:
        opidx = 0
    if re.match('^.* dequeue_op .* latency .*$',line):
        latidx = line_parts.index('latency')+1
        op_track[line_parts[threadidx]] = { "op": line_parts[opidx], "start": line_parts[0], "start_epoch": epoch, "dequeue_latency": float(line_parts[latidx])}
    elif re.match('^.* dequeue_op .* finish$',line):
        start = op_track.get(line_parts[threadidx])
        if start and start["op"] == line_parts[opidx]:
            op_track[line_parts[threadidx]]["end"] = line_parts[0]
            op_track[line_parts[threadidx]]["end_epoch"] = epoch
            op_track[line_parts[threadidx]]["duration"] = abs(epoch - op_track[line_parts[threadidx]]["start_epoch"]) + op_track[line_parts[threadidx]]['dequeue_latency']
            try:
                print(f"{op_track[line_parts[threadidx]]['duration']} - {op_track[line_parts[threadidx]]['op']} {op_track[line_parts[threadidx]]['reported_latency']}")
                del op_track[line_parts[threadidx]]
            except:
                pass
    elif re.match('^.* log_op_stats .*$',line):
        if op_track.get(line_parts[threadidx]):
            op_track[line_parts[threadidx]]['reported_latency'] = float(line_parts[-1])
            try:
                print(f"{op_track[line_parts[threadidx]]['duration']} - {op_track[line_parts[threadidx]]['op']} {op_track[line_parts[threadidx]]['reported_latency']}")
                del op_track[line_parts[threadidx]]
            except:
                pass
