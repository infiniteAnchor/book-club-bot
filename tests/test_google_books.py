"""Parser coverage for lookup_google_books using fake HTTP payloads."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from actions import lookup_google_books


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _volume(title, authors=None, page_count=None, categories=None):
    info = {"title": title}
    if authors is not None:
        info["authors"] = authors
    if page_count is not None:
        info["pageCount"] = page_count
    if categories is not None:
        info["categories"] = categories
    return {"volumeInfo": info}


class GoogleBooksLookupTests(unittest.TestCase):
    def _lookup(self, payload, title="Dune", author=None):
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(payload),
        ) as urlopen:
            result = lookup_google_books(title, author)
        return result, urlopen

    def test_extracts_author_page_count_and_genre(self):
        payload = {
            "items": [
                _volume(
                    "Dune",
                    authors=["Frank Herbert"],
                    page_count=412,
                    categories=["Fiction / Science Fiction"],
                )
            ]
        }
        result, urlopen = self._lookup(payload)
        self.assertEqual(
            result,
            {
                "author": "Frank Herbert",
                "page_count": 412,
                "genre": "Fiction / Science Fiction",
            },
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)

    def test_skips_non_matching_titles_then_matches_substring(self):
        payload = {
            "items": [
                _volume("Hyperion", authors=["Dan Simmons"], page_count=200),
                _volume("Dune Messiah", authors=["Frank Herbert"], page_count=256),
            ]
        }
        result, _urlopen = self._lookup(payload, title="Dune")
        self.assertEqual(result["author"], "Frank Herbert")
        self.assertEqual(result["page_count"], 256)

    def test_ignores_out_of_range_page_counts(self):
        payload = {
            "items": [
                _volume("Dune", authors=["Frank Herbert"], page_count=12),
            ]
        }
        result, _urlopen = self._lookup(payload)
        self.assertEqual(result, {"author": "Frank Herbert"})

        payload = {
            "items": [
                _volume("Dune", authors=["Frank Herbert"], page_count=9000),
            ]
        }
        result, _urlopen = self._lookup(payload)
        self.assertEqual(result, {"author": "Frank Herbert"})

    def test_empty_items_and_http_errors_return_empty(self):
        result, _urlopen = self._lookup({"items": []})
        self.assertEqual(result, {})

        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertEqual(lookup_google_books("Dune"), {})

    def test_query_includes_author_and_optional_api_key(self):
        with mock.patch("config.GOOGLE_BOOKS_API_KEY", "secret-key"):
            result, urlopen = self._lookup({"items": []}, title="Dune", author="Frank Herbert")
        self.assertEqual(result, {})
        request = urlopen.call_args.args[0]
        url = request.full_url
        self.assertIn("intitle:Dune", url)
        self.assertIn("inauthor:Frank%20Herbert", url)
        self.assertIn("key=secret-key", url)
