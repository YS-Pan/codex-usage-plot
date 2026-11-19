"""Run the rejected v4 candidate's synthetic regressions in a child process."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest


REPOSITORY = Path(__file__).resolve().parents[5]
RESEARCH = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法读取研究代码：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="仅运行 v4 研究候选的合成回归，不代表性能验收通过。")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args._worker:
        return subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--_worker"], cwd=REPOSITORY,
        ).returncode
    sys.path.insert(0, str(REPOSITORY))
    import codex_usage
    candidate = load_module("codex_usage.cache", RESEARCH / "cache.py")
    codex_usage.cache = candidate
    tests = load_module("cache_v4_migration_research", RESEARCH / "test_cache_migration.py")
    suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
