#!/usr/bin/env python3
"""
rbd_snap_prune.py - Reduce the snapshot count of an RBD image using only the
librados Python bindings (no librbd / rbd CLI), for images that have exceeded
the point where the rbd tooling works (~16384 snapshots).

For each snapshot chosen for removal (oldest snap IDs first):
  1. Skip it if it has clone v2 children (a snap_children_<snapid> key in the
     parent header, or a nonzero child_count in the snapshot record), is
     protected, is not a plain "user" snapshot, or cannot be decoded safely.
     Clone v1 children and other pools are NOT checked.
  2. Remove its omap metadata from rbd_header.<id> (snapshot_<snapid>) and
     delete its object map (rbd_object_map.<id>.<snapid>).
  3. Report the snap ID to the monitors as removed (selfmanaged_snap_remove)
     so the OSDs snap-trim the clones.

Metadata is removed BEFORE the monitor call. If the monitor call fails, you
are left with an orphaned snap ID, which can be retried later. The reverse
order could leave the header pointing at a snapshot RADOS has discarded.

DRY RUN BY DEFAULT. Pass --execute to make changes.
The image must not be in use: stop all clients first, and check with
`rados -p <pool> listwatchers rbd_header.<id>`.
"""

import argparse
import logging
import struct
import sys

try:
    import rados
except ImportError:
    sys.exit("python3-rados is required")

RBD_SNAP_HARD_LIMIT = 16384
SNAP_KEY_PREFIX = "snapshot_"
V2_CHILDREN_KEY_PREFIX = "snap_children_"
OMAP_PAGE = 1000
OMAP_DELETE_BATCH = 256

SNAP_NS_TYPES = {0: "user", 1: "group", 2: "trash", 3: "mirror"}

log = logging.getLogger("rbd_snap_prune")


# --------------------------------------------------------------------------
# Decoding helpers (Ceph's little-endian encoding)
# --------------------------------------------------------------------------
class Decoder:
    def __init__(self, buf, off=0, end=None):
        self.buf = buf
        self.off = off
        self.end = len(buf) if end is None else end

    def remaining(self):
        return self.end - self.off

    def _take(self, n):
        if self.off + n > self.end:
            raise ValueError("buffer underrun")
        b = self.buf[self.off:self.off + n]
        self.off += n
        return b

    def u8(self):
        return self._take(1)[0]

    def u32(self):
        return struct.unpack("<I", self._take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self._take(8))[0]

    def string(self):
        return self._take(self.u32()).decode("utf-8", "replace")

    def envelope(self):
        """ENCODE_START: u8 version, u8 compat, u32 length. Returns (v, compat, end)."""
        v = self.u8()
        compat = self.u8()
        length = self.u32()
        end = self.off + length
        if end > self.end:
            raise ValueError(f"envelope overruns buffer (v={v} compat={compat} "
                             f"len={length} avail={self.end - self.off})")
        return v, compat, end

    def skip_envelope(self):
        _, _, end = self.envelope()
        self.off = end


def decode_snap(raw):
    """
    Decode the parts of cls_rbd_snap we need, mirroring cls_rbd_snap::decode
    (cls/rbd/cls_rbd.h):
      DECODE_START(8)
        id u64, name str, image_size u64
        features u64 (unused)                      [compat < 8]
        parent (cls_rbd_parent, own envelope)      [v >= 2 && compat < 8]
        protection_status u8                       [v >= 3]
        flags u64                                  [v >= 4]
        snapshot_namespace (own envelope)          [v >= 5]
        timestamp utime_t (8 bytes)                [v >= 6]
        child_count u64                            [v >= 7]
        parent_overlap optional<u64>               [v >= 8]
    Nautilus+ OSDs write compat=8, which omits features and parent.
    """
    d = Decoder(raw)
    v, compat, end = d.envelope()
    d.end = end
    info = {"struct_v": v, "compat": compat, "id": d.u64(), "name": d.string(),
            "protection": 0, "ns_type": 0, "child_count": 0}
    d.u64()  # image_size
    if compat < 8:
        d.u64()  # legacy features
        if v >= 2:
            d.skip_envelope()  # parent
    if v >= 3 and d.remaining():
        info["protection"] = d.u8()
    if v >= 4 and d.remaining():
        d.u64()  # flags
    if v >= 5 and d.remaining():
        _, _, ns_end = d.envelope()
        info["ns_type"] = d.u32()
        d.off = ns_end
    if v >= 6 and d.remaining():
        d._take(8)  # timestamp
    if v >= 7 and d.remaining() >= 4:
        info["child_count"] = d.u32()
    return info


