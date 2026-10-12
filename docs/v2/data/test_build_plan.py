"""Tests for the ownership checker and brief generator in build_plan.py (stdlib unittest)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_plan as bp  # noqa: E402


def row(i, deps=(), status="Not started"):
    return {"id": i, "title": f"t{i}", "depends_on": list(deps), "status": status, "kind": "move", "lane": "x", "gate": "g",
            "note": "", "decisions": [], "review": ["ER-01"], "phase": 1}


class Overlap(unittest.TestCase):
    def test_identical(self):
        self.assertTrue(bp.patterns_overlap("a/b.py", "a/b.py"))

    def test_glob_matches_literal(self):
        self.assertTrue(bp.patterns_overlap("a/*.py", "a/b.py"))
        self.assertTrue(bp.patterns_overlap("a/b.py", "a/**"))
        self.assertFalse(bp.patterns_overlap("a/*.py", "a/sub/b.py"))

    def test_common_existing_file(self):
        files = ["src/x/query_a.py"]
        self.assertTrue(bp.patterns_overlap("src/x/query_*", "src/x/*_a.py", files))
        self.assertFalse(bp.patterns_overlap("src/x/query_*", "src/x/*_a.py", []))

    def test_dir_glob_contains_future_files(self):
        self.assertTrue(bp.patterns_overlap("src/read/**", "src/read/legs/*.py"))
        self.assertTrue(bp.patterns_overlap("src/read/**", "src/*/registry.py"))
        self.assertFalse(bp.patterns_overlap("src/read/**", "src/compose/*.py"))
        self.assertFalse(bp.patterns_overlap("src/read/**", "src/reader.py"))


class Runnable(unittest.TestCase):
    def setUp(self):
        self.rows = [row("A"), row("B", ["A"]), row("C", ["B"]), row("D"), row("E")]
        self.cl = bp.transitive_deps(self.rows)
        self.lanes = {"serial": {"D", "E"}}

    def test_transitive(self):
        self.assertFalse(bp.concurrently_runnable("A", "C", self.cl, self.lanes))

    def test_same_lane(self):
        self.assertFalse(bp.concurrently_runnable("D", "E", self.cl, self.lanes))

    def test_independent(self):
        self.assertTrue(bp.concurrently_runnable("A", "D", self.cl, self.lanes))
        self.assertTrue(bp.concurrently_runnable("C", "E", self.cl, self.lanes))


class Collisions(unittest.TestCase):
    def setUp(self):
        self.rows = [row("A"), row("B"), row("C", ["A"]), row("D", status="Done")]
        self.lanes = {}

    def run_(self, owns, allowed=()):
        return bp.find_collisions(self.rows, owns, list(allowed), [], self.lanes)

    def test_collision_found(self):
        bad, _ = self.run_({"A": ["x/**"], "B": ["x/y.py"]})
        self.assertEqual([(a, b) for a, b, _ in bad], [("A", "B")])

    def test_dependent_pair_ok(self):
        bad, _ = self.run_({"A": ["x/y.py"], "C": ["x/y.py"]})
        self.assertEqual(bad, [])

    def test_done_excluded(self):
        bad, _ = self.run_({"A": ["x/y.py"], "D": ["x/y.py"]})
        self.assertEqual(bad, [])

    def test_allowed_shared(self):
        bad, shared = self.run_({"A": ["x/y.py"], "B": ["x/y.py"]}, [("B", "A", "one agent")])
        self.assertEqual(bad, [])
        self.assertEqual(len(shared), 1)

    def test_no_entry_is_skipped(self):
        bad, _ = self.run_({"A": ["x/y.py"]})
        self.assertEqual(bad, [])


class RealPlan(unittest.TestCase):
    def test_current_plan_has_no_collisions(self):
        rows, _ = bp._build_rows()
        bad, _ = bp.find_collisions(rows, bp.OWNS, bp.ALLOWED_SHARED, bp.repo_files())
        self.assertEqual(bad, [])

    def test_brief_renders(self):
        rows, titles = bp._build_rows()
        tpl = (bp.HERE.parent / "briefs" / "TEMPLATE.md").read_text(encoding="utf-8")
        text = bp.render_brief("X-03", rows, bp.OWNS, titles, bp.ALLOWED_SHARED, tpl)
        self.assertNotIn("{{", text)
        self.assertIn("`tests/golden/**`", text)

    def test_brief_forbids_concurrent_owner(self):
        rows, titles = bp._build_rows()
        tpl = "{{forbidden}}"
        text = bp.render_brief("X-03", rows, bp.OWNS, titles, bp.ALLOWED_SHARED, tpl)
        self.assertIn("P0-09", text)


if __name__ == "__main__":
    unittest.main()
