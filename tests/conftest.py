# encoding:utf-8

"""Shared fixtures: a client whose page is a fake, and no browser anywhere.

Nothing in this suite starts Playwright, dials a port, or launches Edge. The
client's page thread is never started either -- tests call the page-thread
methods directly, on the test's own thread, which is exactly the one-thread
contract those methods assume.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from douyin4u import Douyin  # noqa: E402
from fakes import FakePage  # noqa: E402


@pytest.fixture
def client():
    """A client mid-life: attached to a healthy fake chat page, receiving.

    Marks stay in memory (no ``marks_path``), so a test never writes a file
    it did not ask for.
    """
    dy = Douyin()
    dy._page = FakePage()
    dy.enable_receiving_msg()
    return dy
