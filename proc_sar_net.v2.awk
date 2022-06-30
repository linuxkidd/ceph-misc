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
    return sprintf("%s %02d:%02d:00",mydate,tparts[1],tparts[2])
}

/IFACE/ {
    printstat[myhost]++
    next
}

/^Average/ && printstat[myhost]==1{
    printstat[myhost]++
    next
}

/^Linux / {
    mydate=$4
    myhost=$3
    gsub(/[^a-zA-Z0-9\-\_\.]/,"",myhost)
    MYHOSTS[myhost]=1
}

/^[0-9]*:[0-9]*:[0-9]* */ && printstat[myhost]==1 {
    if($2~/[AP]M/) {
        dt=mydtstamp()
        rx=$6/1024
        tx=$7/1024
        iface=$3
    } else {
        dt=mydate" "$1
        gsub(/:[0-9][0-9]$/,":00",dt)
        iface=$2
        rx=$5/1024
        tx-$6/1024
    }
    if(iface!="lo") {
        IFSTAT[dt][myhost][iface"-rx"]=rx
        IFSTAT[dt][myhost][iface"-tx"]=tx
        IFACES[iface]=1
    }
}

END {
    printf("time")

    n=asorti(IFACES,IFS)
    dtcount=asorti(IFSTAT,DTSTAMPS)
    hscount=asorti(MYHOSTS,MYHS)
    for(m=1;m<=hscount;m++) {
        for(i=1;i<=n;i++) {
            printf(","MYHS[m]"-"IFS[i]"-rx,"MYHS[m]"-"IFS[i]"-tx")
        }
    }
    print ""
    for(i=1;i<=dtcount;i++) {
        mydt=DTSTAMPS[i]
        printf(mydt)
        for(m=1;m<=hscount;m++) {
            for(j=1;j<=n;j++)
                printf(","IFSTAT[mydt][MYHS[m]][IFS[j]"-rx"]","IFSTAT[mydt][MYHS[m]][IFS[j]"-tx"])
        }
        print ""
    }
}
