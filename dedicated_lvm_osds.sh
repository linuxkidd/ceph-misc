#!/bin/bash
#
#  dedicated_lvm_osds.sh
#
# by Michael J. Kidd (linuxkidd@gmail.com)
# Version 1.0, 2018-09-27, Initial Release
#


usage() {
    cat <<EOF
Usage:
    $0 <action> <journalsize> <journalpath> <devpath> [<devpath>...]

Where:
    <action>       One of the following:
                   new      - This wipes ALL devices ( Journal and Data )
                   add      - This wipes ALL Data devices, but adds onto the Journal device
                              NOTE: This 'add' scenario assumes the previous OSDs were deployed with this script

    <journalsize>  The desired size of the journal in Gigabytes
                   Example, for 10 GB: 10

    <journalpath>  The device path to the dedicated journal device for
                   ALL <devpath> OSDs
                   Example: /dev/sdj

    <devpath>      The device path of the new OSD data partitions.
                   Multiple can be specified as space separated list
                   Example: /dev/sdb /dev/sdc

WARNING:
    

Examples:
    $0 new 10 /dev/sdj /dev/sdb /dev/sdc

EOF
    exit 1
}

errorout() {
    cat <<EOF
ERROR!  Something went wrong.

$1
EOF
    exit $2
}

if [ $# -lt 4 ]; then
    echo "Error: Too few arguments"
    echo
    usage
fi

if [ x"$1" != x"new" ] && [ x"$1" != x"add" ]; then
    echo "Error: action ($1) was not 'new' or 'add'."
    echo
    usage
fi

if [ $(echo $2 | grep -c  ^[0-9]*$) -ne 1 ]; then
    echo "Error: $2 is not an integer."
    echo
    usage
fi

if [ ! -e $3 ]; then
    echo "Error: $3 does not exist."
    echo
    usage
fi

if [ ! -b $3 ]; then
    echo "Error: $3 is not a block device."
    echo
    usage
fi

action=$1
journalsize=$2
journalpath=$3

devlist=()
for((devidx=4;devidx<=$#;devidx++)) {
  if [ ! -e ${!devidx} ]; then
    echo "Error: ${!devidx} does not exist, skipping..."
    continue
  fi

  if [ ! -b ${!devidx} ]; then
    echo "Error: ${!devidx}  is not a block device, skipping..."
    continue
  fi
  devlist+=(${!devidx})
}

if [ ${#devlist[@]} -eq 0 ]; then
  echo "Error: None of the specified devices exist.  Exiting."
  echo
  usage
fi

prep_device() {
  devpath=$1
  devtype=$2
  sgdisk -Z $devpath
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "sgdisk failed to zap ${devpath} with error code $RETVAL" $RETVAL
  fi
  sgdisk -n 0:0:0 -t 8300 -c 0:"Ceph $2 LVM" $devpath
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "sgdisk failed to create the necessary partition on ${devpath} with error code $RETVAL" $RETVAL
  fi

  pvcreate ${devpath}1 -y
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "pvcreate failed to create the Physical Volume on ${devpath} with error code $RETVAL" $RETVAL
  fi

  vgcreate ceph-$(basename $devpath) ${devpath}1 -y
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "vgcreate failed to create the Volume Group ceph-$(basename $devpath) with error code $RETVAL" $RETVAL
  fi
}

if [ "$action" == "new" ]; then
  prep_device $journalpath Journal
fi

for devpath in "${devlist[@]}"; do
  prep_device $devpath OSD

  journalExtents=$((($journalsize*1024)/4))
  lvcreate -l $journalExtents -n journal-$(basename $devpath) ceph-$(basename $journalpath) ${journalpath}1 -y
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "lvcreate failed to create the Logical Volume ceph-$(basename $devpath)/journal with error code $RETVAL" $RETVAL
  fi

  lvcreate -l 100%FREE -n data ceph-$(basename $devpath) ${devpath}1 -y
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "lvcreate failed to create the Logical Volume ceph-$(basename $devpath)/data with error code $RETVAL" $RETVAL
  fi
done

cat <<EOF

Creation Complete!

Please add the following to the 'lvm_volumes:' section of /usr/share/ceph-ansible/group_vars/osds.yml

EOF
for devpath in "${devlist[@]}"; do
  cat <<EOF
  - data: ceph-$(basename $devpath)/data
    journal: ceph-$(basename $journalpath)/journal-$(basename $devpath)
EOF
done

