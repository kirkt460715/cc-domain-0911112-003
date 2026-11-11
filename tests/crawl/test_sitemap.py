import unittest
from corpus_gate.errors import GateError
from corpus_gate.crawl import sitemap
from corpus_gate.crawl.fetcher import Response

ORIGIN = 'http://docs.example.com'


def transport(pages):
    def fetch(url, headers=None):
        return pages[url]
    return fetch


def ok(body):
    return Response(200, {'Content-Type': 'application/xml'}, body.encode(), 'u')


class SitemapTests(unittest.TestCase):
    def test_resolves_sitemap_index_then_urlset_and_dedupes_tracking_variants(self):
        pages = {
            'http://docs.example.com/sitemap.xml': ok('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>/pages.xml</loc></sitemap></sitemapindex>'),
            'http://docs.example.com/pages.xml': ok('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/a?utm_source=manual</loc></url><url><loc>/article/a</loc></url></urlset>'),
        }
        articles = sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['canonical_url'], 'http://docs.example.com/article/a')
        self.assertEqual(articles[0]['discovered_url'], 'http://docs.example.com/article/a?utm_source=manual')

    def test_rejects_sitemap_entry_pointing_outside_origin(self):
        pages = {'http://docs.example.com/sitemap.xml': ok('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>http://other.example.net/a</loc></url></urlset>')}
        with self.assertRaises(GateError) as caught:
            sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(caught.exception.code, 'crawl_boundary')

    def test_self_referencing_index_is_a_loop(self):
        pages = {'http://docs.example.com/sitemap.xml': ok('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>/sitemap.xml</loc></sitemap></sitemapindex>')}
        with self.assertRaises(GateError) as caught:
            sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(caught.exception.code, 'crawl_sitemap_loop')

    def test_malformed_xml_and_unknown_roots_are_rejected(self):
        pages = {'http://docs.example.com/sitemap.xml': Response(200, {}, b'<not-xml', 'u')}
        with self.assertRaises(GateError) as caught:
            sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(caught.exception.code, 'crawl_sitemap_parse')
        pages = {'http://docs.example.com/sitemap.xml': ok('<html><body>nope</body></html>')}
        with self.assertRaises(GateError) as caught:
            sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(caught.exception.code, 'crawl_sitemap_shape')

    def test_non_200_sitemap_status_is_an_error(self):
        pages = {'http://docs.example.com/sitemap.xml': Response(404, {}, b'', 'u')}
        with self.assertRaises(GateError) as caught:
            sitemap.discover(transport(pages), 'http://docs.example.com/sitemap.xml', ORIGIN)
        self.assertEqual(caught.exception.code, 'crawl_sitemap_status')


if __name__ == '__main__':
    unittest.main()
