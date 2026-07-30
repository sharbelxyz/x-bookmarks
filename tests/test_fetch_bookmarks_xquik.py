import importlib.util
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_bookmarks_xquik.py"
)
SPEC = importlib.util.spec_from_file_location(
    "fetch_bookmarks_xquik",
    MODULE_PATH,
)
fetch_bookmarks_xquik = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_bookmarks_xquik)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.closed = False

    def read(self):
        return self.body

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def page(tweets, has_next_page=False, next_cursor=""):
    return json.dumps(
        {
            "tweets": tweets,
            "has_next_page": has_next_page,
            "next_cursor": next_cursor,
        }
    ).encode()


class XquikBookmarksTests(unittest.TestCase):
    def test_builds_authenticated_folder_request_and_honors_count(self):
        response = FakeResponse(
            page(
                [{"id": "1"}, {"id": "2"}],
                has_next_page=True,
                next_cursor="unused",
            )
        )
        opener = FakeOpener([response])

        bookmarks = fetch_bookmarks_xquik.fetch_bookmarks(
            api_key="xq_test",
            count=1,
            cursor="start",
            folder_id="read-later",
            opener=opener,
        )

        self.assertEqual([{"id": "1"}], bookmarks)
        self.assertTrue(response.closed)
        self.assertEqual(1, len(opener.requests))
        request, timeout = opener.requests[0]
        parsed = urlparse(request.full_url)
        self.assertEqual("https", parsed.scheme)
        self.assertEqual("xquik.com", parsed.netloc)
        self.assertEqual("/api/v1/x/bookmarks", parsed.path)
        self.assertNotIn("xq_test", request.full_url)
        self.assertEqual(
            {"cursor": ["start"], "folderId": ["read-later"]},
            parse_qs(parsed.query),
        )
        self.assertEqual("xq_test", request.headers["X-api-key"])
        self.assertEqual("application/json", request.headers["Accept"])
        self.assertEqual(
            fetch_bookmarks_xquik.REQUEST_TIMEOUT_SECONDS,
            timeout,
        )

    def test_all_pages_continues_through_an_empty_page(self):
        responses = [
            FakeResponse(page([], has_next_page=True, next_cursor="page-2")),
            FakeResponse(page([{"id": "2"}])),
        ]
        opener = FakeOpener(responses)

        bookmarks = fetch_bookmarks_xquik.fetch_bookmarks(
            api_key="xq_test",
            all_pages=True,
            opener=opener,
        )

        self.assertEqual([{"id": "2"}], bookmarks)
        self.assertEqual(2, len(opener.requests))
        self.assertTrue(all(response.closed for response in responses))
        second_request, _ = opener.requests[1]
        self.assertEqual(
            {"cursor": ["page-2"]},
            parse_qs(urlparse(second_request.full_url).query),
        )

    def test_rejects_a_repeated_cursor(self):
        responses = [
            FakeResponse(page([], has_next_page=True, next_cursor="repeat")),
            FakeResponse(page([], has_next_page=True, next_cursor="repeat")),
        ]

        with self.assertRaisesRegex(
            fetch_bookmarks_xquik.XquikApiError,
            "repeated bookmark cursor",
        ):
            fetch_bookmarks_xquik.fetch_bookmarks(
                api_key="xq_test",
                all_pages=True,
                opener=FakeOpener(responses),
            )

        self.assertTrue(all(response.closed for response in responses))

    def test_requires_an_api_key_before_network_access(self):
        opener = FakeOpener()

        with self.assertRaisesRegex(
            fetch_bookmarks_xquik.XquikApiError,
            "XQUIK_API_KEY is required",
        ):
            fetch_bookmarks_xquik.fetch_bookmarks(
                api_key=" ",
                opener=opener,
            )

        self.assertEqual([], opener.requests)

    def test_wraps_http_errors_and_closes_the_response(self):
        response = FakeResponse(b"rate limited")
        error = HTTPError(
            fetch_bookmarks_xquik.XQUIK_BOOKMARKS_URL,
            429,
            "Too Many Requests",
            None,
            response,
        )

        with self.assertRaisesRegex(
            fetch_bookmarks_xquik.XquikApiError,
            "HTTP 429",
        ):
            fetch_bookmarks_xquik.fetch_bookmarks(
                api_key="xq_test",
                opener=FakeOpener(error=error),
            )

        self.assertTrue(response.closed)

    def test_wraps_network_and_response_errors(self):
        cases = [
            (
                FakeOpener(error=URLError("private network detail")),
                "Check connectivity",
            ),
            (
                FakeOpener([FakeResponse(b"not-json")]),
                "invalid bookmark response",
            ),
            (
                FakeOpener(
                    [
                        FakeResponse(
                            json.dumps(
                                {
                                    "tweets": [],
                                    "has_next_page": "yes",
                                    "next_cursor": "",
                                }
                            ).encode()
                        )
                    ]
                ),
                "invalid bookmark response",
            ),
        ]

        for opener, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                fetch_bookmarks_xquik.XquikApiError,
                message,
            ):
                fetch_bookmarks_xquik.fetch_bookmarks(
                    api_key="xq_test",
                    opener=opener,
                )


if __name__ == "__main__":
    unittest.main()
