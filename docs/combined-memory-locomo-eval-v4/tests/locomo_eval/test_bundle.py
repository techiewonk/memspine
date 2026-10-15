"""Offline transfer packaging checks; no memory backends or models."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from module.locomo_eval.__main__ import create_bundle, parser


class BundleTests(unittest.TestCase):
    def seed(self, root):
        names = [
            f"module/locomo_eval/{name}.py"
            for name in ("__init__", "__main__", "backend", "dataset", "metrics", "runner")
        ]
        names += [
            "tests/locomo_eval/test_combined_eval.py",
            "tests/locomo_eval/test_bundle.py",
            "docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html",
        ]
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        (root / ".env").write_text("secret", encoding="utf-8")
        (root / "module/locomo_eval/private.py").write_text("secret", encoding="utf-8")
        return names

    @patch("module.locomo_eval.__main__.git_revision", return_value="test-revision")
    def test_whitelist_hashes_and_no_overwrite(self, _revision):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = self.seed(root)
            output = root / "transfer.zip"
            create_bundle(output, root=root)
            original = output.read_bytes()
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    set(names) | {"LOCOMO_BUNDLE_MANIFEST.json", "LOCOMO_BUNDLE_README.txt"},
                )
                manifest = json.loads(archive.read("LOCOMO_BUNDLE_MANIFEST.json"))
                self.assertFalse(manifest["standalone"])
                for name, digest in manifest["files"].items():
                    self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
            with self.assertRaises(FileExistsError):
                create_bundle(output, root=root)
            self.assertEqual(original, output.read_bytes())

    def test_missing_html_leaves_no_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            (root / "docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html").unlink()
            output = root / "transfer.zip"
            with self.assertRaises(FileNotFoundError):
                create_bundle(output, root=root)
            self.assertFalse(output.exists())

    def test_cli_dataset_option(self):
        args = parser().parse_args(
            ["bundle", "--output", "transfer.zip", "--include-dataset", "data.json"]
        )
        self.assertEqual(args.include_dataset, Path("data.json"))

    @patch("module.locomo_eval.__main__.git_revision", return_value="test-revision")
    @patch("module.locomo_eval.__main__.load_dataset")
    def test_optional_dataset_is_validated_and_copied(self, validate, _revision):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            dataset = root / "chosen.json"
            dataset.write_bytes(b"[]")
            output = root / "transfer.zip"
            create_bundle(output, dataset, root=root)
            validate.assert_called_once_with(dataset)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("module/locomo_eval/data/locomo10.json"), b"[]")
            validate.side_effect = ValueError("invalid dataset")
            rejected = root / "rejected.zip"
            with self.assertRaises(ValueError):
                create_bundle(rejected, dataset, root=root)
            self.assertFalse(rejected.exists())


if __name__ == "__main__":
    unittest.main()
