"""``python -m memspine.schemas export <outdir>``."""

from __future__ import annotations

import argparse
import sys

from memspine.schemas.export import export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memspine.schemas")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write JSON Schema per model")
    exp.add_argument("outdir")
    args = parser.parse_args(argv)
    if args.command == "export":
        written = export(args.outdir)
        print(f"wrote {len(written)} files to {args.outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
