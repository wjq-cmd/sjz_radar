"""Compatibility and lifecycle checks using the existing V4 plaintext fixture."""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Parser"))
import gateway_parser as gp


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = [json.loads(line) for line in cls.source.read_text(encoding="utf-8-sig").splitlines()]
        cls.packets = [e for e in cls.events if e.get("type") == "udp_plaintext"]
        cls.reference = json.loads(cls.reference_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-v5-parser-")
        self.engine = gp.Engine(Path(self.temp.name) / "semantic", replay=True)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def feed(self, batch=256):
        for i, event in enumerate(self.events):
            self.engine.accept(event)
            if (i + 1) % batch == 0:
                self.engine.flush()
        self.engine.flush()

    def assert_compatible(self):
        state = self.engine.last_state
        self.assertEqual(self.engine.stats["plaintextPackets"], 3854)
        self.assertEqual(state["self"], self.reference["self"])
        self.assertEqual(state["self_name"], self.reference["self_name"])
        self.assertEqual(state["self_hp"], self.reference["self_hp"])
        # Compare user-visible entity fields against the untouched ER reference.
        def project(s):
            return {e["key"]: {k: e.get(k) for k in ("name", "kind", "world", "hp", "hero", "team", "curr_weapon_id")}
                    for e in s["entities"]}
        self.assertEqual(project(state), project(self.reference))
        flow = next(iter(self.engine.flows.values()))
        self.assertEqual(flow.loader.pose_samples, 927)
        self.assertEqual(flow.loader.n_lb, 8329)
        self.assertEqual(flow.loader.packet_errors, 0)
        self.assertEqual(flow.view.decode_error_count(), 0)
        self.assertEqual(sum(len(t) for t in flow.view.slots.values()), 4733)
        self.assertEqual(Counter(e["kind"] for e in state["entities"]), {"mate": 2, "player": 9, "ai": 17, "loot": 2})

    def test_reference_semantics(self):
        self.feed()
        self.assert_compatible()

    def test_compact_identity_cache_and_reuse(self):
        self.engine.close()
        output = Path(self.temp.name) / "compact"
        self.engine = gp.Engine(output, replay=True, cache_only=True)
        self.feed(17)
        self.engine.flush(force=True)
        self.assert_compatible()
        cache = json.loads((output / "identity-cache.json").read_text(encoding="utf-8"))
        flow = next(iter(self.engine.flows.values()))
        saved = cache["flows"][0]
        self.assertEqual(len(saved["players"]), len(flow.view.players))
        self.assertEqual(len(saved["characters"]), len(flow.view.links))
        self.assertEqual(saved["slotClaims"], {str(k): v for k, v in flow.view.claim.items()})
        self.assertTrue(any(v.get("name") for v in saved["players"].values()))
        self.assertTrue(any("move_handle" in v for v in saved["characters"].values()))
        self.assertFalse(any("loc" in v or "move_handle_history" in v for v in saved["characters"].values()))
        before = {p.name: p.stat().st_size for p in output.iterdir() if p.is_file()}
        identity_mtime = (output / "identity-cache.json").stat().st_mtime_ns
        for _ in range(100):
            self.engine.flush(force=True)
        after = {p.name: p.stat().st_size for p in output.iterdir() if p.is_file()}
        self.assertEqual(before.keys(), after.keys())
        # Per-entity sample ages and status rates now change even with a stationary view.
        self.assertLess(sum(after.values()), sum(before.values()) + 8192)
        self.assertEqual(identity_mtime, (output / "identity-cache.json").stat().st_mtime_ns)
        self.assertFalse((output / "states.jsonl").exists())
        self.assertFalse((output / "flows").exists())
        self.engine.close()
        self.engine = gp.Engine(output, replay=True, cache_only=True)
        self.assertEqual(json.loads((output / "identity-cache.json").read_text())["flows"], [])
        self.assertFalse((output / "snapshot-final.json").exists())
        for _ in range(150):
            self.engine.error("x" * 8192)
        self.assertLess((output / "parser-errors.jsonl").stat().st_size, 1024 * 1024 + 8300)

    def test_small_batches_and_reassembly(self):
        self.feed(17)
        self.assert_compatible()

    def test_session_reset_removes_old_entities(self):
        self.feed()
        self.engine.accept({"type": "session_started", "sequence": 1, "data": {}})
        self.engine.flush()
        self.assertFalse(self.engine.last_state["entities"])
        self.assertIsNone(self.engine.last_state["self"])
        self.assertFalse(self.engine.flows)

    def test_epoch_reset_and_late_old_packets(self):
        self.feed()
        packet = copy.deepcopy(self.packets[0])
        packet["sequence"] = self.engine.sequence + 1
        packet["data"]["epoch"] = 2
        self.engine.accept(packet)
        self.engine.flush()
        self.assertFalse(self.engine.last_state["entities"])
        self.assertEqual(len(self.engine.flows), 1)
        self.assertFalse(next(iter(self.engine.flows.values())).loader.hs.ok)
        packet["sequence"] += 1
        packet["data"]["epoch"] = 1
        self.engine.accept(packet)
        self.assertEqual(self.engine.stats["retiredEpochPackets"], 1)

    def test_distinct_endpoints_do_not_share_state(self):
        self.feed()
        for packet in self.packets[:12]:
            event = copy.deepcopy(packet)
            event["sequence"] = self.engine.sequence + 1
            event["data"]["remote"] = "127.0.0.1:30001"
            self.engine.accept(event)
        self.engine.flush()
        self.assertEqual(len(self.engine.flows), 2)
        flows = list(self.engine.flows.values())
        self.assertIsNot(flows[0].loader.reasm, flows[1].loader.reasm)
        self.assertIsNot(flows[0].view.players, flows[1].view.players)
        self.assertEqual(len(flows[0].view.players), 17)
        self.assertLess(len(flows[1].view.players), 17)

    def test_input_gap_invalidates_state(self):
        self.feed()
        self.engine.accept({"sequence": self.engine.sequence + 2, "type": "session_status", "data": {}})
        self.engine.flush()
        self.assertEqual(self.engine.stats["eventGaps"], 1)
        self.assertFalse(self.engine.last_state["entities"])

    def test_duplicate_is_not_decoded_again(self):
        self.feed()
        self.engine.accept(self.events[-1])
        self.assertEqual(self.engine.stats["duplicateOrOutOfOrder"], 1)
        self.assertEqual(self.engine.stats["plaintextPackets"], 3854)

    def test_bad_payload_and_parser_fault(self):
        packet = copy.deepcopy(self.packets[0])
        packet["data"]["payloadBase64"] = "!invalid!"
        with self.assertRaises(ValueError):
            self.engine.accept(packet)
        self.engine.accept({"type": "parser_fault", "data": {"error": "fixture queue fault"}})
        self.assertEqual(self.engine.last_state["status"], "error")
        self.assertFalse(self.engine.last_state["entities"])

    def test_live_idle_and_stop_clear_entities(self):
        self.feed()
        self.engine.replay = False
        self.engine.ended = False
        flow = next(iter(self.engine.flows.values()))
        flow.last_received -= 20
        self.engine.flush()
        self.assertEqual(self.engine.last_state["status"], "idle")
        self.assertFalse(self.engine.last_state["entities"])
        self.engine.ended = True
        self.engine.flush()
        self.assertEqual(self.engine.last_state["status"], "stopped")
        self.assertTrue(self.engine.last_nonempty["entities"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    args, remaining = ap.parse_known_args()
    ParserTests.source, ParserTests.reference_path = args.source, args.reference
    unittest.main(argv=[sys.argv[0]] + remaining)
