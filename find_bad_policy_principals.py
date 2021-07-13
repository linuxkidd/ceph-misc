#!/usr/bin/env python3

import boto,datetime,json,re,subprocess
import boto.s3.connection

s3host = '10.74.254.245'
s3port = 8080

def log_output(message):
    print('{0:%Y-%m-%d %H:%M:%S} {1:s}'.format(datetime.datetime.now(),message))

def test_user(access_key,secret_key):
    global user,usercount,bucketcount,badpolicy
    conn = boto.connect_s3(
            aws_access_key_id = access_key,
            aws_secret_access_key = secret_key,
            host = s3host,
            port = s3port,
            is_secure=False,
            calling_format = boto.s3.connection.OrdinaryCallingFormat(),
        )

    for bucket in conn.get_all_buckets():
        if bucket.name in buckets_processed:
            return
        else:
            bucketcount+=1
            if bucketcount%1000==0:
                log_output("Progress report: {0:d} users, {1:d} buckets, {2:d} bad policies found.  Current user: {3:s}, bucket: {4:s}".format(usercount,bucketcount,badpolicy,users[0],bucket.name))
            try:
                mypolicy=json.loads(bucket.get_policy().decode('utf8'))
            except:
                pass
            else:
                for st in mypolicy["Statement"]:
                    if not re.match('^arn:aws:iam:.*:.*:user\/.*$',st["Principal"]["AWS"]):
                        log_output("Bad policy principal detected on bucket {0:s}: {1:s}".format(bucket.name,json.dumps(st)))
                        badpolicy+=1


## Main

usercount=0
bucketcount=0
badpolicy=0

buckets_processed=[]

log_output("Fetching user list from radosgw-admin...")
try:
    users=json.loads(subprocess.run(['radosgw-admin', 'user','list'], stdout=subprocess.PIPE).stdout)
except:
    log_output("FAILED")
    quit()
else:
    log_output("Done")

for user in users:
    usercount+=1
    if usercount%1000==0:
        log_output("Progress report: {0:d} users, {1:d} buckets, {2:d} bad policies found.  Current user: {3:s}".format(usercount,bucketcount,badpolicy,users[0]))
    try:
        userinfo=json.loads(subprocess.run(['radosgw-admin', 'user','info','--uid',users[0]], stdout=subprocess.PIPE).stdout)
    except:
        log_output("FAILED")
    else:
        test_user(userinfo['keys'][0]['access_key'],userinfo['keys'][0]['secret_key'])

log_output("Processing done: {0:d} users, {1:d} buckets, {2:d} bad policies found.".format(usercount,bucketcount,badpolicy))
