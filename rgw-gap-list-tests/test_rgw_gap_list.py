#!/usr/bin/env python3
"""
Offline tests of rgw-gap-list.py's parsing and classification helpers; they
need no cluster, and stub the rados module.

    python3 test_rgw_gap_list.py [path/to/rgw-gap-list.py]
"""
import importlib.util
import io
import json
import struct
import sys
import types
import unittest

rados = types.ModuleType("rados")
rados.Error = type("Error", (Exception,), {})
rados.ObjectNotFound = type("ObjectNotFound", (rados.Error,), {})
sys.modules["rados"] = rados

path = sys.argv.pop(1) if len(sys.argv) > 1 else "rgw-gap-list.py"
spec = importlib.util.spec_from_file_location("gaplist", path)
gl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl)

M = "1b14fa0e-7ecf-44dd-8b56-ba8e1473a227.4200.8"


def encode_refcount(refs, retired=(), version=2):
    def s(t):
        b = t.encode()
        return struct.pack("<I", len(b)) + b
    body = struct.pack("<I", len(refs)) + b"".join(s(t) + bytes([1]) for t in refs)
    if version >= 2:
        body += struct.pack("<I", len(retired)) + b"".join(s(t) for t in retired)
    return struct.pack("<BBI", version, 1, len(body)) + body


class ParseOid(unittest.TestCase):
    def test_kinds(self):
        up = "2~9wJL7tK7u4D4KqIaxd8eXjZUQdK38RW"
        cases = {
            f"{M}_obj": ("head", None, None),
            f"{M}___shadow_x": ("head", None, None),              # a key named _shadow_x
            f"{M}__:Y8tL7MyndszuwTxg4Itgx6eICC1bm9p_gone": ("head", None, None),
            f"{M}__shadow_.QmeTJR66RPFocC7r5-Uu_Tn4Kpp4X_1": ("shadow", None, None),
            f"{M}__multipart_a.b.{up}.2": ("part", "a.b", up),
            f"{M}__shadow_a.b.{up}.2_1": ("mp_shadow", "a.b", up),
            f"{M}__multipart_a.b.{up}.meta": ("meta", "a.b", up),
            "no-underscore": ("other", None, None),
        }
        for oid, (kind, key, upload) in cases.items():
            marker, k, kk, u = gl.parse_oid(oid)
            self.assertEqual((k, kk, u), (kind, key, upload), oid)
            if kind != "other":
                self.assertEqual(marker, M)

    def test_keys(self):
        self.assertEqual(gl.split_key("obj[abc]"), ("obj", "abc"))
        self.assertEqual(gl.split_key("obj"), ("obj", ""))
        self.assertEqual(gl.key_oid("obj"), "obj")
        self.assertEqual(gl.key_oid("_obj"), "__obj")
        self.assertEqual(gl.key_oid("obj", "null"), "obj")
        self.assertEqual(gl.key_oid("obj", "abc"), "_:abc_obj")
        for rest in ("obj", "__obj", "_:abc_obj"):
            name, inst = gl.head_key(rest)
            self.assertEqual(gl.key_oid(name, inst), rest)

    def test_index_objects(self):
        self.assertEqual(gl.index_objects({"id": "x", "num_shards": 0}), [".dir.x"])
        self.assertEqual(gl.index_objects({"id": "x", "num_shards": 2}), [".dir.x.0", ".dir.x.1"])
        self.assertEqual(gl.index_objects({"id": "x", "num_shards": 2, "index_generation": 3}),
                         [".dir.x.3.0", ".dir.x.3.1"])


class Refcount(unittest.TestCase):
    def test_decode(self):
        refs, retired = gl.decode_refcount(encode_refcount(["", "copy\0"], ["old\0"]))
        self.assertEqual(refs, {"": True, "copy": True})
        self.assertEqual(retired, {"old"})
        refs, retired = gl.decode_refcount(encode_refcount(["a"], version=1))
        self.assertEqual((refs, retired), ({"a": True}, set()))

    def test_survives_gc(self):
        # no refcount attribute: GC of any tag frees the object
        self.assertFalse(gl.survives_gc(["t"], None))
        # a copy's reference keeps it after the source's tag is put
        self.assertTrue(gl.survives_gc(["src"], ({"": True, "copy": True}, set())))
        # both put: freed
        self.assertFalse(gl.survives_gc(["src", "copy"], ({"": True, "copy": True}, set())))
        # a retired tag is not put twice
        self.assertTrue(gl.survives_gc(["copy"], ({"": True}, {"copy"})))


class Json(unittest.TestCase):
    def test_stream(self):
        items = [{"a": i, "s": "x" * (i * 7)} for i in range(200)]
        text = json.dumps(items, indent=4)
        got = list(gl.iter_json_array(io.StringIO(text), chunk_size=64))
        self.assertEqual(got, items)
        self.assertEqual(list(gl.iter_json_array(io.StringIO("[]"))), [])
        self.assertEqual(list(gl.iter_json_array(io.StringIO(""))), [])

    def test_times(self):
        t = gl.parse_time("2026-09-26T23:12:18.123456Z")
        self.assertEqual(gl.iso(t), "2026-09-26T23:12:18Z")
        self.assertEqual(gl.parse_time("2026-09-26 23:12:18.1"), t)
        self.assertIsNone(gl.parse_time("garbage"))


class Causes(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        gl.findings = self.out
        gl.logger = types.SimpleNamespace(error=lambda *a: None)
        gl.cluster_majors = set()
        gl.fixed_prs = set()
        gl.fixed_since = None

    def last(self):
        return json.loads(self.out.getvalue().splitlines()[-1])

    def test_release_filter(self):
        causes = [gl.cause("abort-race", "high", None), gl.cause("ix-fail", "high", None),
                  gl.cause("delete-race", "medium", None), gl.cause("mp-meta-left", "low", None)]
        for majors, names in (({19}, ["abort-race", "mp-meta-left"]),
                              ({20}, ["delete-race", "mp-meta-left"]),
                              ({21}, ["ix-fail", "delete-race", "mp-meta-left"]),
                              (set(), ["abort-race", "ix-fail", "delete-race", "mp-meta-left"])):
            gl.cluster_majors = majors
            gl.emit("data_loss", "missing_data", "b", "k", causes=causes)
            self.assertEqual([c["cause"] for c in self.last()["causes"]], names, majors)

    def test_after_fix(self):
        gl.fixed_prs = {72103}
        gl.fixed_since = gl.parse_time("2026-09-01 00:00:00")
        when = gl.parse_time("2026-09-20 00:00:00")
        gl.emit("at_risk", "completed_upload_open", "b", "k", when=when,
                causes=[gl.cause("mp-meta-left", "high", None)])
        rec = self.last()
        self.assertTrue(rec["causes"][0]["fixed_here"])
        self.assertTrue(rec["after_fix"])
        gl.emit("at_risk", "completed_upload_open", "b", "k", when=gl.parse_time("2026-08-20 00:00:00"),
                causes=[gl.cause("mp-meta-left", "high", None)])
        self.assertNotIn("after_fix", self.last())


if __name__ == "__main__":
    unittest.main(verbosity=1)
