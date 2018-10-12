#!/usr/bin/env python

import sys
import json

obj=json.load(sys.stdin)

for pg in obj['osdmap']['pg_upmap_items']:
    print pg['pgid']

