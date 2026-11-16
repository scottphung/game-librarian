"""Runs the wishlist's JavaScript storage tests (tests/js) with Node, when Node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed; the wishlist JS tests need it")
def test_wishlist_storage_javascript():
    result = subprocess.run(["node", "--test", str(JS_TESTS / "wishlist.test.mjs")], capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
