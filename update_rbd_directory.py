#!/usr/bin/env python
"""
Filename: update_rbd_directory.py
Written by: Michael J. Kidd aka linuxkidd
Version: 2.0
Last modified: 2022-03-16

Usage help:
  ./update_rbd_directory.py --help

Purpose:
This script processes the 'rados ls' output, looking for 'rbd_id.$name'
  entries, where $name is the name of an RBD.

Once found, it checks the 'rbd_directory' object to ensure the expected
  'name_$name' and 'id_$id' entries are present for the found RBD.

If, the 'name_$name' or 'id_$id' omap entries are missing, the script
  will stage the missing entries until the end of the object loop, at 
  which point, the missing entries will be added to the rbd_directory
  omap data.

NOTE:
- The 'name_$name' entry contains the ID value
- The 'id_$id' entry contains the NAME value
-- Otherwise, it wouldn't be much of a mapping, would it?

"""

import argparse,rados,rbd,struct

needed_count={ 'name':0, 'id': 0}
keys = []
values = []

antifield={ "name":"id", "id":"name" }
fields={}

"""
checkIter(iterator, fields, myfield)
 - iterator == ioctx iterator returned from 'get_omap_vals_by_keys'
 - fields   == dictionary with 'name' and 'id' values
 - myfield  == the current field, either 'name' or 'id', being checked

Try to iterate the omap key, if there are no values, the StopIteration
exception is thrown, meaning the omap key is missing and needs to be 
created.  The key == value pair are appended to the 'keys' / 'values'
global variables for addition by addOmap() function.
"""

def checkIter(iter,fields,myfield):
    try:
        mykey = iter.__next__()
    except StopIteration:
        # Always report the name, since the ID is harder to understand.
        print("Missing {} entry {}".format(myfield,fields['name']))

        needed_count[myfield]+=1
        keys.append("{}_{}".format(myfield,fields[myfield]))
        values.append("{}{}".format(struct.pack('<I',len(fields[antifield[myfield]])),fields[antifield[myfield]]))

"""
loopRados(ioctx)
 - ioctx == IO Control provided from cluster.open_ioctx() in __main__

Loops over the rados pool specified by args.pool ( via ioctx ), looking
for objects of name 'rbd_id.$name' where $name is the name of an RBD in
the pool.  Then, calls 'checkIter()' to confirm if the name and id keys
for the RBD are present.  If not, the needed keys/values are appended
to keys / values lists for later addition via 'addOmap()'.
"""
def loopRados(ioctx):
    for object in ioctx.list_objects():
        if object.key[:7]=="rbd_id.":
            myrbd = rbd.Image(ioctx,name=object.key[7:])
            fields={ "name": myrbd.get_name(), "id": myrbd.id() }
            with rados.ReadOpCtx() as read_op:
                for myfield in antifield.keys():
                    iter, ret = ioctx.get_omap_vals_by_keys(read_op, tuple(["{}_{}".format(myfield,fields[myfield]),]))
                    ioctx.operate_read_op(read_op,"rbd_directory")
                    checkIter(iter,fields,myfield)

"""
addOmap(ioctx)
 - ioctx == IO Control provided from cluster.open_ioctx() in __main__

Adds missing omap 'key = value' pairs to 'rbd_directory' object in the
rados pool specified by args.pool ( via ioctx ).  These values are 
populated by checkIter() called from loopRados().
"""
def addOmap(ioctx):
    if needed_count['name']>0 or needed_count['id']>0:
        with rados.WriteOpCtx() as write_op:
            ioctx.set_omap(write_op, tuple(keys), tuple(values))
            write_op.set_flags(rados.LIBRADOS_OPERATION_SKIPRWLOCKS)
            ioctx.operate_write_op(write_op, "rbd_directory")

        print("Added {} name entries, and {} id entries.".format(needed_count['name'],needed_count['id']))
    else:
        print("No missing entries.")
    ioctx.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--conf",     default = "/etc/ceph/ceph.conf", help="Ceph config file to use ( default: /etc/ceph/ceph.conf )")
    parser.add_argument("-p", "--pool",     default = "rbd",                 help="Pool to use for RBD directory correction ( default: rbd )")
    args = parser.parse_args()

    try:
        cluster = rados.Rados(conffile=args.conf)
    except:
        print("Failed to read {}, do you need to specify a different conf file with -c ?".format(args.conf))
        exit(1)

    try:
        cluster.connect()
    except:
        print("Failed to connect to cluster using: {}, do you need to specify a different conf file with -c ?".format(args.conf))
        exit(1)

    try:
        ioctx = cluster.open_ioctx(args.pool)
    except:
        print("Failed to open pool {}, do you need to specify a different pool with -p ?".format(args.pool))
        exit(1)

    loopRados(ioctx)
    addOmap(ioctx)