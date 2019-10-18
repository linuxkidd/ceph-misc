#!/usr/bin/awk -f
#
# Presuming you have an sosreport output structure like:
#   $hostname/sosreport_contents
# Run:
#   # cat */ps | find_ceph.awk
#
# From the directory with multiple $hostname sosreport output directories
#
#
#

{
    split($1,a,":");
    split(a[1],b,"/");
    myprocs[b[1]][$11]++;
    myhosts[b[1]]=1;
}

END {
    for(myhost in myhosts) {
        print myhost;
        print "-----------------";
        for(myproc in myprocs[myhost]) {
            print myproc"\t"myprocs[myhost][myproc];
        }
        print ""
    }
}
