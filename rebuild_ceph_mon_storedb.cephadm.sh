#!/usr/bin/bash



log() {
  echo $(date +%F\ %T) $(hostname -s) "$1"
}

checkReturn() {
    if [ $1 -ne 0 ]; then
        log "ERROR: ${2} failed: returned ${1}"
        if [ ! -z "$3" ]; then
            exit $1
        fi
    fi
}

log "INFO: Gathering fsid"
fsid=$(awk '/fsid *= */ {print $NF}' /etc/ceph/ceph.conf)
checkReturn $? "Gather FSID" 1

log "INFO: Gathering OSD list"
# CSV: host,osd[,osd]...
# if all mons are down, ceph orch is likely inaccessible
#osd_list=$(ceph orch ps | awk '/^osd\.[0-9][0-9]* / { gsub(/[^0-9]/,"",$1); osdlist[$2]=osdlist[$2]","$1 } END { hcount=asorti(osdlist,sorted); for(i=1;i<=hcount;i++) { print sorted[i] osdlist[sorted[i]]; }}')
# so sometimes we need to build this differently

# Please edit this script and set this to a space deliminated list of your osd hosts
osd_hosts=""

if [ "$osd_hosts" == "" ]; then
    log "Error: Please edit this script and configure the osd_hosts option with the list of all OSD hosts in your cluster."
    exit;
fi

osd_list=""
for h in $osd_hosts; do
        host_output=$(ssh -T $h <<EOF
lvs --noheadings -a -o lv_tags | tr "," "\n" | awk -v hostname=\$(hostname) -F= 'BEGIN { printf("%s",hostname); } /^ceph\.osd_id=/ {printf(",%d", \$2)} END { print "" }'
EOF
)
	if [ "$osd_list" == "" ]; then
		osd_list="$host_output"
	else
		osd_list+=" $host_output"
	fi
done

checkReturn $? "OSD list" 1
log "INFO: Constructed the OSD list : $osd_list"
log "CONFIRM: Please confirm this is formatted correctly to continue (hostname1,1,2,3 hostname2,4,5,6 ...). Press any key to continue, CTRL-C to quit"
read ans


dirbase=/tmp/monrecovery.$(date +%F_%H-%M-%S)
log "INFO: Setting up directory structure in ${dirbase}"
for mydir in ms db db_slow logs; do
    mkdir -p "${dirbase}/${mydir}" &> /dev/null
done

log "INFO: Creating osd_mon-store.db_rebuild.sh script"
# osd_mon-store.db_rebuild.sh runs within the podman recovery container. log all of its output to a logfile
cat <<EOF > ${dirbase}/osd_mon-store.db_rebuild.sh
#!/bin/bash
recopath=/var/log/ceph/monrecovery
logfile=\${recopath}/logs/\$(ls /var/lib/ceph/osd)_recover.log

# log all output from this to
log() {
  echo \$(date +%F\ %T) \${HOSTNAME} "\$1" >> \$logfile
}
checkReturn() {
    if [ \$1 -ne 0 ]; then
        log "ERROR: \${2} failed: returned \${1}"
    fi
}
log "INFO: Sleep 5 seconds for filesystem stabilization"
sleep 5
log "INFO: Moving db and db_slow to ~/"
mv \${recopath}/{db,db_slow} ~/
for datadir in /var/lib/ceph/osd/ceph-*; do
    log "INFO: Running update-mon-db on \${datadir}"
    cd ~/
    CEPH_ARGS="--no_mon_config" ceph-objectstore-tool --data-path \${datadir} --type bluestore --op  update-mon-db --mon-store-path \${recopath}/ms &> \${recopath}/logs/osd.\$(basename \$datadir)_cot.log
    checkReturn \$? "COT update-mon-db"
    if [ -e \${datadir}/keyring ]; then
        cat \${datadir}/keyring >> \${recopath}/ms/keyring
        echo '    caps mgr = "allow profile osd"' >> \${recopath}/ms/keyring
        echo '    caps mon = "allow profile osd"' >> \${recopath}/ms/keyring
        echo '    caps osd = "allow *"' >> \${recopath}/ms/keyring
        echo >> \${recopath}/ms/keyring
    else
        log "WARNING: \${datadir} does not have a local keyring."
    fi
done
log "INFO: Moving db and db_slow from ~/"
mv ~/{db,db_slow} /var/log/ceph/monrecovery/
EOF
chmod 755 ${dirbase}/osd_mon-store.db_rebuild.sh

