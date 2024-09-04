#!/bin/bash
#
# Manual deep-scrub management script for Ceph 18.x
#
# by: Michael J. Kidd <linuxkidd@gmail.com>
# version: 1.0
# https://github.com/linuxkidd/ceph-misc/blob/main/deep-scrub.sh
#
#
# Usage:
#    ./deep-scrub.sh <concurrency> <osd_scrub_sleep> <scrub_chunk>
# 
# Where:
#     <concurrency>:      Is the number of concurrent deep-scrubs to schedule
#                         (Required)
# 
#     <osd_scrub_sleep>:  Fractional seconds to set for sleep between scrub 
#                         chunk reads during deep-scrub
#                         (Optional, default: no change)
# 
#     <scrub_chunk>:      How many objects to read at a time for deep-scrub
#                         (Optional, default: no change)
#
# NOTE:
#      Automatic deep-scrub scheduling should be disabled in the cluster when
#      using this tool for deep-scrub management.
#      # ceph osd set nodeep-scrub
#

usage() {
    echo
    echo Usage:
    echo "    $0 <concurrency> <osd_scrub_sleep> <scrub_chunk>"
    echo
    echo Where:
    echo "    <concurrency>:      Is the number of concurrent deep-scrubs to schedule"
    echo "                        (Required)"
    echo
    echo "    <osd_scrub_sleep>:  Fractional seconds to set for sleep between scrub "
    echo "                        chunk reads during deep-scrub"
    echo "                        (Optional, default: no change)"
    echo
    echo "    <scrub_chunk>:      How many objects to read at a time for deep-scrub"
    echo "                        (Optional, default: no change)"
    echo
    echo NOTE:
    echo "    Automatic deep-scrub scheduling should be disabled in the cluster when"
    echo "    using this tool for deep-scrub management."
    echo "    # ceph osd set nodeep-scrub"
    echo
    echo
    exit 1
}

logline() {
  echo $(date +%F\ %T) $1
}

if [ $# -eq 3 ]; then
  if [ $(echo $3 | grep -c '^[0-9][0-9]*$') -eq 1 ]; then
    CHUNK=$3
    logline "Setting osd_scrub_chunk_min and max to $CHUNK"
    ceph config set osd osd_scrub_chunk_max $CHUNK
    ceph config set osd osd_scrub_chunk_min $CHUNK
  else
    echo ERROR: 3rd parameter \(osd_scrub_chunk_max\) must be a positive whole number.
    usage
  fi
fi

if [ $# -ge 2 ]; then
  if [ $(echo $2 | grep -c '^[0-9\.][0-9\.]*$') -eq 1 ]; then
    SLEEP=$2
    logline "Setting osd_scrub_sleep to $SLEEP"
    ceph config set osd osd_scrub_sleep $SLEEP
  else
    echo ERROR: 2nd parameter \(osd_scrub_sleep\) must be a positive decimal number.
    usage
  fi
fi

if [ $# -ge 1 ]; then
  if [ $(echo $1 | grep -c '^[0-9][0-9]*$') -eq 1 ]; then
    CONCUR=$1
  else
    echo ERROR: 1st parameter \(concurrency\) must be a positive whole number.
    usage
  fi
else
  usage
fi

queuedscrub=$(ceph pg dump 2> /dev/null | grep -c 'queued for deep scrub')
activescrub=$(ceph pg dump 2> /dev/null | grep -c 'scrubbing+deep')
takenslots=$((queuedscrub + activescrub))
openslots=$((CONCUR - takenslots))


logline "$activescrub actively deep-scrubbing."
logline "$queuedscrub queued for deep-scrub."
logline "$openslots open scrub slots."

if [ $openslots -gt 0 ]; then
  oldestnotscrub=$(ceph pg dump 2> /dev/null | grep -v scrubbing+deep | grep -v 'queued for deep scrub' | awk '$1 ~ /[0-9a-f]+\.[0-9a-f]+/ {print $22, $1}' | sort | head -n $openslots | awk '{print $2}')
  for i in $oldestnotscrub; do
    logline "$(ceph pg deep-scrub $i 2>&1)"
  done
fi
