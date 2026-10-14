"""``python -m memspine_evals.inspect <command> ...``: step | assemble | checks | snapshot."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmds = {"step": "step", "assemble": "assemble", "checks": "checks", "snapshot": "snapshot"}
    if not argv or argv[0] not in cmds:
        print(__doc__)
        return 2
    import importlib

    mod = importlib.import_module(f"memspine_evals.inspect.{cmds[argv[0]]}")
    return int(mod.main(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