pullData() {
    log "INFO: Pulling ${1}:/var/log/ceph/${fsid}/monrecovery/"
    rsync -aqz --delete --remove-source-files ${1}:/var/log/ceph/${fsid}/monrecovery/ ${dirbase}/
    checkReturn $? "Pulling ${1}:/var/lib/ceph/${fsid}/monrecovery" 1
}

pushData() {
    log "INFO: Pushing ${1}:/var/log/ceph/${fsid}/monrecovery/"
    rsync -aqz --delete --remove-source-files ${dirbase}/ ${1}:/var/log/ceph/${fsid}/monrecovery/
    checkReturn $? "Pushing ${1}:/var/lib/ceph/${fsid}/monrecovery" 1
}


for hostosd in $osd_list; do
    osdhost=$(echo $hostosd | sed -e 's/,.*$//')
    osdids=$(echo $hostosd | sed -e 's/^[^,]*,//' -e 's/,/ /g')

    # skipping maintenance mode. Do we want to set noout or any flags while we bounce the osds?
    # log "INFO: Putting host ${osdhost} into maintenance mode"
    # ceph orch host maintenance enter ${osdhost} --force 2>&1 >> ${dirbase}/logs/${osdhost}_mgmt.log
    #
    # Since ceph orch not available without the  monitors ... stop/start the osds below in the ssh loop
    # we also do podman run instead of ceph orch
    #
    pushData $osdhost

    log "INFO: Starting osd_mon-store.db_rebuild.sh loop on ${osdhost} for OSDs $osdids"

    # make a script locally, scp it and run it remotely. unescaped variables will expand, escaped will be variables on the remote host
    cat <<EOF > ${dirbase}/recover_${osdhost}.sh
#!/bin/bash
log() {
  echo \$(date +%F\ %T) \${HOSTNAME} "\$1"
}
checkReturn() {
    if [ \$1 -ne 0 ]; then
        log "ERROR: \${2} failed: returned \${1}"
    fi
}

for osdid in ${osdids}; do
    # barebones container with the recovery script as the entry point  pointing to all of the specifics for the osd
    shell_cmd=\$(/bin/podman run -i --rm --ipc=host --stop-signal=SIGTERM --authfile=/etc/ceph/podman-auth.json --net=host --entrypoint /var/log/ceph/monrecovery/osd_mon-store.db_rebuild.sh --privileged  -v /var/run/ceph/${fsid}:/var/run/ceph:z -v /var/log/ceph/${fsid}:/var/log/ceph:z -v /var/lib/ceph/${fsid}/osd.\${osdid}:/var/lib/ceph/osd/ceph-\${osdid}:z -v /var/lib/ceph/${fsid}/osd.\${osdid}/config:/etc/ceph/ceph.conf:z -v /dev:/dev -v /run/udev:/run/udev -v /sys:/sys -v /run/lvm:/run/lvm -v /run/lock/lvm:/run/lock/lvm -v /var/lib/ceph/${fsid}/selinux:/sys/fs/selinux:ro -v /:/rootfs registry.redhat.io/rhceph/rhceph-5-rhel8@sha256:3075e8708792ebd527ca14849b6af4a11256a3f881ab09b837d7af0f8b2102ea )

    systemctl stop ceph-${fsid}@osd.\${osdid}.service
    # use the podman straight from the unit.run file
    exec \$shell_cmd

    systemctl start ceph-${fsid}@osd.\${osdid}.service
done
EOF
    chmod +x ${dirbase}/recover_${osdhost}.sh

    scp -q ${dirbase}/recover_${osdhost}.sh $osdhost:/tmp/
    ssh -T ${osdhost} /tmp/recover_${osdhost}.sh
    sleep 30
    ssh -T ${osdhost} rm -rf /tmp/recover_${osdhost}.sh
    pullData ${osdhost}

    # again, no maintenance mode used, unset any flags
    # log "INFO: Removing host ${osdhost} from maintenance mode"
    # ceph orch host maintenance exit ${osdhost} 2>&1 >> ${dirbase}/logs/${osdhost}_mgmt.log
done
log "INFO: Done. ... document further steps. https://docs.ceph.com/en/quincy/rados/troubleshooting/troubleshooting-mon/#mon-store-recovery-using-osds"
log "INFO: ceph-monstore-tool ${dirbase} rebuild -- --keyring /path/to/admin.keyring --mon-ids alpha beta gamma"
log "INFO: Need to specify mon-ids in numerical IP address order"
