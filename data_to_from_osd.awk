#!/usr/bin/awk -f
#
# Pipe the output of 'ceph pg dump' into this script,
# apped '-v osd=#' where # is the numeric ID of an OSD
# to check.
#
# Dumps the listing of PGs going to / moving from the
# specified OSD id during backfill/recovery operations,
# along with totals for objects, objects misplaced,
# objects degraded, and total bytes.
#
# NOTE: Total gigabytes output assumes replicated pool,
# for EC, divide the value by K ( EC K+M ) to get a 'close'
# estimate.
#
# To have the script do that for you, provide '-v k=#'
# where # is the K value for EC K+M.
#
# Example:
#  ceph pg dump | data_to_from_osd.awk -v osd=100 -v k=8
# 


BEGIN { 
    pattern="(\\[|,)"osd"(,|\\])";
    if(k=="")
        k=1
}

/^[0-9][0-9]*\.[0-9a-f][0-9a-f]* / {
    if($17 ~ pattern && $19 !~ pattern) 
    {
        to_osd=to_osd" "sprintf("%s\t%s\t%s\t%s\t%s\n", $1, $2, $4, $5, $7)
        to["objects"]+=$2
        to["degraded"]+=$4
        to["misplaced"]+=$5
        to["gigabytes"]+=$7/1024/1024/1024
    }
    else if ($17 !~ pattern && $19 ~ pattern)
    {
        from_osd=from_osd" "sprintf("%s\t%s\t%s\t%s\t%s\n", $1, $2, $4, $5, $7)
        from["objects"]+=$2
        from["degraded"]+=$4
        from["misplaced"]+=$5
        from["gigabytes"]+=$7/1024/1024/1024
    }
}

END {
    print "To osd "osd": "
    print " PG\tObjects\tDegrad\tMispla\tBytes"
    print to_osd
    print "\n\nFrom osd "osd":"
    print " PG\tObjects\tDegrad\tMispla\tBytes"
    print from_osd
    print "\n\n\t\tTo:\tFrom:"
    for(i in from) 
    {
        if(i=="gigabytes")
            print i":\t"to[i]/k"\t"from[i]/k
        else
            print i":\t"to[i]"\t"from[i]
    }
}