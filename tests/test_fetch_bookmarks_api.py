import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPTS_PATH = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_PATH))
SPEC = importlib.util.spec_from_file_location(
    "fetch_bookmarks_api",
    SCRIPTS_PATH / "fetch_bookmarks_api.py",
)
fetch_bookmarks_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_bookmarks_api)


class FetchAllBookmarksTests(unittest.TestCase):
    def test_all_pages_uses_the_api_page_limit(self):
        calls = []
        responses = [
            {
                "data": [{"id": "1", "text": "first"}],
                "meta": {"next_token": "next-page"},
            },
            {
                "data": [{"id": "2", "text": "second"}],
                "meta": {},
            },
        ]
        original_get_me = fetch_bookmarks_api.get_me
        original_fetch_page = fetch_bookmarks_api.fetch_bookmarks_page

        def fake_fetch_page(
            token,
            user_id,
            max_results,
            pagination_token,
            since_id,
        ):
            calls.append((max_results, pagination_token))
            return responses.pop(0)

        try:
            fetch_bookmarks_api.get_me = lambda token: "user-id"
            fetch_bookmarks_api.fetch_bookmarks_page = fake_fetch_page
            bookmarks = fetch_bookmarks_api.fetch_all_bookmarks(
                "token",
                all_pages=True,
            )
        finally:
            fetch_bookmarks_api.get_me = original_get_me
            fetch_bookmarks_api.fetch_bookmarks_page = original_fetch_page

        self.assertEqual(["1", "2"], [bookmark["id"] for bookmark in bookmarks])
        self.assertEqual([(100, None), (100, "next-page")], calls)


if __name__ == "__main__":
    unittest.main()
