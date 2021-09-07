#!/usr/bin/awk -f
#
# Pipe the output of 'ceph pg dump' into this script.
#
# Dumps the listing of data counts going to / moving from
# each OSD ID during backfill/recovery operations.
#
# NOTE: Total gigabytes output assumes replicated pool,
# for EC, divide the value by K ( EC K+M ) to get a 'close'
# estimate.
#
# To have the script do that for you, provide '-v k=#'
# where # is the K value for EC K+M.
#
# Example:
#  ceph pg dump | ./all_data_to_move.awk -v k=8
#
# NOTE: If not all Pools are EC or Replicated, you will
# need to grep for a specific poolid (or IDs) on the input
# to this script so that the 'k' value applies properly.
#
# Example, pool 3 is EC 8+3, but the rest are replicated
#  ceph pg dump | grep ^3\\. | ./all_data_to_move.awk -v k=8
#  ceph pg dump | grep -v ^3\\. | ./all_data_to_move.awk
#

BEGIN { 
    if(k=="")
        k=1
}

function movePG(toosdid,fromosdid) {
    if(toosdid>=0) {
        osds[toosdid]=1
        osdmove["pgcount"][toosdid]["to"]++
        osdmove["objects"][toosdid]["to"]+=$2
        osdmove["degraded"][toosdid]["to"]+=$4
        osdmove["misplaced"][toosdid]["to"]+=$5
        osdmove["gigabytes"][toosdid]["to"]+=$7/1024/1024/1024
    }
    if(fromosdid>=0) {
        osds[fromosdid]=1
        osdmove["pgcount"][fromosdid]["from"]++
        osdmove["objects"][fromosdid]["from"]+=$2
        osdmove["degraded"][fromosdid]["from"]+=$4
        osdmove["misplaced"][fromosdid]["from"]+=$5
        osdmove["gigabytes"][fromosdid]["from"]+=$7/1024/1024/1024
    }
}

/^[0-9][0-9]*\.[0-9a-f][0-9a-f]* / {
    gsub(/(\[|\])/,"",$17)
    gsub(/(\[|\])/,"",$19)
    split($17,upset,",")
    split($19,actset,",")

    if(k>1) { 
        # EC Pool, check positions, not just presence / absence
        for(i=1;i<=length(upset);i++) {
            if(upset[i]!=actset[i])
                movePG(upset[i],actset[i])
        }
    } else {
        for(upid in upset) {
            didmatch=0
            for(actid in actset) {
                if(upid==actid)
                    didmatch=1
            }
            if(didmatch==0)
                movePG(upid,-1)
        }
        for(actid in actset) {
            didmatch=0
            for(upid in upset) {
                if(upid==actid)
                    didmatch=1
            }
            if(didmatch==0)
                movePG(-1,actid)
        }
    }
}

END {
    txtfields="pgcount objects degraded misplaced gigabytes"
    txtdirs="to from"
    split(txtfields,fields," ")
    split(txtdirs,dirs," ")
    printf("osd.id")
    for(fid in fields) {
        for(did in dirs) {
            printf(",%s %s",dirs[did],fields[fid])
            if(field=="gigabytes" && k>1) {
                printf("/%d",k)
            }
        }
    }
    print ""
    osdcount=asorti(osds,sortedosds)
    for(id=1;id<=osdcount;id++) {
        printf("%s",sortedosds[id])
        for(fid in fields) {
            for(did in dirs) {
                if(field=="gigabytes") {
                    printf(",%0.2f",osdmove[fields[fid]][sortedosds[id]][dirs[did]]/k)
                } else {
                    printf(",%s",osdmove[fields[fid]][sortedosds[id]][dirs[did]])
                }
            }
        }
        print ""
    }
}
