#!/usr/bin/bash


# Please edit this script and set this to a space delimitated list of your osd hosts
osd_hosts=""

# ----------- Do not edit below this line -----------
display_usage() {

    echo "$0 usage : "
    echo "first parameter of -x will toggle on debugging"
    echo "Pass osd hosts as additional paramters"
    echo "You can also edit this script and set the osd_hosts variable"
    echo "Expects that the admin node you're running this script from has root level ssh key access to all osd nodes"
    exit

}

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

if [ $# -eq 0 ] && [ "$osd_hosts" == "" ]; then
    display_usage
    exit;
fi


# if first option is -x, we turn on set -x
if [ "$1" == "-x" ]; then
    set -x
    shift
fi

# or you can also pass a list of the osd nodes as parameters
if [ $# -gt 0 ] && [ "$osd_hosts" == "" ]; then
    for node in "$@"; do
        if [ "$osd_hosts" == "" ]; then
            osd_hosts="$node"
        else
            osd_hosts+=" $node"
        fi
    done
else
    if [ "$osd_hosts" == "" ]; then
        log "Error: Please edit this script and configure the osd_hosts option with the list of all OSD hosts in your cluster."
        exit;
    fi
fi
log "INFO: Gathering fsid"
fsid=$(awk '/fsid *= */ {print $NF}' /etc/ceph/ceph.conf)
checkReturn $? "Gather FSID" 1


# determine the container engine in-use. use rpm as there is a podman-docker container that supplies a podman 'docker' symlink
# defaults to checking for podman
container_engine=$(which podman)
if [ "$?" == "1" ]; then
	container_engine=$(which docker)
	if [ "$?" == "1" ]; then
		container_engine=$(which crio)
		checkReturn $? "Checking for container enginer" 1
	fi
fi
log "INFO: Using $container_engine for containers"

exit;

log "INFO: Gathering OSD list"
# CSV: host,osd[,osd]...
# if all mons are down, ceph orch is likely inaccessible
#osd_list=$(ceph orch ps | awk '/^osd\.[0-9][0-9]* / { gsub(/[^0-9]/,"",$1); osdlist[$2]=osdlist[$2]","$1 } END { hcount=asorti(osdlist,sorted); for(i=1;i<=hcount;i++) { print sorted[i] osdlist[sorted[i]]; }}')
# so sometimes we need to build this differently

osd_list=""
for h in $osd_hosts; do
    remote_output=$(ssh -T $h lvs --noheadings -a -o lv_tags)
    host_string="${h},"
    for lvs in $remote_output; do
        host_string+=$(echo $lvs|sed 's/.*,ceph\.osd_id=\([[:digit:]]*\),.*/\1/'|tr "\n" ",")
    done
    osd_list+=" $(echo ${host_string}|sed 's/,$//')"
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
logfile="\${recopath}/logs/\$(ls /var/lib/ceph/osd)_recover.log"
echo > \$logfile

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
    log "INFO: Checking for locks on \${datadir} before trying ceph-objectstore-tool"
    lslocks |grep \${datadir} >> \${logfile}
    count=0
    while  [ "\$?" == "0" ]; do
        log "DEBUG: still locked, sleeping for 5 seconds"
        sleep 5
        ((count++))
        if [ "\${count}" == "10" ]; then
            log "ERROR: osd is still locked after 10 attempts, failing"
            exit 1
        fi
        lslocks |grep \${datadir} >> \${logfile}
    done

    log "INFO: Running update-mon-db on \${datadir}"
    CEPH_ARGS="--no_mon_config" ceph-objectstore-tool --debug --data-path \${datadir} --type bluestore --op  update-mon-db --mon-store-path \${recopath}/ms &> \${recopath}/logs/osd.\$(basename \$datadir)_cot.log
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
log "INFO: monrecovery directory listing \n \$(ls -laR /var/log/ceph/monrecovery/)"
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
logfile="/var/log/ceph/${fsid}/monrecovery/logs/${osdhost}.log"
log() {
  echo \$(date +%F\ %T) \${HOSTNAME} "\$1" >> \${logfile}
}
checkReturn() {
    if [ \$1 -ne 0 ]; then
        log "ERROR: \${2} failed: returned \${1}"
    fi
}

for osdid in ${osdids}; do
    # barebones container with the recovery script as the entry point  pointing to all of the specifics for the osd
    shell_cmd="/bin/podman run -i --rm --ipc=host --stop-signal=SIGTERM --authfile=/etc/ceph/podman-auth.json --net=host --entrypoint /var/log/ceph/monrecovery/osd_mon-store.db_rebuild.sh --privileged  -v /var/run/ceph/${fsid}:/var/run/ceph:z -v /var/log/ceph/${fsid}:/var/log/ceph:z -v /var/lib/ceph/${fsid}/osd.\${osdid}:/var/lib/ceph/osd/ceph-\${osdid}:z -v /var/lib/ceph/${fsid}/osd.\${osdid}/config:/etc/ceph/ceph.conf:z -v /dev:/dev -v /run/udev:/run/udev -v /sys:/sys -v /run/lvm:/run/lvm -v /run/lock/lvm:/run/lock/lvm -v /var/lib/ceph/${fsid}/selinux:/sys/fs/selinux:ro -v /:/rootfs registry.redhat.io/rhceph/rhceph-5-rhel8@sha256:3075e8708792ebd527ca14849b6af4a11256a3f881ab09b837d7af0f8b2102ea"

    systemctl stop ceph-${fsid}@osd.\${osdid}.service >> \$logfile
    checkReturn $? "Stopping osd \${osdid}" 1
    # after stopping this osd, we loop up to 10 times waiting for the lock on the osd fsid to disappear
    # otherwise the  entrypoint script will fail because the osd still has a lock on the device
    sleep 10
    count=0
    lslocks |grep /var/lib/ceph/osd/ceph-\${osdid}/fsid > /dev/null
    while [ "\$?" == "0" ]; do
        sleep 10
        ((count++))
        if [ \$count -gt 10 ]; then
            log "ERROR: We've looped 10 times waiting for \${osdid} to stop."
            exit
        fi
        lslocks |grep /var/lib/ceph/osd/ceph-\${osdid}/fsid > /dev/null
    done
    # run the container with the osd_mon-store.db_rebuild.sh entry point
    exec \$shell_cmd

    systemctl start ceph-${fsid}@osd.\${osdid}.service >> \$logfile
    sleep 10
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
    # when done with the current osd host, sleep for another 60 seconds and then prompt for the next node
    #
    sleep 60
    log "CONFIRM: Done with ${osdhost} - Please confirm when you're ready to move on to the next osd node. Press any key to continue, CTRL-C to quit"
    read ans
done
log "INFO: Done. ... document further steps. https://docs.ceph.com/en/quincy/rados/troubleshooting/troubleshooting-mon/#mon-store-recovery-using-osds"
log "INFO: ceph-monstore-tool ${dirbase} rebuild -- --keyring /path/to/admin.keyring --mon-ids alpha beta gamma"
log "INFO: Need to specify mon-ids in numerical IP address order"
log "INFO: Final Results : $(ls -laR $dirbase)"
if [ ! -e $dirbase/ms/store.db ]; then
    log "ERROR:  Something did not go as expected. No store.db directory generated."
fi
