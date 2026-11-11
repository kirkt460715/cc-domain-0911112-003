import unittest
from corpus_gate.errors import GateError
from corpus_gate.crawl import urls


class UrlNormalizationTests(unittest.TestCase):
    def test_tracking_parameters_are_stripped_but_real_query_is_kept(self):
        self.assertEqual(urls.normalize('http://docs.example.com/a?utm_source=x&id=7&fbclid=z'),
                         'http://docs.example.com/a?id=7')
        self.assertEqual(urls.normalize('http://docs.example.com/a?gclid=1&spm=2'),
                         'http://docs.example.com/a')
        # 同文不同导航（仅跟踪参数差异）归并到同一 canonical
        self.assertEqual(urls.normalize('http://docs.example.com/article/a?utm_campaign=nav'),
                         urls.normalize('http://docs.example.com/article/a'))

    def test_default_port_and_case_are_normalized(self):
        self.assertEqual(urls.normalize('http://Docs.Example.com:80/path/'), 'http://docs.example.com/path/')
        self.assertEqual(urls.normalize('https://docs.example.com:443/x'), 'https://docs.example.com/x')
        self.assertEqual(urls.normalize('http://docs.example.com:8080/x'), 'http://docs.example.com:8080/x')

    def test_non_http_and_garbage_urls_are_rejected(self):
        for bad in ('ftp://docs.example.com/a', 'not a url', 'http:///nohost', ''):
            with self.assertRaises(GateError) as caught:
                urls.normalize(bad)
            self.assertEqual(caught.exception.code, 'crawl_url')

    def test_origin_boundary_rejects_cross_site_and_accepts_same_origin(self):
        allowed = 'http://docs.example.com'
        urls.ensure_inside(allowed, 'http://docs.example.com/article/1?x=1')
        urls.ensure_inside(allowed, 'http://docs.example.com:80/')
        for outside in ('http://other.example.com/a', 'https://docs.example.com/a',
                        'http://docs.example.com:8080/a', 'http://evil.com/'):
            with self.assertRaises(GateError) as caught:
                urls.ensure_inside(allowed, outside)
            self.assertEqual(caught.exception.code, 'crawl_boundary')

    def test_public_ip_origin_is_rejected_but_private_loopback_is_allowed(self):
        with self.assertRaises(GateError) as caught:
            urls.ensure_inside('http://8.8.8.8', 'http://8.8.8.8/a')
        self.assertEqual(caught.exception.code, 'crawl_boundary')
        urls.ensure_inside('http://127.0.0.1:8765', 'http://127.0.0.1:8765/article/a')

    def test_external_id_is_stable_and_identifier_shaped(self):
        first = urls.external_id_for('http://docs.example.com/guide/pump?utm_source=x')
        second = urls.external_id_for('http://docs.example.com/guide/pump')
        self.assertEqual(first, second)
        self.assertTrue(first[0].isalpha())
        self.assertRegex(first, r'[A-Za-z][A-Za-z0-9._:-]*')
        self.assertNotEqual(urls.external_id_for('http://docs.example.com/guide/valve'), first)


if __name__ == '__main__':
    unittest.main()