# --------------------------------------------------------------------------
# RADOS helpers
# --------------------------------------------------------------------------
def omap_iter(ioctx, oid, prefix=""):
    """Yield (key, value) for all omap entries with the given prefix, paged."""
    start_after = ""
    while True:
        with rados.ReadOpCtx() as op:
            it, _ = ioctx.get_omap_vals(op, start_after, prefix, OMAP_PAGE)
            ioctx.operate_read_op(op, oid)
            page = list(it)
        if not page:
            return
        for k, v in page:
            yield k, v
        if len(page) < OMAP_PAGE:
            return
        start_after = page[-1][0]


def omap_get(ioctx, oid, key):
    with rados.ReadOpCtx() as op:
        it, _ = ioctx.get_omap_vals_by_keys(op, (key,))
        ioctx.operate_read_op(op, oid)
        return dict(it).get(key)


def resolve_image_id(ioctx, name):
    raw = omap_get(ioctx, "rbd_directory", "name_" + name)
    if raw is None:
        raise SystemExit(f"image '{name}' not found in rbd_directory "
                         "(v1 images and trashed images are not supported)")
    return Decoder(raw).string()


def find_children(header_omap):
    """
    Clone v2 only: return {snap_id: [reason, ...]} from the parent's own header.
    A snapshot with v2 children has a snap_children_<snapid hex> omap key whose
    value is the set of child specs (u32 count + entries). The per-snapshot
    child_count is checked separately in main().
    """
    children = {}
    for k, v in header_omap.items():
        if not k.startswith(V2_CHILDREN_KEY_PREFIX):
            continue
        try:
            sid = int(k[len(V2_CHILDREN_KEY_PREFIX):], 16)
        except ValueError:
            log.warning("unparseable children key %s - review manually", k)
            continue
        n = struct.unpack_from("<I", v)[0] if v and len(v) >= 4 else None
        children.setdefault(sid, []).append(
            f"v2 key {k}" + (f" ({n} children)" if n is not None else ""))
    return children


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", required=True, help="pool containing the image")
    ap.add_argument("--image", required=True, help="RBD image name")
    ap.add_argument("--threshold", type=int, default=RBD_SNAP_HARD_LIMIT - 4,
                    help="target snapshot count, must be < 16384 "
                         f"(default {RBD_SNAP_HARD_LIMIT - 4})")
    ap.add_argument("--namespace", default="", help="RADOS namespace of the image")
    ap.add_argument("--conf", default="/etc/ceph/ceph.conf")
    ap.add_argument("--execute", action="store_true",
                    help="actually make changes (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not 0 <= a.threshold < RBD_SNAP_HARD_LIMIT:
        sys.exit(f"--threshold must be between 0 and {RBD_SNAP_HARD_LIMIT - 1}")

    cluster = rados.Rados(conffile=a.conf)
    cluster.connect()
    io = cluster.open_ioctx(a.pool)
    io.set_namespace(a.namespace)
    if a.execute and not hasattr(io, "remove_self_managed_snap"):
        sys.exit("this python-rados has no remove_self_managed_snap (needs Octopus or later)")

    image_id = resolve_image_id(io, a.image)
    header = f"rbd_header.{image_id}"
    log.info("image %s/%s id=%s header=%s", a.pool, a.image, image_id, header)

    # 1. List all snapshots from the header omap
    header_omap, snaps = {}, []
    for k, v in omap_iter(io, header):
        if k.startswith(V2_CHILDREN_KEY_PREFIX):
            header_omap[k] = v
        if not k.startswith(SNAP_KEY_PREFIX):
            continue
        try:
            sid = int(k[len(SNAP_KEY_PREFIX):], 16)
        except ValueError:
            log.warning("unexpected key %s - ignored", k)
            continue
        try:
            info = decode_snap(v)
        except Exception as e:  # noqa: BLE001
            info = {"decode_error": str(e)}
        snaps.append((sid, k, info))
    snaps.sort()
    total = len(snaps)
    to_remove = total - a.threshold
    log.info("found %d snapshots; threshold %d -> need to remove %d",
             total, a.threshold, max(to_remove, 0))
    if to_remove <= 0:
        log.info("nothing to do")
        return

    # 2. Check children
    children = find_children(header_omap)
    log.info("%d snapshots have clone v2 children (checked: %s<snapid> keys and "
             "child_count)", len(children) + sum(1 for s, _, i in snaps if
             s not in children and i.get("child_count")), V2_CHILDREN_KEY_PREFIX)

    selected = []
    for sid, key, info in snaps:
        if len(selected) >= to_remove:
            break
        if "decode_error" in info:
            log.warning("snapid %d (0x%x) skipped: cannot decode (%s)",
                        sid, sid, info["decode_error"])
            continue
        if sid in children or info["child_count"]:
            why = children.get(sid, [f"child_count={info['child_count']}"])
            log.info("snapid %d (0x%x) '%s' skipped due to children: %s",
                     sid, sid, info["name"], "; ".join(why))
            continue
        if info["protection"] != 0:
            log.info("snapid %d (0x%x) '%s' skipped: protected (status %d)",
                     sid, sid, info["name"], info["protection"])
            continue
        if info["ns_type"] != 0:
            log.info("snapid %d (0x%x) '%s' skipped: %s-namespace snapshot",
                     sid, sid, info["name"],
                     SNAP_NS_TYPES.get(info["ns_type"], info["ns_type"]))
            continue
        selected.append((sid, key, info))

    log.info("%d snapshots selected for removal", len(selected))
    if len(selected) < to_remove:
        log.warning("only %d eligible; the count will be %d, above the threshold",
                    len(selected), total - len(selected))
    if not selected:
        return

    if not a.execute:
        for sid, _, info in selected[:20]:
            log.info("[dry-run] would remove snapid %d (0x%x) '%s'", sid, sid, info["name"])
        if len(selected) > 20:
            log.info("[dry-run] ... and %d more", len(selected) - 20)
        log.info("dry run complete; rerun with --execute to apply")
        return

    if not a.yes:
        ans = input(f"Remove {len(selected)} snapshots from {a.pool}/{a.image}? "
                    "Make sure the image is NOT in use. Type 'yes': ")
        if ans.strip() != "yes":
            sys.exit("aborted")

    # 3. Remove omap metadata + object maps, then retire the snap ids at the monitors
    removed = failed_mon = 0
    for i in range(0, len(selected), OMAP_DELETE_BATCH):
        batch = selected[i:i + OMAP_DELETE_BATCH]
        with rados.WriteOpCtx() as op:
            io.remove_omap_keys(op, tuple(k for _, k, _ in batch))
            io.operate_write_op(op, header)
        for sid, _, info in batch:
            try:
                io.remove_object(f"rbd_object_map.{image_id}.{sid:016x}")
            except rados.ObjectNotFound:
                pass
            try:
                io.remove_self_managed_snap(sid)
                removed += 1
                log.info("removed snapid %d (0x%x) '%s'", sid, sid, info["name"])
            except rados.Error as e:
                failed_mon += 1
                log.error("snapid %d (0x%x): metadata removed but the monitor call failed "
                          "(%s) - retry with remove_self_managed_snap(%d)", sid, sid, e, sid)

    log.info("done: %d removed, %d monitor failures, %d snapshots remain",
             removed, failed_mon, total - removed - failed_mon)

    print("\nMonitor snap trimming progress with:")
    print(f"  watch -n 10 \"ceph osd pool ls detail -f json | "
          f"jq '.[] | select(.pool_name==\\\"{a.pool}\\\") | .removed_snaps_queue'\"")
    print("  watch -n 10 'ceph pg ls snaptrim snaptrim_wait | wc -l'   # PGs still trimming")
    print("Trimming is finished when removed_snaps_queue is empty and no PGs are in "
          "snaptrim/snaptrim_wait.")
    print("Throttle if needed: ceph config set osd osd_snap_trim_sleep 1")

    io.close()
    cluster.shutdown()


if __name__ == "__main__":
    main()
