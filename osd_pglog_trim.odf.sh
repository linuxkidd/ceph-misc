#!/usr/bin/bash
usage() {
  cat <<EOF >&2

Usage: $0 -o <osdid> [-f <0|1>] [-i <imageurl>] [-m <maxPGLog>] [-p <pgid>] [-d <0|1>]

Where:
    -o <osdid>    is the numeric ID of the OSD to run against.
                  ( required )

    -f <0|1>      Set to 1 to enable setting / unsetting 'noout' flag.
                  ( optional, default 0 - NOT set/unset 'nooup' flag )

    -i <imageurl> is the image address to run the trim shell with.
                  ( optional, default is system default image )

    -m <maxPGlog> is the value for osd_pg_log_trim_max
                  ( optional, default 500000 )

    -n <0|1>      No-trim, only dumps the PGlog, then exits
                  ( optional, default is 0 - perfom the trim )

    -p <pgid>     is the Placement Group to Trim
                  ( optional, default is trim all PGs on the OSD )

    -d <0|1>      Set to 1 to enable post-trim PGlog dump
	                ( optional, default is 0 - not generate post-trim PGlog dump )

    NOTES:
     - The specified OSD will be stopped for some period of time, then restarted.
     - If '-f 1' is NOT specified, recommend setting 'noout' flag before, then unsetting after.
       #-- Before --
       $ oc scale deployment {rook-ceph,ocs}-operator --replicas=0 -n openshift-storage
       $ oc rsh -n openshift-storage $(oc get po -l app=rook-ceph-tools -oname) ceph osd set noout
       #-- After --
       $ oc rsh -n openshift-storage $(oc get po -l app=rook-ceph-tools -oname) ceph osd unset noout
       $ oc scale deployment {rook-ceph,ocs}-operator --replicas=1 -n openshift-storage

EOF
  exit 1
}

log() {
  echo $(date +%F\ %T) $(hostname -s) "$1"
}

restoreOSD() {
  if [ -e ${1}.yaml ]; then
    #oc replace --force -f ${1}.${starttime}.yaml
    RETVAL=$?
    if [ $RETVAL -ne 0 ]; then
      log "ERROR: Failed to restore deployment for osd.${1} - ret: $RETVAL"
      exit $RETVAL
    fi
  else 
    log "CRITICAL: Backup ${1}.yaml file not found.  OSD.${1} not restored!"
    exit 1
  fi
}

waitOSDPod() {
  mysleep=0
  isRunning=0

  if [ ! -z "$2" ]; then
    log "INFO: Waiting for old pod to terminate"
    while [ $(oc get pod -l osd=${1} -o name | grep -c ${2}) -gt 0 ] && [ $mysleep -lt 120 ]; do
      echo -n .
      ((mysleep++))
      sleep 1
    done
    echo
  fi

  log "INFO: Waiting up to 2 minutes for osd.${1} pod to be Running"
  while [ $mysleep -lt 120 ]; do
    isRunning=$(oc get pod -l osd=${1} -o json | jq '.items[0].status.containerStatuses | last | .state | select(.running != null)' | wc -l)
    if [ $isRunning -gt 0 ]; then
      break
    fi
    echo -n .
    ((mysleep++))
    sleep 1
  done
  echo

  if [ $isRunning -eq 0 ]; then
    log "ERROR: Patched container failed to enter Running state."
    restoreOSD $osdid
    exit 1
  fi
  log "INFO: Sleeping 5 seconds for container init to complete."
  mysleep=0
  while [ $mysleep -lt 5 ]; do
    echo -n .
    ((mysleep++))
    sleep 1
  done
  echo
}

if [ $# -lt 1 ]; then
  echo
  echo "ERROR: Required parameters missing."
  usage
fi

## Defaults
osdid=""
cephadmopts=""
imagerepo=""
maxtrim=500000
allpgs=1
pgid=""
error=0
posttrimdump=0
manageflags=0

while getopts ":o:i:m:n:p:d:f:" o; do
  case "${o}" in
    d)
      if [ $(echo ${OPTARG} | egrep -c "^[0-1]$") -eq 1 ]; then
        posttrimdump=${OPTARG}
      else
        echo
        echo "ERROR: -d paramter must be numeric only"
	      error=1
      fi
      ;;
    f)
      if [ $(echo ${OPTARG} | egrep -c "^[0-1]$") -eq 1 ]; then
        manageflags=${OPTARG}
      else
        echo
        echo "ERROR: -f paramter must be numeric only"
	      error=1
      fi
      ;;
    i)
      imagerepo="${OPTARG}"
      ;;
    m)
      if [ $(echo ${OPTARG} | egrep -c "^[0-9][0-9]*$") -eq 1 ]; then
        maxtrim=${OPTARG}
      else
        echo
        echo "ERROR: -m paramter must be numeric only"
	      error=1
      fi
      ;;
    n)
      if [ $(echo ${OPTARG} | egrep -c "^[0-1]$") -eq 1 ]; then
        notrim=${OPTARG}
      else
        echo
        echo "ERROR: -n paramter must be numeric only"
	      error=1
      fi
      ;;
    o)
      if [ $(echo ${OPTARG} | egrep -c '^[0-9][0-9]*$') -eq 1 ]; then
        osdid=${OPTARG}
      else
        echo
        echo "ERROR: -o parameter must be numeric ID only."
	      error=1
      fi
      ;;
    p)
      if [ $(echo ${OPTARG} | egrep -c "^[0-9][0-9]*\.[0-9a-f][0-9a-f]*$" ) -eq 1 ]; then
        allpgs=0
        pgid="${OPTARG}"
      else
        echo
        echo "ERROR: -p paramter must be a valid Placement Group ID format (Example: 1.a7)"
	      error=1
      fi
      ;;
    *)
      echo
      echo "Unrecognized argument: ${o}"
      usage
      ;;
  esac
