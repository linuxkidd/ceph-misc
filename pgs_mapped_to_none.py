#!/usr/bin/env python

# 2023-07-31T03:41:23.689+0000 7f89031af640  1 osd.2 pg_epoch: 2129 pg[11.79s9( v 2128'209499 (2126'207246,2128'209499] local-lis/les=1957/1958 n=28727 ec=1766/320 lis/c=1957/1562 les/c/f=1958/1566/0 sis=2129 pruub=8.995185852s) [10,NONE,14,27,9,6,19,13,4,11,18,22,12,2]/[9,6,19,13,NONE,NONE,NONE,NONE,12,2,20,28,4,11]p9(0) r=9 lpr=2129 pi=[1562,2129)/4 luod=0'0 crt=2128'209497 mlcod 0'0 active+remapped pruub 336562.500000000s@ mbc={}] start_peering_interval up [10,29,14,27,9,6,19,13,4,11,18,22,12,2] -> [10,2147483647,14,27,9,6,19,13,4,11,18,22,12,2], acting [9,6,19,13,2147483647,2147483647,2147483647,2147483647,12,2,20,28,4,11] -> [9,6,19,13,2147483647,2147483647,2147483647,2147483647,12,2,20,28,4,11], acting_primary 9(0) -> 9, up_primary 10(0) -> 10, role 9 -> 9, features acting 4540138320759226367 upacting 4540138320759226367


# attempt to map PG, OSD.id where OSD.id went to NONE.

import io,re,sys

up_acting_re = f'[\[\]]'

def osd2ary(osdString):
    return [ int(x) if x.isdigit() else None for x in re.sub(up_acting_re,"",re.sub(r'\].*$','',osdString)).split(',') ]

osdNone = (2<<30)-1

if len(sys.argv)==2:
    downSet = set( [ int(x) if x.isdigit() else x for x in sys.argv[1].split(",") ])
else:
    downSet = {}

for line in io.TextIOWrapper(sys.stdin.buffer, encoding='unicode_escape'):
    if re.match('^.* pg_epoch: .*NONE.*$',line):
        lparts = line.split(" ")
        pgid=""
        pg_epoch=0
        for i in range(0,len(lparts)):
            if lparts[i]=="pg_epoch:":
                pg_epoch = lparts[i+1]
            if re.match('^pg\[[0-9]+\.[0-9a-fs]+\($',lparts[i]):
                pgid=re.sub('\($','',re.sub('^pg\[','',lparts[i]))
            #if re.match('^\[([0-9,]|NONE)+\]/[([0-9,]|NONE)+\]$',lparts[i]):
            if lparts[i] in {"up","acting"} and re.match('^\[[0-9,]+\]$',lparts[i+1]) and lparts[i+2]=="->" and re.match('^\[[0-9,]+\],$',lparts[i+3]):
                # acting [9,6,19,13,2147483647,2147483647,2147483647,2147483647,12,2,20,28,4,11] -> [9,6,19,13,2147483647,2147483647,2147483647,2147483647,12,2,20,28,4,11],
                pre = osd2ary(lparts[i+1])
                post = osd2ary(lparts[i+3])
                #
                # up Pre:  [10, 29, 14, 27, 9, 6, 19, 13, 4, 11, 18, 22, 12, 2]
                # up Post: [10, 2147483647, 14, 27, 9, 6, 19, 13, 4, 11, 18, 22, 12, 2]
                # acting Pre:  [9, 6, 19, 13, 2147483647, 2147483647, 2147483647, 2147483647, 12, 2, 20, 28, 4, 11]
                # acting Post: [9, 6, 19, 13, 2147483647, 2147483647, 2147483647, 2147483647, 12, 2, 20, 28, 4, 11]
                #
                replaced=set([])
                for x in range(0,max(len(pre),len(post))):
                    try:
                        if pre[x] != post[x] and post[x]==osdNone:
                            replaced.add(pre[x])
                    except IndexError as e:
                        print(f"Pre ( {len(pre)} ) and Post ( {len(post)} ) are not the same length!")

                if len(replaced):
                    if len(downSet):
                        r = ', '.join([ str(x) for x in replaced.difference(downSet)])
                        if r:
                            print(f"{lparts[0]} {pgid} epoch {pg_epoch} {lparts[i]} replaced: {r}")
                    else:
                        r = ', '.join([ str(x) for x in replaced])
                        print(f"{lparts[0]} {pgid} epoch {pg_epoch} {lparts[i]} replaced: {r}")

