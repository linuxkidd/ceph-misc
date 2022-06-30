#!/usr/bin/awk -f

BEGIN {

}

function histoevent(mykey,myevent,myfunc,myvalue) {
  EVENTHEADERS[myevent]=1
  if(myfunc=="sum")
    EVENTCOUNT[mykey][myevent]+=myvalue
  else if(myfunc=="set")
    EVENTCOUNT[mykey][myevent]=myvalue
  else if(myfunc=="inc")
    EVENTCOUNT[mykey][myevent]++
}


/^[a-zA-Z]/ {
  if($1 !~ /avg-cpu/ && $1 !~ /Linux/ && $1 !~ /Device/) {
    mykey=sprintf("%d",int($NF/10)*10)
    if($NF>100)
      mykey=sprintf("%d",int($NF/100)*100)
    histoevent(mykey,$1,"inc",1)
    histoevent("TOT",$1,"inc",1)
  }
}

END {
  n=asorti(EVENTHEADERS)
  printf("%Util")
  for (i=1;i<=n;i++) {
    printf(",%s",EVENTHEADERS[i])
  }
  print
  for (j=0;j<=100;j+=10) {
    printf("%d",j)
    for (i=1;i<=n;i++) {
      printf(",%0.2f",(EVENTCOUNT[j][EVENTHEADERS[i]]/EVENTCOUNT["TOT"][EVENTHEADERS[i]])*100)
    }
    print
  }
}