done
shift $((OPTIND-1))

if [ $error -gt 0 ]; then
  usage
fi

if [ -z "${osdid}" ]; then
  echo
  echo "ERROR: -o osdid required!"
  usage
fi

starttime=$(date +%F_%H-%M-%S)

log "PARAM: Paramters:"
log "PARAM:   osdid=${osdid}"
log "PARAM:   cephadmopts=${cephadmopts}"
log "PARAM:   maxtrim=${maxtrim}"
log "PARAM:   allpgs=${allpgs}"
log "PARAM:   pgid=${pgid}"
log "PARAM:   posttrimdump=${posttrimdump}"
log "PARAM:   notrim=${notrim}"
log "PARAM:   manageflags=${manageflags}"
log "PARAM:   imagerepo=${imagerepo}"

if [ $manageflags -eq 1 ]; then
  log "INFO: scaling down rook-ceph and ocs operators"
  oc scale deployment {rook-ceph,ocs}-operator --replicas=0 -n openshift-storage
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to scale down - ret: $RETVAL"
    exit $RETVAL
  fi

  log "INFO: setting noout flag"
  oc rsh -n openshift-storage $(oc get po -l app=rook-ceph-tools -oname) ceph osd set noout &> /dev/null
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to set noout flag - ret: $RETVAL"
    exit $RETVAL
  fi
fi

log "INFO: Backing up osd deployment yaml"
oc get deployment rook-ceph-osd-${osdid} -o yaml > ${osdid}.${starttime}.yaml
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to dump osd deployment yaml - ret: $RETVAL"
  exit $RETVAL
fi

log "INFO: Removing liveness and startup Probes for osd.${osdid} pod"
osdpod=$(oc get pod -l osd=${osdid} -o name)
resp=$(oc set probe deployment rook-ceph-osd-${osdid} --remove --liveness --startup)
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to remove Probes osd.${osdid} - ret: $RETVAL"
  exit $RETVAL
fi
if [ $(echo $resp | grep -c "updated") -gt 0 ]; then
  waitOSDPod ${osdid} ${osdpod}
fi

if [ ! -z "$imagerepo" ]; then
  osdpod=$(oc get pod -l osd=${osdid} -o name)
  log "INFO: Setting pod imge for osd.${osdid}"
  resp=$(oc set image deployment rook-ceph-osd-${osdid} osd=${imagerepo})
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to set image osd.${osdid} - ret: $RETVAL"
    exit $RETVAL
  fi
  if [ $(echo $resp | grep -c "updated") -gt 0 ]; then
    waitOSDPod ${osdid} ${osdpod}
  fi
fi

log "INFO: Setting CPU/Memory to 8/64"
osdpod=$(oc get pod -l osd=${osdid} -o name)
resp=$(oc set resources deployment rook-ceph-osd-${osdid} --limits=cpu=8,memory=64Gi --requests=cpu=8,memory=64Gi)
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to set CPU/Memory osd.${osdid} - ret: $RETVAL"
  exit $RETVAL
fi
if [ $(echo $resp | grep -c "updated") -gt 0 ]; then
  waitOSDPod ${osdid} ${osdpod}
fi


log "INFO: Sleeping osd.${osdid} pod"
osdpod=$(oc get pod -l osd=${osdid} -o name)
resp=$(oc patch deployment rook-ceph-osd-${osdid} -n openshift-storage -p '{"spec": {"template": {"spec": {"containers": [{"name": "osd", "command": ["sleep"], "args": ["infinity"]}]}}}}')
RETVAL=$?

if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to sleep osd.${osdid} - ret: $RETVAL"
  exit $RETVAL
fi
if [ $(echo $resp | grep -c "no change") -eq 0 ]; then
  waitOSDPod ${osdid} ${osdpod}
