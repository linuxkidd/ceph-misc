#!/usr/bin/env python3

"""
By: Michael J. Kidd (linuxkidd)
Last Revision: 2026-10-23
Version: 1.0

Fetches, decompiles and modifies the crush map to scale by the
provided scale factor.

Usage:
./scale_crush_map.py <scale_factor>

Where <scale_factor> is a decimal value to scale by.

"""

import subprocess
import re
import sys

def scale_crushmap_cli(factor=0.1):
    try:
        # 1. Fetch current CRUSH map binary via CLI
        print("Fetching CRUSH map from cluster...")
        subprocess.run(['ceph', 'osd', 'getcrushmap', '-o', 'map.bin'], check=True)

        # 2. Decompile to plaintext
        print("Decompiling CRUSH map...")
        subprocess.run(['crushtool', '-d', 'map.bin', '-o', 'map.txt'], check=True)

        # 3. Parse and scale all bucket item weights bottom-up
        print(f"Scaling OSD weights by a factor of {factor} and reconstructing bucket sums...")
        
        calculated_weights = {}
        current_bucket = None
        
        with open('map.txt', 'r') as infile, open('map_scaled.txt', 'w') as outfile:
            for line in infile:
                # Check if we are entering a bucket (e.g., "host node-1 {")
                # We specifically exclude "rule" blocks which also use braces
                bucket_start_match = re.search(r'^([a-zA-Z0-9_.-]+)\s+([a-zA-Z0-9_.-]+)\s*\{', line)
                if bucket_start_match and bucket_start_match.group(1) != 'rule':
                    current_bucket = bucket_start_match.group(2)
                    calculated_weights[current_bucket] = 0.0
                    outfile.write(line)
                    continue
                    
                # Check if we are exiting the current bucket
                if current_bucket and re.search(r'^\s*\}', line):
                    current_bucket = None
                    outfile.write(line)
                    continue

                # Strip out the "# weight <val>" comment inside buckets. 
                # Leaving the old negative comments will cause confusion during your manual review.
                # When you eventually recompile, crushtool will automatically generate correct ones.
                if current_bucket and re.search(r'^\s*#\s*weight\s+[-]?\d+\.\d+', line):
                    continue

                # Check for items inside a bucket
                if current_bucket:
                    # Matches items: "    item osd.0 weight 18.453" or "    item node-1 weight -6144.000"
                    item_match = re.search(r'^(\s+item\s+)([a-zA-Z0-9_.-]+)(\s+weight\s+)([-]?\d+\.\d+)(.*)$', line)
                    if item_match:
                        prefix = item_match.group(1)
                        item_name = item_match.group(2)
                        midfix = item_match.group(3)
                        old_weight_str = item_match.group(4)
                        suffix = item_match.group(5)
                        
                        # If the item is a known bucket, use its recalculated sum!
                        # If the item is NOT in our dictionary, it is a leaf device (OSD).
                        if item_name in calculated_weights:
                            new_weight = calculated_weights[item_name]
                        else:
                            new_weight = float(old_weight_str) * factor
                        
                        # Add this item's true scaled weight to the parent bucket's running total
                        calculated_weights[current_bucket] += new_weight
                        
                        outfile.write(f"{prefix}{item_name}{midfix}{new_weight:.5f}{suffix}\n")
                        continue
                
                # Write any unmatched lines (tunables, devices, types, rules) as-is
                outfile.write(line)

        print("Scaling complete. The reconstructed map is ready for manual review at 'map_scaled.txt'.")

    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}")
        sys.exit(1)

def usage():
    print(f"""Usage: {sys.argv[0]} <scale_factor>

Where:
    <scale_factor> is a decimal value to scale by.

Example:
    {sys.argv[0]} 0.1

This will scale the weight values down to 10% of their original value.

""")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        usage()
        sys.exit()

    try:
        if float(sys.argv[1]) >= 1.0:
            print(f"\n\nThe provided argument ( {sys.argv[1]} ) does not appear to be a floating point number < 1.\n\n")
            usage()
            sys.exit(1)            
        scale_crushmap_cli(factor=float(sys.argv[1]))
    except ValueError:
        print(f"\n\nThe provided argument ( {sys.argv[1]} ) does not appear to be a floating point number < 1.\n\n")
        usage()
        sys.exit(1)