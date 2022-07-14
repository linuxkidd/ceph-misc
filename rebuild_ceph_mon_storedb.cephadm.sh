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

osdid=$1
log "INFO: Gathering OSD list"
# CSV: host,osd[,osd]...
osd_list=$(ceph orch ps | awk '/^osd\.[0-9][0-9]* / { gsub(/[^0-9]/,"",$1); osdlist[$2]=osdlist[$2]","$1 } END { hcount=asorti(osdlist,sorted); for(i=1;i<=hcount;i++) { print sorted[i] osdlist[sorted[i]]; }}')
checkReturn $? "OSD list" 1


dirbase=/tmp/monrecovery.$(date +%F_%H-%M-%S)
log "INFO: Setting up directory structure in ${dirbase}"
localdirs="ms db db_slow"

for mydir in $localdirs; do
    mkdir -p "${dirbase}/${mydir}" &> /dev/null
done

log "INFO: Creating osd_rebuild.sh script"
cat <<EOF > ${dirbase}/osd_rebuild.sh
#!/bin/bash

log() {
  echo \$(date +%F\ %T) \${HOSTNAME} "\$1"
}

checkReturn() {
    if [ \$1 -ne 0 ]; then
        log "ERROR: \${2} failed: returned \${1}"
    fi
}

datadir=/var/lib/ceph/osd/ceph-*
if [ ! -e \$datadir ]; then
    log "ERROR: No OSD data directory found"
    exit 1
fi
recopath=/var/log/ceph/monrecovery
log "INFO: Moving db and db_slow to ~/"
mv \${recopath}/{db,db_slow} ~/
log "INFO: Running update-mon-db on \${datadir}"
cd ~/
ceph-objectstore-tool --data-path \${datadir} --op  update-mon-db --no-mon-config --mon-store-path \${recopath}/ms
checkReturn \$? "COT update-mon-db"

if [ -e \${datadir}/keyring ]; then
    cat \${datadir}/keyring >> \${recopath}/ms/keyring
    echo '    caps mgr = "allow profile osd"' >> \${recopath}/ms/keyring
    echo '    caps mon = "allow profile osd"' >> \${recopath}/ms/keyring
    echo '    caps osd = "allow *"' >> \${recopath}/ms/keyring
    echo > \${recopath}/ms/keyring
else
    log "WARNING: \${datadir} does not have a local keyring."
fi

log "INFO: Moving db and db_slow from ~/"
mv ~/{db,db_slow} /var/log/ceph/monrecovery/
EOF
chmod 755 ${dirbase}/osd_rebuild.sh

pullData() {
    log "INFO: Pulling ${1}:/var/log/ceph/${fsid}/monrecovery/"
    rsync -avz --delete --remove-source-files ${1}:/var/log/ceph/${fsid}/monrecovery/ ${dirbase}/
    checkReturn $? "Pulling ${1}:/var/lib/ceph/${fsid}/monrecovery" 1
}

pushData() {
    log "INFO: Pushing ${1}:/var/log/ceph/${fsid}/monrecovery/"
    rsync -avz --delete --remove-source-files ${dirbase}/ ${1}:/var/log/ceph/${fsid}/monrecovery/
    checkReturn $? "Pushing ${1}:/var/lib/ceph/${fsid}/monrecovery" 1
}

lasthost=""
for hostosd in $osd_list; do
    osdhost=$(echo $hostosd | sed -e 's/,.*$//')
    osdids=$(echo $hostosd | sed -e 's/^[^,]*,//' -e 's/,/ /g')

    pushData $osdhost

    log "INFO: Putting host ${osdhost} into maintenance mode"
    ceph orch host maintenance enter ${osdhost} --force

    log "INFO: Starting osd_recovery.sh loop on ${osdhost}"
    ssh ${osdhost} <<EOF
for osdid in ${osdids}; do
    cephadm shell --name osd.\${osdid} /var/lib/ceph/monrecovery/osd_recovery.sh
done
EOF

    pullData ${osdhost}

    log "INFO: Removing host ${osdhost} from maintenance mode"
    ceph orch host maintenance exit ${osdhost}
done

