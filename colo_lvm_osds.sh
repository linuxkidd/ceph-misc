#!/bin/bash
#
#  colo_lvm_osds.sh
#
# by Michael J. Kidd (linuxkidd@gmail.com)
# Version 1.0, 2018-09-27, Initial Release
#


usage() {
    cat <<EOF
Usage:
    $0 <journalsize> <devpath> [<devpath>...]

Where:
    <journalsize>  The desired size of the journal in Gigabytes
                   Example, for 10 GB: 10

    <devpath>      The device path of the new co-located OSD
                   Multiple can be specified as space separated list
                   Example: /dev/sdb /dev/sdc

Examples:
    $0 10 /dev/sdb /dev/sdc

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

if [ $# -lt 2 ]; then
    echo "Error: Too few arguments"
    echo
    usage
fi

if [ $(echo $1 | grep -c  ^[0-9]*$) -ne 1 ]; then
    echo "Error: $1 is not an integer."
    echo
    usage
fi

devlist=()
for((devidx=2;devidx<=$#;devidx++)) {
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

for devpath in "${devlist[@]}"; do
  parted -s $devpath mklabel gpt
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "parted failed to wipe ${devpath} partition table with error code $RETVAL" $RETVAL
  fi

  fdisk $devpath &> /dev/null <<EOF
n



w
EOF
  RETVAL=$?
  if [ $RETVAL -ne 0 ]; then
    errorout "fdisk failed to create the necessary partition on ${devpath} with error code $RETVAL" $RETVAL
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

  journalExtents=$((($1*1024)/4))
  lvcreate -l $journalExtents -n journal ceph-$(basename $devpath) ${devpath}1 -y
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
    journal: ceph-$(basename $devpath)/journal
EOF
done

