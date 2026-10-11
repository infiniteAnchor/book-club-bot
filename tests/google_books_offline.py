"""Offline Google Books stub shared by pytest and simulate.py."""
from unittest.mock import patch


class EmptyGoogleBooksResponse:
    def read(self):
        return b'{"items":[]}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def patch_google_books_offline():
    return patch("urllib.request.urlopen", return_value=EmptyGoogleBooksResponse())
