#!/usr/bin/bash

if [ $# -lt 1 ]; then
  echo Usage: $0 osdid
  echo
  echo "  Where osdid is the numeric ID of the OSD to run against."
  echo "  NOTE: This OSD will be stopped for some period of time, then restarted."
  exit 1
fi

osdid=$1
echo $(date +%F\ %T) $(hostname -s) INFO: Gathering fsid

fsid=$(awk '/fsid\ *=\ */ {print $NF}' /etc/ceph/ceph.conf)
mkdir /var/log/ceph/${fsid}/osd.${osdid} &>/dev/null

echo $(date +%F\ %T) $(hostname -s) INFO: setting noout flag
cephadm shell --fsid ${fsid} ceph osd set noout &>/dev/null
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to set noout flag - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: stopping osd.${osdid}
systemctl disable --now ceph-${fsid}@osd.${osdid}
cephadm unit --fsid ${fsid} --name osd.${osdid} stop
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to stop osd.${osdid} - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: Listing all PGs for osd.${osdid}
cephadm shell --fsid ${fsid} --name osd.${osdid}  ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op list-pgs > /var/log/ceph/${fsid}/osd.${osdid}/osd.${osdid}_list-pgs.txt #2>/dev/null
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to dump list-pgs from osd.${osdid} - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: Generating PG log script for osd.${osdid}
cat << EOF > /var/log/ceph/${fsid}/osd.${osdid}/dump_pglog_osd.${osdid}.sh
#!/usr/bin/bash

for pgid in \$(cat /var/log/ceph/osd.${osdid}/osd.${osdid}_list-pgs.txt); do
  echo \$(date +%F\ %T) $(hostname -s) INFO: Dumping pg log for \$pgid
  ceph-objectstore-tool --data-path /var/lib/ceph/osd/ceph-${osdid} --op log --pgid \$pgid > /var/log/ceph/osd.${osdid}/osd.${osdid}_pgid_\${pgid}_log.json
done
EOF
chmod 755 /var/log/ceph/${fsid}/osd.${osdid}/dump_pglog_osd.${osdid}.sh

echo $(date +%F\ %T) ${HOSTNAME} INFO: Running PG log script for osd.${osdid}
cephadm shell --fsid ${fsid} --name osd.${osdid} /var/log/ceph/osd.${osdid}/dump_pglog_osd.${osdid}.sh
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to dump pglog for osd.${osdid} - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: starting osd.${osdid}
systemctl enable ceph-${fsid}@osd.${osdid}
cephadm unit --fsid ${fsid} --name osd.${osdid} start
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to start osd.${osdid} - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: unsetting noout flag
cephadm shell --fsid ${fsid} ceph osd unset noout &>/dev/null
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to unset noout flag - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) INFO: creating tar.gz of osd.${osdid} output
outfile=/var/log/ceph/${fsid}/osd.${osdid}_pglog_dump.tar.gz
tar -C /var/log/ceph/${fsid}/osd.${osdid} -czf $outfile ./
RETVAL=$?
if [ $RETVAL -ne 0 ]; then
  echo $(date +%F\ %T) $(hostname -s) ERROR: Failed to create tar.gz of osd.${osdid} output - ret: $RETVAL
  exit $RETVAL
fi

echo $(date +%F\ %T) $(hostname -s) Processing of osd.${osdid} complete.
echo
echo
echo
echo Please attach $outfile to your support ticket
