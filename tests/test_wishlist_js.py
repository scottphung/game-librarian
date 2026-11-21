"""Runs the browser storage tests (tests/js: the wishlist and Daily Trivia) with Node, when Node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"
needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="Node.js is not installed; the browser storage tests need it")


def run_node_tests(name: str) -> None:
    result = subprocess.run(["node", "--test", str(JS_TESTS / name)], capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
def test_wishlist_storage_javascript():
    run_node_tests("wishlist.test.mjs")


@needs_node
def test_trivia_storage_javascript():
    run_node_tests("trivia.test.mjs")
