"""Compare declared dependencies/uv.lock with non-yanked stable PyPI releases.

Run with Python 3.13 in the development environment. Requires ``packaging``
(installed by the development tooling). This reads public metadata only: it
does not install packages, execute downloaded code, or load model weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]


def audit(root: Path, python: str) -> dict[str, Any]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    references: dict[str, list[dict[str, str]]] = defaultdict(list)
    areas = {"core": project["project"]["dependencies"]}
    areas.update(project["project"].get("optional-dependencies", {}))
    areas.update({f"group:{k}": v for k, v in project.get("dependency-groups", {}).items()})
    areas["build"] = project["build-system"]["requires"]
    for area, requirements in areas.items():
        for value in requirements:
            requirement = Requirement(value)
            if canonicalize_name(requirement.name) != canonicalize_name(project["project"]["name"]):
                references[canonicalize_name(requirement.name)].append(
                    {"area": area, "requirement": value}
                )
    locked: dict[str, list[str]] = defaultdict(list)
    for package in lock["package"]:
        locked[canonicalize_name(package["name"])].append(package["version"])
    host_tags = set(sys_tags())

    def fetch(name: str) -> dict[str, Any]:
        url = f"https://pypi.org/pypi/{name}/json"
        request = urllib.request.Request(url, headers={"User-Agent": "memspine-dependency-audit/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            metadata = json.load(response)
        releases: list[tuple[Version, list[dict[str, Any]]]] = []
        for raw_version, files in metadata["releases"].items():
            try:
                version = Version(raw_version)
            except InvalidVersion:
                continue
            if version.is_prerelease or version.is_devrelease:
                continue
            eligible = [
                file
                for file in files
                if not file.get("yanked")
                and SpecifierSet(file.get("requires_python") or "").contains(python)
            ]
            if eligible:
                releases.append((version, eligible))
        releases.sort(key=lambda entry: entry[0], reverse=True)
        if not releases:
            raise ValueError(f"{name}: no stable non-yanked release declares support for {python}")
        allowed = [
            entry
            for entry in releases
            if all(
                Requirement(ref["requirement"]).specifier.contains(entry[0])
                for ref in references[name]
            )
        ]
        if not allowed:
            raise ValueError(f"{name}: no release satisfies declared constraints on {python}")
        selected, files = allowed[0]
        wheels = [file["filename"] for file in files if file["filename"].endswith(".whl")]
        host_wheel = any(parse_wheel_filename(filename)[3] & host_tags for filename in wheels)
        return {
            "name": name,
            "references": references[name],
            "locked": locked.get(name, []),
            "latest_python313": str(releases[0][0]),
            "latest_within_constraints": str(selected),
            "lock_current": str(selected) in locked.get(name, []) if name != "hatchling" else None,
            "requires_python": sorted(
                {file.get("requires_python") or "unspecified" for file in files}
            ),
            "host_compatible_wheel": host_wheel,
            "wheel_filenames": wheels,
            "source": url,
        }

    with ThreadPoolExecutor(max_workers=10) as pool:
        packages = list(pool.map(fetch, sorted(references)))
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "target_python": python,
        "evidence_level": "Published Requires-Python and release-file metadata, not runtime proof",
        "host_wheel_scope": "Interpreter/platform running this audit only; not a cross-platform guarantee",
        "manifest_source": "pyproject.toml and uv.lock",
        "source_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock")
        },
        "packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.13.0")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "docs/provider_verification/dependency-audit.json"
    )
    parser.add_argument(
        "--check", action="store_true", help="Fail when a direct lock entry is outdated"
    )
    args = parser.parse_args()
    report = audit(ROOT, args.python)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    outdated = [p for p in report["packages"] if p["lock_current"] is False]
    for package in report["packages"]:
        print(
            f"{package['name']}: locked={','.join(package['locked']) or 'build-isolated'} "
            f"latest-allowed={package['latest_within_constraints']} "
            f"latest-Python-3.13={package['latest_python313']}"
        )
    print(
        f"Audited {len(report['packages'])} dependencies; {len(outdated)} outdated direct lock entries"
    )
    return 1 if args.check and outdated else 0


if __name__ == "__main__":
    raise SystemExit(main())
