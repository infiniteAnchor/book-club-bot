"""Keep pytest offline and skip durable SQLite fsyncs."""
import os

import pytest

from google_books_offline import patch_google_books_offline

os.environ.setdefault("BOOKCLUB_TEST_FAST_SQLITE", "1")


@pytest.fixture(autouse=True)
def _offline_google_books():
    with patch_google_books_offline():
        yield
