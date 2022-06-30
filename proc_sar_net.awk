#!/usr/bin/awk -f


function mydtstamp(mydt) {
    split($1,tparts,":")
    switch($2){ 
        case "AM":
            if(tparts[1]==12)
                tparts[1]="00"
            break
        case "PM":
            if(tparts[1]<12)
                tparts[1]+=12
            break
    }
    return sprintf("%s %02d:%02d:00,%s",mydate,tparts[1],tparts[2],myhost)
}

BEGIN {
    print "host,iface,time,host,RX,TX"
}

/IFACE/ {
    printstat++
    if(printstat>1)
        exit
    next
}

/^Linux / {
    mydate=$4
    myhost=$3
    gsub(/[^a-zA-Z0-9\-\_\.]/,"",myhost)
}

/^[0-9]*:[0-9]*:[0-9]* */ {
    if(printstat==1) {
        if($2~/[AP]M/) {
            dt=mydtstamp()
            rx=$6/1024
            tx=$7/1024
            iface=$3
        } else {
            dt=mydate" "$1
            iface=$2
        }
        if(iface!="lo")
            print myhost","iface","dt","rx","tx
    }
}
