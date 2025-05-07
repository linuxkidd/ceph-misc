# ceph-misc
Miscellaneous scripts I created for various tasks with Ceph.

## Table of Contents:
- [parse_ms_response_times.awk](#parse_ms_response_timesawk)
- [colo_lvm_osds.sh](#colo_lvm_osdssh)
- [dedicated_lvm_osds.sh](#dedicated_lvm_osdssh)
- [find_upmap_items.py](#find_upmap_itemspy)
- [parse_historic_ops.py](#parse_historic_opspy)
- [update_rbd_direcotry.py](#update_rbd_directorypy)

Missing documents for a script?  Check the file contents for more details.

## Tool Explanations:

#### parse_ms_response_times.awk
- With `debug_ms=1` set, pass the resulting log file to the script
- The script will output a CSV format detailing each message

##### Notes:
- This parser is targeted at OSD bound messages and replies.
- It may kinda work on messages to other Ceph services, but has not been tested.
- Tested with output from Ceph 14.x.  Newer releases may have changed log formatting causing unexpected results.

##### Example:
```
./parse_ms_response_times.awk ceph-client.rgw.log
start,end,duration,osd,pg,object
2025-05-07 02:56:32.495,2025-05-07 02:56:47.127,14.632000,osd.390,58.54,.dir.default.437320374.599592.760
2025-05-07 02:56:32.481,2025-05-07 02:56:47.114,14.633000,osd.390,58.25,.dir.default.437320374.599592.686
2025-05-07 02:56:32.501,2025-05-07 02:56:47.134,14.633000,osd.390,58.79,.dir.default.437320374.599592.787
2025-05-07 02:56:32.485,2025-05-07 02:56:47.120,14.635000,osd.390,58.1a5,.dir.default.437320374.599592.709
2025-05-07 02:56:32.497,2025-05-07 02:56:47.132,14.635000,osd.390,58.1b9,.dir.default.437320374.599592.769
2025-05-07 02:56:32.498,2025-05-07 02:56:47.138,14.640000,osd.390,58.95,.dir.default.437320374.599592.777
```

##### Column definitions:
- start: The start time when the message was sent
- end: The end time when the response was received
- duration: Duration between start and end in seconds
- osd: The OSD where the message was sent
- pg: The PG id for the object
- object: The object name of the op

#### colo_lvm_osds.sh
- This script creates LVM layout on a single disk for deploying with `ceph-volume lvm` feature.
- This script outputs the exact syntax needed to apply into the `lvm_volumes:` section of `/usr/share/ceph-ansible/group_vars/osds.yml`
- Run with desired journal size and path(s) to devices

```
./colo_lvm_osds.sh 10 /dev/vd{b..d}
./colo_lvm_osds.sh 25 /dev/sd{b..z}
```

###### Example:
```
# ./colo_lvm_osds.sh 10 /dev/vd{b..d}
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
  Physical volume "/dev/vdb1" successfully created.
  Volume group "ceph-vdb" successfully created
  Wiping crypto_LUKS signature on /dev/ceph-vdb/journal.
  Logical volume "journal" created.
  Logical volume "data" created.
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
  Physical volume "/dev/vdc1" successfully created.
  Volume group "ceph-vdc" successfully created
  Wiping crypto_LUKS signature on /dev/ceph-vdc/journal.
  Logical volume "journal" created.
  Logical volume "data" created.
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
  Physical volume "/dev/vdd1" successfully created.
  Volume group "ceph-vdd" successfully created
  Wiping crypto_LUKS signature on /dev/ceph-vdd/journal.
  Logical volume "journal" created.
  Logical volume "data" created.

Creation Complete!

Please add the following to the 'lvm_volumes:' section of /usr/share/ceph-ansible/group_vars/osds.yml

  - data: ceph-vdb/data
    journal: ceph-vdb/journal
  - data: ceph-vdc/data
    journal: ceph-vdc/journal
  - data: ceph-vdd/data
    journal: ceph-vdd/journal
```

#### dedicated_lvm_osds.sh
- This script creates LVM layout on a set of disks for deploying with `ceph-volume lvm` feature.
- This script outputs the exact syntax needed to apply into the `lvm_volumes:` section of `/usr/share/ceph-ansible/group_vars/osds.yml`
- Run with desired action, journal size, path to journal device and path(s) to data devices
- NOTE: All 'data devices' will share the provided journal device.

```
./dedicated_lvm_osds.sh new 10 /dev/vde /dev/vd{b..d}
./dedicated_lvm_osds.sh add 25 /dev/sdg /dev/sd{b..f}
```

###### Example:
```
# ./dedicated_lvm_osds.sh new 10 /dev/vde /dev/vd{b..d}
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
The operation has completed successfully.
  Physical volume "/dev/vde1" successfully created.
  Volume group "ceph-vde" successfully created
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
The operation has completed successfully.
  Physical volume "/dev/vdb1" successfully created.
  Volume group "ceph-vdb" successfully created
  Logical volume "journal-vdb" created.
  Wiping crypto_LUKS signature on /dev/ceph-vdb/data.
  Logical volume "data" created.
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
The operation has completed successfully.
  Physical volume "/dev/vdc1" successfully created.
  Volume group "ceph-vdc" successfully created
  Logical volume "journal-vdc" created.
  Wiping crypto_LUKS signature on /dev/ceph-vdc/data.
  Logical volume "data" created.
GPT data structures destroyed! You may now partition the disk using fdisk or
other utilities.
Creating new GPT entries.
Setting name!
partNum is 0
REALLY setting name!
The operation has completed successfully.
  Physical volume "/dev/vdd1" successfully created.
  Volume group "ceph-vdd" successfully created
  Logical volume "journal-vdd" created.
  Wiping crypto_LUKS signature on /dev/ceph-vdd/data.
  Logical volume "data" created.

Creation Complete!

Please add the following to the 'lvm_volumes:' section of /usr/share/ceph-ansible/group_vars/osds.yml

  - data: ceph-vdb/data
    journal: ceph-vde/journal-vdb
  - data: ceph-vdc/data
    journal: ceph-vde/journal-vdc
  - data: ceph-vdd/data
    journal: ceph-vde/journal-vdd
```

#### find_upmap_items.py
- This script parses the output of `ceph report` to show PGs which have upmap items
```
ceph report | ./find_upmap_items.py
```
###### Example:
```
# ceph report | ./find_upmap_items.py
12.a
12.14
13.1
13.3
15.0
15.1
15.2
15.4
15.6
15.7
15.9
15.a
15.10
15.11
15.13
15.15
15.17
15.23
...
```

#### parse_historic_ops.py
- This script parses the output of `ceph daemon osd.<id> dump_historic_ops` to show the total time spent in each event for each slow op.
- The output shows Date/Time of the start of the Op followed by the Op description, then 1 line for each op type along with the cumulative time in seconds and completed time per op type.

```
ceph daemon osd.<id> dump_historic_ops | ./parse_historic_ops.py
```
###### Example:
```
# ceph daemon osd.104 dump_historic_ops | ./parse_historic_ops.py
2018-10-02 05:56:55.795025 osd_op(client.46802596.0:14766739 3.39828228 3:1441419c:::rbd_data.b9722b2e598557.00000000000000ab:head [stat,set-alloc-hint object_size 4194304 write_size 4194304,write 2580480~86016] snapc 0=[] ack+ondisk+write+known_if_redirected e49197)
          0.0000 2018-10-02 05:56:55.795025 initiated
          0.0003 2018-10-02 05:56:55.795327 queued_for_pg
         20.8710 2018-10-02 05:57:18.852467 waiting for rw locks
         23.3882 2018-10-02 05:57:19.183485 reached_pg
          0.0000 2018-10-02 05:57:19.183526 started
          0.0001 2018-10-02 05:57:19.183624 waiting for subops from 137,250
          0.0003 2018-10-02 05:57:19.183932 commit_queued_for_journal_write
          0.0000 2018-10-02 05:57:19.183950 write_thread_in_journal_buffer
          0.0001 2018-10-02 05:57:19.184058 journaled_completion_queued
          0.0019 2018-10-02 05:57:19.185953 op_commit
          0.0000 2018-10-02 05:57:19.185967 op_applied
          1.7058 2018-10-02 05:57:20.889458 sub_op_commit_rec from 250
          1.7066 2018-10-02 05:57:20.890195 sub_op_commit_rec from 137
          0.0001 2018-10-02 05:57:20.890274 commit_sent
          0.0000 2018-10-02 05:57:20.890306 done
2018-10-02 05:56:55.854901 osd_op(client.46802596.0:14766757 3.39828228 3:1441419c:::rbd_data.b9722b2e598557.00000000000000ab:head [stat,set-alloc-hint object_size 4194304 write_size 4194304,write 1978368~118784] snapc 0=[] ack+ondisk+write+known_if_redirected e49197)
          0.0000 2018-10-02 05:56:55.854901 initiated
          0.0003 2018-10-02 05:56:55.855158 queued_for_pg
         21.2026 2018-10-02 05:57:19.184133 waiting for rw locks
         25.0640 2018-10-02 05:57:20.919182 reached_pg
          0.0001 2018-10-02 05:57:20.919233 started
          0.0001 2018-10-02 05:57:20.919315 waiting for subops from 137,250
          0.0003 2018-10-02 05:57:20.919609 commit_queued_for_journal_write
          0.0000 2018-10-02 05:57:20.919628 write_thread_in_journal_buffer
          0.0002 2018-10-02 05:57:20.919791 journaled_completion_queued
          0.0001 2018-10-02 05:57:20.919847 op_commit
          0.0003 2018-10-02 05:57:20.920168 op_applied
          0.0086 2018-10-02 05:57:20.927876 sub_op_commit_rec from 137
          0.0088 2018-10-02 05:57:20.928153 sub_op_commit_rec from 250
          0.0000 2018-10-02 05:57:20.928183 commit_sent
          0.0000 2018-10-02 05:57:20.928190 done
2018-10-02 05:56:55.858645 osd_op(client.46802596.0:14766758 3.39828228 3:1441419c:::rbd_data.b9722b2e598557.00000000000000ab:head [stat,set-alloc-hint object_size 4194304 write_size 4194304,write 2666496~4096] snapc 0=[] ack+ondisk+write+known_if_redirected e49197)
          0.0000 2018-10-02 05:56:55.858645 initiated
          0.0002 2018-10-02 05:56:55.858803 queued_for_pg
         21.2025 2018-10-02 05:57:19.184197 waiting for rw locks
         25.0711 2018-10-02 05:57:20.929862 reached_pg
          0.0000 2018-10-02 05:57:20.929906 started
          0.0001 2018-10-02 05:57:20.929992 waiting for subops from 137,250
          0.0003 2018-10-02 05:57:20.930271 commit_queued_for_journal_write
          0.0000 2018-10-02 05:57:20.930293 write_thread_in_journal_buffer
          0.0001 2018-10-02 05:57:20.930351 journaled_completion_queued
          0.0001 2018-10-02 05:57:20.930453 op_commit
          0.0002 2018-10-02 05:57:20.930677 op_applied
          0.0030 2018-10-02 05:57:20.933037 sub_op_commit_rec from 250
          0.0031 2018-10-02 05:57:20.933063 sub_op_commit_rec from 137
          0.0000 2018-10-02 05:57:20.933108 commit_sent
          0.0000 2018-10-02 05:57:20.933125 done
```

#### update_rbd_directory.py
- This script repairs missing omap entries on `rbd_directory`, which are used by `rbd ls` command for listing RBDs
- Usage help: ./update_rbd_directory.py --help
```
usage: update_rbd_directory.py [-h] [-c CONF] [-f] [-p POOL]

options:
  -h, --help            show this help message and exit
  -c CONF, --conf CONF  Ceph config file to use ( default: /etc/ceph/ceph.conf )
  -f, --force           Force refresh of all OMAP entries ( default: not-set )
  -p POOL, --pool POOL  Pool to use for RBD directory correction ( default: rbd )
```

##### Example:
```
[root@mons-0 ~]# rbd create --size 10 rbd/linuxkidd-test

[root@mons-0 ~]# rbd -p rbd ls
linuxkidd-test

[root@mons-0 ~]# rbd -p rbd ls -l
NAME           SIZE   PARENT FMT PROT LOCK 
linuxkidd-test 10 MiB          2

[root@mons-0 ~]# rados -p rbd listomapvals rbd_directory
id_131d41d5a28b
value (18 bytes) :
00000000  0e 00 00 00 6c 69 6e 75  78 6b 69 64 64 2d 74 65  |....linuxkidd-te|
00000010  73 74                                             |st|
00000012

name_linuxkidd-test
value (16 bytes) :
00000000  0c 00 00 00 31 33 31 64  34 31 64 35 61 32 38 62  |....131d41d5a28b|
00000010

[root@mons-0 ~]# ./update_rbd_directory.py 
No missing entries.

[root@mons-0 ~]# rados -p rbd rmomapkey rbd_directory name_linuxkidd-test
[root@mons-0 ~]# rados -p rbd rmomapkey rbd_directory id_131d41d5a28b
[root@mons-0 ~]# rbd -p rbd ls
[root@mons-0 ~]# rbd -p rbd ls -l
[root@mons-0 ~]# rados -p rbd ls
rbd_object_map.131d41d5a28b
rbd_directory
rbd_id.linuxkidd-test
rbd_info
rbd_header.131d41d5a28b

[root@mons-0 ~]# ./update_rbd_directory.py 
Missing name entry linuxkidd-test
Missing id entry 131d41d5a28b
Added 1 name entries, and 1 id entries.

[root@mons-0 ~]# rados -p rbd listomapvals rbd_directory
id_131d41d5a28b
value (18 bytes) :
00000000  0e 00 00 00 6c 69 6e 75  78 6b 69 64 64 2d 74 65  |....linuxkidd-te|
00000010  73 74                                             |st|
00000012

name_linuxkidd-test
value (16 bytes) :
00000000  0c 00 00 00 31 33 31 64  34 31 64 35 61 32 38 62  |....131d41d5a28b|
00000010

[root@mons-0 ~]# rbd -p rbd ls
linuxkidd-test

[root@mons-0 ~]# rbd -p rbd ls -l
NAME           SIZE   PARENT FMT PROT LOCK 
linuxkidd-test 10 MiB          2
```
