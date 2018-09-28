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
- This script parses the output of `ceph daemon osd.<id> dump_historic_ops` to show the op which took the longest time to complete for each slow event.
- The output shows the time in seconds, time of log entry, the Operation type, the full event description ( client, object, etc )

```
ceph daemon osd.<id> dump_historic_ops | ./parse_historic_ops.py
```
###### Example:
```
# ceph daemon osd.104 dump_historic_ops | ./parse_historic_ops.py
922.9391,2018-09-26 16:48:09.012033,reached_pg,osd_op(client.45043494.0:240719 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13807)
917.6075,2018-09-26 16:48:08.962761,wait for new map,pg_notify((query:13808 sent:13808 59.1c( v 13792'27216015 (13786'27214415,13792'27216015] local-lis/les=13791/13792 n=205 ec=1387/1387 lis/c 13791/13784 les/c/f 13792/13786/0 13808/13808/13803))=([13784,13807] intervals=([13791,13798] acting 50,54),([13803,13807] acting 50,104)) epoch 13808)
917.6390,2018-09-26 16:48:08.994288,wait for new map,pg_query(56.0,59.b,61.31 epoch 13808)
917.6390,2018-09-26 16:48:08.994344,wait for new map,pg_notify((query:13808 sent:13808 61.21( v 13786'46230 (12543'44725,13786'46230] local-lis/les=13784/13786 n=0 ec=1390/1390 lis/c 13784/13784 les/c/f 13786/13786/0 13808/13808/13803))=([13784,13807] intervals=([13784,13790] acting 54,101,104)) epoch 13808)
916.6448,2018-09-26 16:48:08.994377,wait for new map,pg_notify((query:13809 sent:13809 61.21( v 13786'46230 (12543'44725,13786'46230] local-lis/les=13784/13786 n=0 ec=1390/1390 lis/c 13784/13784 les/c/f 13786/13786/0 13808/13808/13803))=([13784,13807] intervals=([13784,13790] acting 54,101,104)) epoch 13809)
916.6414,2018-09-26 16:48:08.994429,wait for new map,pg_notify((query:13809 sent:13809 59.1c( v 13792'27216015 (13786'27214415,13792'27216015] local-lis/les=13791/13792 n=205 ec=1387/1387 lis/c 13791/13784 les/c/f 13792/13786/0 13808/13808/13803))=([13784,13807] intervals=([13791,13798] acting 50,54),([13803,13807] acting 50,104)) epoch 13809)
916.6223,2018-09-26 16:48:09.020689,reached_pg,osd_op(client.45044623.0:247181 59.0 59.f65fd260 (undecoded) ondisk+read+known_if_redirected e13809)
908.2419,2018-09-26 16:48:09.012057,reached_pg,osd_op(client.44949281.0:277866 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13809)
908.0087,2018-09-26 16:48:09.012054,reached_pg,osd_op(client.45043494.0:240752 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13809)
902.4838,2018-09-26 16:48:08.994752,reached_pg,osd_op(client.44950121.0:262737 59.1c 59.238f23c (undecoded) ondisk+read+known_if_redirected e13809)
896.2635,2018-09-26 16:48:08.997531,reached_pg,osd_op(client.45043978.0:299798 59.1c 59.238f23c (undecoded) ondisk+read+known_if_redirected e13809)
885.9745,2018-09-26 16:48:08.997412,reached_pg,osd_op(client.45044503.0:261967 59.1c 59.238f23c (undecoded) ondisk+read+known_if_redirected e13809)
885.9768,2018-09-26 16:48:09.012067,reached_pg,osd_op(client.45043494.0:240791 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13809)
885.8449,2018-09-26 16:48:08.997440,reached_pg,osd_op(client.45044503.0:261976 59.1c 59.238f23c (undecoded) ondisk+read+known_if_redirected e13809)
884.7025,2018-09-26 16:48:08.994840,reached_pg,osd_op(client.44950121.0:262785 59.1c 59.238f23c (undecoded) ondisk+read+known_if_redirected e13809)
880.0099,2018-09-26 16:48:09.015362,reached_pg,osd_op(client.44950121.0:262797 56.5 56.cd173545 (undecoded) ondisk+write+known_if_redirected e13809)
877.8218,2018-09-26 16:48:09.020962,reached_pg,osd_op(client.44950121.0:262820 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13809)
870.2831,2018-09-26 16:48:09.012059,reached_pg,osd_op(client.44980252.0:269795 61.30 61.75c0ea70 (undecoded) ondisk+read+known_if_redirected e13809)
830.6679,2018-09-26 16:48:09.015366,reached_pg,osd_op(client.45044503.0:262116 56.5 56.cd173545 (undecoded) ondisk+write+known_if_redirected e13809)
774.9295,2018-09-26 16:48:09.015354,reached_pg,osd_op(client.45044623.0:247460 56.5 56.cd173545 (undecoded) ondisk+write+known_if_redirected e13809)
```
