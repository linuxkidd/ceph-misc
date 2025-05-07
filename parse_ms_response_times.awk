#!/usr/bin/gawk -f

# 2025-05-06 17:47:50.467 7f7b6f6be700  1 -- 192.168.1.145:0/839786584 --> [v2:192.168.1.43:6800/152776,v1:192.168.1.43:6801/152776] -- osd_op(unknown.0.0:1744380 58.1b7 58:edded3f7:::.dir.default.272336018.162228.1:head [call rgw.bucket_list in=53b] snapc 0=[] ondisk+read+known_if_redirected e209855) v8 -- 0x55fc288a6f00 con 0x55fc176af180
# 2025-05-06 17:47:50.500 7f7bd0f81700  1 -- 192.168.1.145:0/839786584 <== osd.384 v2:192.168.1.43:6800/152776 99613 ==== osd_op_reply(1730308 .dir.default.272336018.162228.1 [call out=110b] v0'0 uv188594076 ondisk = 0) v8 ==== 175+0+110 (crc 0 0 0) 0x55fc16e13b80 con 0x55fc176af180

function epochtime(d,t) {
  gsub(/[-:]/," ",d)
  gsub(/[-:]/," ",t)
  MYTIME=mktime(d" "t)
  split(t,secs,".")
  millisecs=sprintf("0.%s",secs[2])
  MYTIME+=millisecs
  return MYTIME
}

BEGIN {
    print "start,end,duration,osd,pg,object"
}

/-->/ {
  split($12,a,":")
  gsub(/[\[\]]/,"",$8)
  split($8,b,",")
  op[b[1]" "a[5]] = epochtime($1,$2)
  start[b[1]" "a[5]] = $1" "$2
  pg[b[1]" "a[5]] = $11
}

/<==/ {
  if($9" "$13 in op) {
    delta = epochtime($1,$2) - op[$9" "$13]
    printf("%s,%s %s,%f,%s,%s,%s\n",start[$9" "$13],$1,$2,delta,$8,pg[$9" "$13],$13)
    delete op[$9" "$13]
    delete start[$9" "$13]
    delete pg[$9" "$13]
  }
}
