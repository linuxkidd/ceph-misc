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
osd_list=$(ceph orch ps | awk '/^osd\.[0-9][0-9]* / { gsub(/[^0-9]/,"",$1); osdlist[$2]=osdlist[$2]","$1 } END { hcount=asorti(osdlist,sorted); for(i=1;i<=hcount;i++) { print sorted[i] osdlist[sorted[i]]; }}')
checkReturn $? "OSD list" 1

dirbase=/tmp/monrecovery.$(date +%F_%H-%M-%S)
log "INFO: Setting up directory structure in ${dirbase}"
for mydir in ms db db_slow logs; do
    mkdir -p "${dirbase}/${mydir}" &> /dev/null
done

log "INFO: Creating osd_mon-store.db_rebuild.sh script"
cat <<EOF > ${dirbase}/osd_mon-store.db_rebuild.sh
#!/bin/bash

log() {
  echo \$(date +%F\ %T) \${HOSTNAME} "\$1"
}

checkReturn() {
    if [ \$1 -ne 0 ]; then
        log "ERROR: \${2} failed: returned \${1}"
    fi
}

log "INFO: Sleep 5 seconds for filesystem stabilization"
sleep 5

recopath=/var/log/ceph/monrecovery
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
    log "INFO: Putting host ${osdhost} into maintenance mode"
    ceph orch host maintenance enter ${osdhost} --force 2>&1 >> ${dirbase}/logs/${osdhost}_mgmt.log

    pushData $osdhost

    log "INFO: Starting osd_mon-store.db_rebuild.sh loop on ${osdhost}"
    ssh ${osdhost} <<EOF
for osdid in ${osdids}; do
    cephadm shell --name osd.\${osdid} /var/log/ceph/monrecovery/osd_mon-store.db_rebuild.sh
done
EOF

    pullData ${osdhost}

    log "INFO: Removing host ${osdhost} from maintenance mode"
    ceph orch host maintenance exit ${osdhost} 2>&1 >> ${dirbase}/logs/${osdhost}_mgmt.log
done