fi

if [ $allpgs -eq 1 ]; then
  log "INFO: Operating on all PGs for osd.${osdid}"
  pglist="\$(ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op list-pgs)"
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to dump list-pgs from osd.${osdid} - ret: $RETVAL"
    restoreOSD $osdid
    exit $RETVAL
  fi
else
  log "INFO: Operating only on PG: ${pgid}"
  pglist=${pgid}
fi

log "INFO: Generating PG log script for osd.${osdid}"
pretrimline=""
#trimline="CEPH_ARGS='--no_mon_config --rocksdb_cache_size=10737418240 --bluestore_rocksdb_options=\"compression=kNoCompression,max_write_buffer_number=4,min_write_buffer_number_to_merge=1,recycle_log_file_num=4,write_buffer_size=268435456,writable_file_max_buffer_size=0,compaction_readahead_size=2097152,max_background_compactions=2,max_total_wal_size=1073741824,compact_on_mount=false\"  --osd_pg_log_dups_tracked=1 --osd_pg_log_trim_max=${maxtrim}' ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op trim-pg-log-dups --pgid \$pgid &> /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_trim-pg-log.log"
trimline="CEPH_ARGS='--no_mon_config --rocksdb_cache_size=10737418240 --osd_pg_log_dups_tracked=1 --osd_pg_log_trim_max=${maxtrim}' ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op trim-pg-log-dups --pgid \$pgid &> /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_trim-pg-log.log"
posttrimline=""
if [ $notrim ]; then
  log "INFO: Setting up PGLog dump only."
  pretrimline="CEPH_ARGS='--no_mon_config --osd_pg_log_dups_tracked=999999999999' ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op log --pgid \$pgid > /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_pre-trim-dump_pg-log.json"
  trimline=""
  posttrimline=""
elif [ $posttrimdump -eq 1 ]; then
  log "INFO: Including post-trim PGLog dump in script."
  pretrimline="CEPH_ARGS='--no_mon_config --osd_pg_log_dups_tracked=999999999999' ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op log --pgid \$pgid > /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_pre-trim-dump_pg-log.json"
  posttrimline="CEPH_ARGS='--no_mon_config --osd_pg_log_dups_tracked=999999999999' ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op log --pgid \$pgid > /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_post-trim-dump_pg-log.json"
fi

osdpod=$(oc get pod -l osd=${osdid} -o name)
log "INFO: Executing PG log script for osd.${osdid} via pod ${osdpod}"
oc rsh ${osdpod} << EOF
echo \$(date %F\ %T) ${osdpod} INFO: Dumping PG list
mypglist=${pglist}
total=\$(echo \$mypglist | wc -w)
count=0
mkdir -p /var/log/ceph/osd.${osdid} &> /dev/null
for pgid in \${mypglist}; do
  ((count++))
  echo \$(date +%F\ %T) ${osdpod} INFO: Processing pg log for \$pgid :: Progress \$count / \$total
  ${pretrimline}
  ${trimline}
  ${posttrimline}
done
EOF
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to run pglog trim script for osd.${osdid} - ret: $RETVAL"
  restoreOSD $osdid
  exit $RETVAL
fi

osdpod=$(echo $osdpod | sed -e 's/^[^\/]*\///')
log "INFO: Copying output for osd.${osdid} locally"
mkdir -p ./osd.${osdid} &> /dev/null
oc cp ${osdpod}:/var/log/ceph/osd.${osdid}/ ./osd.${osdid}/
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to copy data for osd.${osdid} - ret: $RETVAL"
  restoreOSD $osdid
  exit $RETVAL
fi


log "INFO: Reverting deployment for osd.${osdid}"
restoreOSD $osdid

if [ $manageflags -eq 1 ]; then
  log "INFO: scaling up rook-ceph and ocs operators"
  oc scale deployment {rook-ceph,ocs}-operator --replicas=1 -n openshift-storage
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to scale up - ret: $RETVAL"
    exit $RETVAL
  fi

  log "INFO: unsetting noout flag"
  oc rsh -n openshift-storage $(oc get po -l app=rook-ceph-tools -oname) ceph osd unset noout &> /dev/null
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    log "ERROR: Failed to unset noout flag - ret: $RETVAL"
    exit $RETVAL
  fi
fi

log "INFO: creating tar.gz of osd.${osdid} output"
outfile=./osd.${osdid}_pglog_trim_dump.tar.gz
tar -C ./osd.${osdid} -czf $outfile ./
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  log "ERROR: Failed to create tar.gz of osd.${osdid} output - ret: $RETVAL"
  exit $RETVAL
fi

log "Processing of osd.${osdid} complete."
echo
echo
echo
echo Please attach $outfile to your support ticket
echo
echo
echo