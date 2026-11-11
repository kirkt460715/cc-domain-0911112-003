import unittest
from corpus_gate.errors import GateError
from corpus_gate.crawl import fetcher
from corpus_gate.crawl.fetcher import Response


def fake(pages):
    def transport(url, headers):
        return pages[url]
    return transport


class FetcherTests(unittest.TestCase):
    ORIGIN = 'http://docs.example.com'

    def test_follows_in_origin_redirect_and_sends_conditional_headers(self):
        pages = {
            'http://docs.example.com/old': Response(302, {'Location': '/new'}, b'', 'http://docs.example.com/old'),
            'http://docs.example.com/new': Response(200, {'Content-Type': 'text/html; charset=utf-8', 'ETag': '"n"'}, '<main>ok</main>'.encode(), 'http://docs.example.com/new'),
        }
        seen = []

        def transport(url, headers):
            seen.append((url, headers))
            return pages[url]
        response = fetcher.fetch(transport, self.ORIGIN, 'http://docs.example.com/old', {'etag': '"old"', 'last_modified': 'Wed, 11 Nov 2026 00:00:00 GMT'})
        self.assertEqual(response.status, 200)
        self.assertEqual(seen[0][1]['If-None-Match'], '"old"')
        self.assertEqual(seen[0][1]['If-Modified-Since'], 'Wed, 11 Nov 2026 00:00:00 GMT')

    def test_cross_origin_redirect_is_blocked(self):
        pages = {'http://docs.example.com/leak': Response(302, {'Location': 'http://outside.invalid/x'}, b'', 'u')}
        with self.assertRaises(GateError) as caught:
            fetcher.fetch(fake(pages), self.ORIGIN, 'http://docs.example.com/leak')
        self.assertEqual(caught.exception.code, 'crawl_boundary')

    def test_redirect_chain_loop_hits_limit(self):
        pages = {
            'http://docs.example.com/a': Response(302, {'Location': '/b'}, b'', 'u'),
            'http://docs.example.com/b': Response(302, {'Location': '/a'}, b'', 'u'),
        }
        with self.assertRaises(GateError) as caught:
            fetcher.fetch(fake(pages), self.ORIGIN, 'http://docs.example.com/a')
        self.assertIn(caught.exception.code, {'crawl_boundary', 'crawl_redirect_limit'})

    def test_redirect_without_location_is_an_error(self):
        pages = {'http://docs.example.com/a': Response(302, {}, b'', 'u')}
        with self.assertRaises(GateError) as caught:
            fetcher.fetch(fake(pages), self.ORIGIN, 'http://docs.example.com/a')
        self.assertEqual(caught.exception.code, 'crawl_redirect')

    def test_oversized_body_is_rejected(self):
        pages = {'http://docs.example.com/big': Response(200, {}, b'x' * 10, 'u')}
        with self.assertRaises(GateError) as caught:
            fetcher.fetch(fake(pages), self.ORIGIN, 'http://docs.example.com/big', max_bytes=5)
        self.assertEqual(caught.exception.code, 'crawl_too_large')

    def test_http_error_status_is_returned_for_engine_to_classify(self):
        pages = {'http://docs.example.com/gone': Response(404, {}, b'not found', 'u')}
        response = fetcher.fetch(fake(pages), self.ORIGIN, 'http://docs.example.com/gone')
        self.assertEqual(response.status, 404)

    def test_decode_uses_declared_charset_and_falls_back_to_utf8(self):
        gb = '压差表读数需要人工确认。'.encode('gb18030')
        response = Response(200, {'Content-Type': 'text/html; charset=gb18030'}, gb, 'u')
        text, encoding = fetcher.decode(response)
        self.assertIn('gb18030', encoding.lower())
        self.assertEqual(text, '压差表读数需要人工确认。')
        utf8 = Response(200, {'Content-Type': 'text/html'}, '纯 utf8'.encode(), 'u')
        text, encoding = fetcher.decode(utf8)
        self.assertEqual(text, '纯 utf8')

    def test_undecodable_mixed_bytes_leave_a_reason(self):
        response = Response(200, {'Content-Type': 'text/html; charset=ascii'}, b'abc\xff\xfe broken', 'u')
        with self.assertRaises(GateError) as caught:
            fetcher.decode(response)
        self.assertEqual(caught.exception.code, 'crawl_encoding')


if __name__ == '__main__':
    unittest.main()
