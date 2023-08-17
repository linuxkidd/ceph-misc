#!/usr/bin/env python3

"""
-- Example usage:
  $ egrep -ah '(dequeue_op .* (latency|finish)|log_op_stats)' ceph-osd.1.log* | ./osd_debug_log_op_duration.py > osd.1.duration.txt

-- Operation:
  1. Find the `dequeue_op .* latency` line, catalog the date/time of the log line, as well as the reported 'latency' value.
  2. Find the `dequeue_op .* finish` line, catalog the date/time of the log line.
  3. Find the `log_op_stats` line reported `lat` value.
  4. Determine op duration by subtracting date/time of log line 1 from log line 2, then add the 'dequeue_op' latency in log line 1
  5. Print the calculated duration, the op ID ( the value after 'dequeue_op` in the log lines ), and the reported `log_op_stats` lat value.

"""


import re,sys
from datetime import datetime

dt_format="%Y-%m-%dT%H:%M:%S.%f"
op_track={}
threadidx = 0

for line in sys.stdin:
    line_parts = line.split(" ")
    if not threadidx:
        for i in range(6):
            if re.match('^[0-9a-f]{12}$',line_parts[i]):
                threadidx=i
                break
    if not threadidx:
        print("Could not find thread ID field in log line.")
        exit(1)

    try:
        if re.match('^.*\+.*$',line_parts[0]):
            dt = datetime.strptime(line_parts[0].split("+")[0], dt_format)
            epoch = (dt - datetime(1970,1,1)).total_seconds()
        else:
            dt = datetime.strptime(line_parts[0][:26], dt_format)
            epoch = (dt - datetime(1970,1,1)).total_seconds()
            epoch += float(line_parts[0][26:29])*0.000001
    except:
        continue
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
            try: # Try to print, but if 'log_op_stats' line hasn't been seen, this will fail
                print(f"{op_track[line_parts[threadidx]]['duration']} - {op_track[line_parts[threadidx]]['op']} {op_track[line_parts[threadidx]]['reported_latency']}")
            except:
                pass
            else:
                del op_track[line_parts[threadidx]]
    elif re.match('^.* log_op_stats .*$',line):
        if op_track.get(line_parts[threadidx]):
            op_track[line_parts[threadidx]]['reported_latency'] = float(line_parts[-1])
            try: # Try to print, but if 'dequeue_op .* finish' line hasn't been seen, this will fail
                print(f"{op_track[line_parts[threadidx]]['duration']} - {op_track[line_parts[threadidx]]['op']} {op_track[line_parts[threadidx]]['reported_latency']}")
            except:
                pass
            else:
                del op_track[line_parts[threadidx]]

