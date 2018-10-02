# ceph-misc
Miscellaneous scripts I created for various tasks with Ceph.

## Tool Explanations:

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
        1538459815.7950 2018-10-02 05:56:55.795025 initiated
         23.3882 2018-10-02 05:57:19.183485 reached_pg
         20.8710 2018-10-02 05:57:18.852467 waiting for rw locks
          1.7035 2018-10-02 05:57:20.889458 sub_op_commit_rec from 250
          0.0019 2018-10-02 05:57:19.185953 op_commit
          0.0007 2018-10-02 05:57:20.890195 sub_op_commit_rec from 137
          0.0003 2018-10-02 05:57:19.183932 commit_queued_for_journal_write
          0.0003 2018-10-02 05:56:55.795327 queued_for_pg
          0.0001 2018-10-02 05:57:19.184058 journaled_completion_queued
          0.0001 2018-10-02 05:57:19.183624 waiting for subops from 137,250
          0.0001 2018-10-02 05:57:20.890274 commit_sent
          0.0000 2018-10-02 05:57:19.183526 started
          0.0000 2018-10-02 05:57:20.890306 done
          0.0000 2018-10-02 05:57:19.183950 write_thread_in_journal_buffer
          0.0000 2018-10-02 05:57:19.185967 op_applied
2018-10-02 05:56:55.854901 osd_op(client.46802596.0:14766757 3.39828228 3:1441419c:::rbd_data.b9722b2e598557.00000000000000ab:head [stat,set-alloc-hint object_size 4194304 write_size 4194304,write 1978368~118784] snapc 0=[] ack+ondisk+write+known_if_redirected e49197)
        1538459815.8549 2018-10-02 05:56:55.854901 initiated
         25.0640 2018-10-02 05:57:20.919182 reached_pg
         21.2026 2018-10-02 05:57:19.184133 waiting for rw locks
          0.0077 2018-10-02 05:57:20.927876 sub_op_commit_rec from 137
          0.0003 2018-10-02 05:57:20.920168 op_applied
          0.0003 2018-10-02 05:57:20.919609 commit_queued_for_journal_write
          0.0003 2018-10-02 05:57:20.928153 sub_op_commit_rec from 250
          0.0003 2018-10-02 05:56:55.855158 queued_for_pg
          0.0002 2018-10-02 05:57:20.919791 journaled_completion_queued
          0.0001 2018-10-02 05:57:20.919315 waiting for subops from 137,250
          0.0001 2018-10-02 05:57:20.919847 op_commit
          0.0001 2018-10-02 05:57:20.919233 started
          0.0000 2018-10-02 05:57:20.928183 commit_sent
          0.0000 2018-10-02 05:57:20.919628 write_thread_in_journal_buffer
          0.0000 2018-10-02 05:57:20.928190 done
2018-10-02 05:56:55.858645 osd_op(client.46802596.0:14766758 3.39828228 3:1441419c:::rbd_data.b9722b2e598557.00000000000000ab:head [stat,set-alloc-hint object_size 4194304 write_size 4194304,write 2666496~4096] snapc 0=[] ack+ondisk+write+known_if_redirected e49197)
        1538459815.8586 2018-10-02 05:56:55.858645 initiated
         25.0711 2018-10-02 05:57:20.929862 reached_pg
         21.2025 2018-10-02 05:57:19.184197 waiting for rw locks
          0.0024 2018-10-02 05:57:20.933037 sub_op_commit_rec from 250
          0.0003 2018-10-02 05:57:20.930271 commit_queued_for_journal_write
          0.0002 2018-10-02 05:57:20.930677 op_applied
          0.0002 2018-10-02 05:56:55.858803 queued_for_pg
          0.0001 2018-10-02 05:57:20.930453 op_commit
          0.0001 2018-10-02 05:57:20.929992 waiting for subops from 137,250
          0.0001 2018-10-02 05:57:20.930351 journaled_completion_queued
          0.0000 2018-10-02 05:57:20.933108 commit_sent
          0.0000 2018-10-02 05:57:20.929906 started
          0.0000 2018-10-02 05:57:20.933063 sub_op_commit_rec from 137
          0.0000 2018-10-02 05:57:20.930293 write_thread_in_journal_buffer
          0.0000 2018-10-02 05:57:20.933125 done
```
