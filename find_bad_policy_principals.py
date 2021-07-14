#!/usr/bin/env python3

'''
Usage:
  1: Make the script executable:
     # chmod 755 find_bad_policy_principals.py

  2: Run the script:
     # ./find_bad_policy_principals.py -n localhost -p 8080

  3: For full usage details, run with -h or --help.

Assumptions:
 * The 'radosgw-admin' command runs normally with the proper keyring present
 * If extra parameters are needed for 'radosgw-admin' to execute, add them to
   the CEPH_ARGS environment variable BEFORE executing the script.

Example:
   # export CEPH_ARGS='--cluster prod'
   # ./find_bad_policy_principals.py -n localhost

'''

import argparse,boto,datetime,json,os,re,subprocess,sys
import boto.s3.connection

cephargs=os.environ.get('CEPH_ARGS')

def log_output(message):
    print('{0:%Y-%m-%d %H:%M:%S} {1:s}'.format(datetime.datetime.now(),message))

def test_user(access_key,secret_key):
    global bucketcount,bucketmod,badpolicy,s3host,s3port,s3secure,user,usercount
    conn = boto.connect_s3(
            aws_access_key_id = access_key,
            aws_secret_access_key = secret_key,
            host = s3host,
            port = s3port,
            is_secure = s3secure,
            calling_format = boto.s3.connection.OrdinaryCallingFormat(),
        )

    for bucket in conn.get_all_buckets():
        bucketcount+=1
        if bucketcount%1000==0:
            log_output("Progress report: {0:d} users, {1:d} buckets, {2:d} bad policies found.  Current user: {3:s}, bucket: {4:s}".format(usercount,bucketcount,badpolicy,user,bucket.name))
        try:
            mypolicy=json.loads(bucket.get_policy().decode('utf8'))
        except:
            pass
        else:
            for st in mypolicy["Statement"]:
                if not re.match('^arn:aws:iam:.*:.*:user\/.*$',st["Principal"]["AWS"]):
                    log_output("Bad policy principal detected on bucket {0:s}: {1:s}".format(bucket.name,json.dumps(st)))
                    badpolicy+=1


'''
Main
'''

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Script to identify improperly formatted Policy Principals.  Please see\nhttps://tracker.ceph.com/issues/46078 for details on the bug this is to\nhelp address.',
        epilog="NOTE:\n  If additional arguments are required for radosgw-admin to execute,\n  please set them in CEPH_ARGS environment variable BEFORE runnint this\n  script.\n\nExample:\n\n  # export CEPH_ARGS='--cluster prod'\n  # {0:s}".format(sys.argv[0]))
    parser.add_argument("-b", "--buckets",  default = 1000, type=int, help="Progress report every X buckets ( default: 1000 )")
    parser.add_argument("-n", "--hostname", required = True,          help="S3 Host Name or IP ( required parameter )")
    parser.add_argument("-p", "--port",     default = 8080, type=int, help="S3 Port ( default: 8080 ")
    parser.add_argument("-s", "--secure",   action = 'store_true',    help="Set if using SSL/TLS ( default: not-set )")
    parser.add_argument("-u", "--users",    default = 1000, type=int, help="Progress report every X users ( default: 1000 )")
    args = parser.parse_args()

    s3host    = args.hostname
    s3port    = args.port
    s3secure  = args.secure
    bucketmod = args.buckets
    usermod   = args.users

    usercount=0
    bucketcount=0
    badpolicy=0
    
    log_output("Fetching user list from radosgw-admin...")
    command=['radosgw-admin', 'user', 'list']
    if cephargs!=None:
        command.append(cephargs)
    
    try:
        users=json.loads(subprocess.run(command, stdout=subprocess.PIPE).stdout)
    except:
        log_output("FAILED")
        quit()
    else:
        log_output("Done")
    
    for user in users:
        usercount+=1
        if usercount%usermod==0:
            log_output("Progress report: {0:d} users, {1:d} buckets, {2:d} bad policies found.  Current user: {3:s}".format(usercount,bucketcount,badpolicy,user))
    
        command=None
        command=['radosgw-admin', 'user', 'info', '--uid', users[0]]
        if cephargs!=None:
            command.append(cephargs)
    
        try:
            userinfo=json.loads(subprocess.run(command, stdout=subprocess.PIPE).stdout)
        except:
            log_output("FAIL: 'radosgw-admin user info --uid {0:s} failed.".format(user))
        else:
            test_user(userinfo['keys'][0]['access_key'],userinfo['keys'][0]['secret_key'])
    
    log_output("Processing done: {0:d} users, {1:d} buckets, {2:d} bad policies found.".format(usercount,bucketcount,badpolicy))
    
