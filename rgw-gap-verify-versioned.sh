#!/bin/bash
#
# When using the rgw-gap-list.py script, it has been noted that deleted 
# versioned objects have been incorrectly identified as gaps.  This script
# can be used to check for these deleted versioned objects and will only
# output any remaining gaps.
# 
# Usage:
# cat gap-lsit-reults.txt | grep '\] MISSING ' | ./rgw-gap-verify-versioned.sh
#
# Results:
# - Any versioned object that does not have the 'delete' marker set will output
#   the original line as a real match
# - Any object that is not versioned will have the original line output, but
#   with a prefix of "Not Versioned Instance: " -- these are also indicative
#   of a real match
# - Any versioned object that does have the 'delete' marker set will be omitted
#   from the output.
#

while IFS= read -r line; do
    bucket=$(echo $line | sed -e 's/^s3:..//' -e 's/\/.*$//')
    object=$(echo $line | sed -e 's/^s3:..[^\/]*\///' -e 's/\[[a-zA-Z0-9]*\] MISSING .*$//')
    instance=$(echo $line | sed -e 's/^s3:..[^\[]*\[//' -e 's/\] MISSING .*$//')
    flags=$(radosgw-admin bi list --bucket="${bucket}" --object="${object}" | jq --arg O "{$object}" --arg I "${instance}" -r ' .[] | select(.entry.name == $O) | select (.entry.instance == $I ) | select( .type == "plain") | .entry.flags')
    if [ -z "$flags" ]; then 
        echo "Not Versioned Instance: ${line}"
    else
        if [ $(echo $(( $flags & 4 ))) -ne 4 ]; then
            echo $line
        fi
    fi
done
