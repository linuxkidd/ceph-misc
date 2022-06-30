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

/DEV/ {
    printstat[myhost]++
    next
}

/^Average/ && printstat[myhost]==1 {
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
        DEVSTAT[dt][myhost"-"$3]["await"]=$10
        DEVSTAT[dt][myhost"-"$3]["util"]=$NF
        DEVICES[myhost"-"$3]=1
    } else {
        dt=mydate" "$1
        gsub(/:[0-9][0-9]$/,":00",dt)
        DEVSTAT[dt][myhost"-"$2]["await"]=$9
        DEVSTAT[dt][myhost"-"$2]["util"]=$NF
        DEVICES[myhost"-"$2]=1
    }
}

END {
    printf("time")>>"await.csv"
    printf("time")>>"util.csv"

    n=asorti(DEVICES,DEVS)
    for(i=1;i<=n;i++) {
        printf(","DEVS[i]) >> "await.csv"
        printf(","DEVS[i]) >> "util.csv"
    }
    print "" >> "await.csv"
    print "" >> "util.csv"
    dtcount=asorti(DEVSTAT,DTSTAMPS)
    for(i=1;i<=dtcount;i++) {
        mydt=DTSTAMPS[i]
        printf(mydt)>>"await.csv"
        printf(mydt)>>"util.csv"
        for(j=1;j<=n;j++) {
            printf(","DEVSTAT[mydt][DEVS[j]]["await"])>>"await.csv"
            printf(","DEVSTAT[mydt][DEVS[j]]["util"])>>"util.csv"
        }
        print "" >>"await.csv"
        print "" >>"util.csv"
    }
}
