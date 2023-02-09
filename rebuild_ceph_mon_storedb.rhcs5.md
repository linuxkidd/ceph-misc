# Notes/Ideas

## check status of the osds before we do anything .....

Check the status of each osd before hand
$ osd6_status=$(ssh rhel86-rhcs52-admin "systemctl status ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service")

$? represents the return of the remote ssh command
echo $?
0

and $osd6_status represents the output of the command

$ echo $osd6_status
● ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service - Ceph osd.6 for 6660cb98-5153-11ed-a9da-525400adb33b Loaded: loaded (/etc/systemd/system/ceph-6660cb98-5153-11ed-a9da-525400adb33b@.service; enabled; vendor preset: disabled) Active: active (running) since Wed 2023-02-08 16:50:22 EST; 5min ago Process: 6016 ExecStopPost=/bin/rm -f /run/ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service-pid /run/ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service-cid (code=exited, status=0/SUCCESS) Process: 5941 ExecStopPost=/bin/bash /var/lib/ceph/6660cb98-5153-11ed-a9da-525400adb33b/osd.6/unit.poststop (code=exited, status=0/SUCCESS) Process: 5900 ExecStop=/bin/bash -c /bin/podman stop ceph-6660cb98-5153-11ed-a9da-525400adb33b-osd.6 ; bash /var/lib/ceph/6660cb98-5153-11ed-a9da-525400adb33b/osd.6/unit.stop (code=exited, status=0/SUCCESS) Process: 6020 ExecStart=/bin/bash /var/lib/ceph/6660cb98-5153-11ed-a9da-525400adb33b/osd.6/unit.run (code=exited, status=0/SUCCESS) Process: 6019 ExecStartPre=/bin/rm -f /run/ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service-pid /run/ceph-6660cb98-5153-11ed-a9da-525400adb33b@osd.6.service-cid (code=exited, status=0/SUCCESS) Main PID: 6180 (conmon) Tasks: 62 (limit: 17391) Memory: 104.8M CGroup:


## Additional troubleshooting ...
for some reason, randomly in my lab after several tests, started to end up
with an empty store.db directory ... also happened in Steve's QL cluster. Why??
