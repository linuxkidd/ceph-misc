#!/usr/bin/env python

import re,sys
from datetime import datetime

# RegEx GREP format:
#   pgs:[^;]* [0-9]* [^(active)]
# 2023-07-31T03:41:49.223832+0000 mgr.pluto001.jhzenz (mgr.15501) 516804 : cluster [DBG] pgmap v514156: 353 pgs: 14 peering, 3 stale+active+clean, 1 active+undersized+degraded+remapped+backfilling, 53 active+undersized+degraded, 47 active+undersized, 9 remapped+peering, 9 active+undersized+degraded+remapped+backfill_wait+backfill_toofull, 57 active+undersized+degraded+remapped+backfill_wait, 1 stale+active+undersized+degraded, 22 active+remapped+backfill_wait, 137 active+clean; 985 GiB data, 2.2 TiB used, 2.8 TiB / 5 TiB avail; 19 KiB/s wr, 36 op/s; 7307561/51464928 objects degraded (14.199%); 19787938/51464928 objects misplaced (38.449%); 1.6 MiB/s, 4 objects/s recovering
#
# NOTE: Python REGEX sets [] -- remove all special functions of characters inside. Thus, must use negative lookahead pattern.
#  [^(active)]   becomes   (?!active)
#


dt_format = "%Y-%m-%dT%H:%M:%S.%f"
inactive = '^.*pgs:[^;]*?\s[0-9]+\s(?!active).*$'

prevstate = 1
stateChange = 0
states = [ "Inactive", "Active" ];

for line in sys.stdin:
    if re.match(inactive,line):
        if prevstate:
            lparts = line.split(' ');
            eventTime = float(datetime.strptime(lparts[0].split("+")[0], dt_format).strftime("%s"))
            if stateChange:
                dur = round(eventTime - stateChange,2);
                print(f"{lparts[0]}: {states[prevstate]} for {dur} seconds.")
            stateChange = eventTime;
            prevstate = 0
    else:
        if not prevstate:
            lparts = line.split(' ');
            eventTime = float(datetime.strptime(lparts[0].split("+")[0], dt_format).strftime("%s"))
            if stateChange:
                dur = round(eventTime - stateChange,2);
                print(f"{lparts[0]}: {states[prevstate]} for {dur} seconds.")
            stateChange = eventTime;
            prevstate = 1

