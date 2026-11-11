from tests.support import Case, authorization, context
from corpus_gate.crawl import engine, parsers as crawl_parsers, urls
from corpus_gate.crawl.fetcher import Response
from corpus_gate.ingest import sources
from corpus_gate import documents, inspection
from corpus_gate.errors import GateError

ORIGIN = 'http://site.test'


def page(body, etag='"v1"', headers=None, status=200):
    head = {'Content-Type': 'text/html; charset=utf-8'}
    if etag:
        head['ETag'] = etag
    if headers:
        head.update(headers)
    return Response(status, head, body.encode() if isinstance(body, str) else body, 'u')


class ScriptedTransport:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        key = url[len(ORIGIN):]
        value = self.pages[key]
        if isinstance(value, list):
            value = value.pop(0)
        return value


SITEMAP_INDEX = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>/pages.xml</loc></sitemap></sitemapindex>'
URLSET = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/one?utm_campaign=nav</loc></url></urlset>'
URLSET_TWO = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/one</loc></url><url><loc>/article/two</loc></url></urlset>'


def article(body_text, nav='站点导航', canonical='/article/one', next_link=None):
    nxt = f'<a rel="next" href="{next_link}">下一页</a>' if next_link else ''
    return (f'<html><head><link rel="canonical" href="{canonical}"></head><body>'
            f'<nav>{nav}</nav><main>{body_text}</main><footer>旧页脚</footer>{nxt}'
            '<script>track()</script></body></html>')


class CrawlEngineTests(Case):
    def setUp(self):
        super().setUp()
        sources.create(self.db, 'web', authorization(regions=['cn', 'eu']))
        self.ctx = context()

    def transport(self, pages):
        return ScriptedTransport(pages)

    def start(self, job_id, transport, sitemap_url=ORIGIN + '/sitemap.xml', source='web'):
        return engine.start(self.db, job_id, source, sitemap_url, self.ctx, transport=transport)

    def test_full_crawl_ingests_article_and_maps_original_address(self):
        body = ('<h1>标题</h1><p>alpha beta gamma delta epsilon zeta eta theta</p>'
                '<ul><li>记录压力</li></ul><table><tr><th>部件</th><th>周期</th></tr>'
                '<tr><td>滤芯</td><td>三个月</td></tr></table>')
        pages = {'/sitemap.xml': page(SITEMAP_INDEX, '"s"'), '/pages.xml': page(URLSET, '"p"'),
                 '/article/one': page(article(body), '"a1"')}
        result = self.start('job1', self.transport(pages))
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['summary'], {'blocked': [], 'discovered': 1, 'failed': 0, 'ingested': 1, 'new_versions': 1, 'unchanged': 0})
        info = engine.article(self.db, 'web', ORIGIN + '/article/one?utm_campaign=nav')
        self.assertEqual(info['canonical_url'], ORIGIN + '/article/one')
        self.assertIsNotNone(info['document_id'])
        self.assertEqual([e['version'] for e in info['evidence']], [1])
        raw = info['document']['raw_text']
        self.assertIn('# 标题', raw)
        self.assertIn('滤芯 | 三个月', raw)
        self.assertNotIn('站点导航', raw)
        self.assertNotIn('track()', raw)
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_navigation_only_change_does_not_create_version(self):
        pages_one = {'/sitemap.xml': page(SITEMAP_INDEX, '"s"'), '/pages.xml': page(URLSET, '"p"'),
                     '/article/one': [page(article('<p>alpha beta gamma delta epsilon zeta</p>', '旧导航'), '"a1"'),
                                      page(article('<p>alpha beta gamma delta epsilon zeta</p>', '新导航完全不同'), '"a2"')]}
        self.start('job1', self.transport(pages_one))
        again = self.start('job2', self.transport(pages_one))
        self.assertEqual(again['summary']['new_versions'], 0)
        self.assertEqual(again['summary']['unchanged'], 1)
        info = engine.article(self.db, 'web', ORIGIN + '/article/one')
        self.assertEqual(len(info['evidence']), 1)
        self.assertEqual(info['document']['version'], 1)

    def test_body_change_creates_version_with_chain_of_evidence(self):
        first = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta eta</p>'), '"v1"')}
        self.start('job1', self.transport(first))
        second = {'/sitemap.xml': page(SITEMAP_INDEX, '"s2"'), '/pages.xml': page(URLSET, '"p2"'),
                  '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta eta</p><p>新增 theta iota kappa</p>'), '"v2"')}
        result = self.start('job2', self.transport(second))
        self.assertEqual(result['summary']['new_versions'], 1)
        info = engine.article(self.db, 'web', ORIGIN + '/article/one')
        self.assertEqual([e['version'] for e in info['evidence']], [1, 2])
        self.assertNotEqual(info['evidence'][0]['content_hash'], info['evidence'][1]['content_hash'])
        self.assertEqual(info['document']['version'], 2)
        self.assertEqual(len(documents.versions(self.db, 'web', info['external_id'])), 2)

    def test_etag_unchanged_reuses_original_page_and_sends_conditional_headers(self):
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': [page(article('<p>alpha beta gamma delta epsilon zeta</p>'), '"fixed"'),
                                  Response(304, {'ETag': '"fixed"'}, b'', 'u')]}
        transport = self.transport(pages)
        self.start('job1', transport)
        result = self.start('job2', transport)
        self.assertEqual(result['summary']['unchanged'], 1)
        conditional = [h for _, h in transport.requests if h.get('If-None-Match') == '"fixed"']
        self.assertTrue(conditional)
        row = self.db.one("SELECT etag,parser_revision FROM crawl_pages WHERE url=?", (ORIGIN + '/article/one',))
        self.assertEqual(row['etag'], '"fixed"')

    def test_pagination_chain_is_joined_under_one_article(self):
        urlset = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/p</loc></url></urlset>'
        first_html = ('<html><head><link rel="canonical" href="/article/p"></head><body><main>'
                      '<h1>标题</h1><p>alpha beta gamma delta</p></main>'
                      '<a rel="next" href="/article/p/2">下一页</a></body></html>')
        second_html = '<main><p>epsilon zeta eta theta iota kappa</p></main>'
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(urlset),
                 '/article/p': page(first_html, '"p1"'), '/article/p/2': page(second_html, '"p2"')}
        self.start('job1', self.transport(pages))
        info = engine.article(self.db, 'web', ORIGIN + '/article/p')
        self.assertEqual(info['canonical_url'], ORIGIN + '/article/p')
        self.assertEqual([p['url'] for p in info['evidence'][0]['pages']], [ORIGIN + '/article/p', ORIGIN + '/article/p/2'])
        self.assertIn('epsilon zeta', info['document']['raw_text'])
        # 分页地址也能定位到同一文档
        self.assertEqual(engine.article(self.db, 'web', ORIGIN + '/article/p/2')['document_id'], info['document_id'])

    def test_pagination_loop_is_recorded_as_failure_not_empty_article(self):
        loop_html = '<main><p>alpha beta gamma delta epsilon</p></main><a rel="next" href="/article/loop">下一页</a>'
        urlset = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/loop</loc></url></urlset>'
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(urlset), '/article/loop': page(loop_html)}
        result = self.start('job1', self.transport(pages))
        self.assertEqual(result['summary']['failed'], 1)
        failure = engine.failures(self.db, 'job1')[0]
        self.assertEqual(failure['code'], 'crawl_pagination_loop')
        self.assertIsNone(documents.head(self.db, 'web', urls.external_id_for(ORIGIN + '/article/loop')))

    def test_missing_body_is_a_failure_with_reason(self):
        urlset = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/missing</loc></url></urlset>'
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(urlset),
                 '/article/missing': page('<html><nav>维护中</nav></html>')}
        result = self.start('job1', self.transport(pages))
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(engine.failures(self.db, 'job1')[0]['code'], 'crawl_no_content')

    def test_mixed_encoding_failure_is_recorded(self):
        urlset = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/gb</loc></url></urlset>'
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(urlset),
                 '/article/gb': Response(200, {'Content-Type': 'text/html; charset=ascii'}, '主文档内容'.encode('gb18030'), 'u')}
        result = self.start('job1', self.transport(pages))
        self.assertEqual(engine.failures(self.db, 'job1')[0]['code'], 'crawl_encoding')

    def test_cross_origin_redirect_failure_is_recorded(self):
        urlset = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>/article/redirect</loc></url></urlset>'
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(urlset),
                 '/article/redirect': Response(302, {'Location': 'http://outside.invalid/x'}, b'', 'u')}
        result = self.start('job1', self.transport(pages))
        self.assertEqual(engine.failures(self.db, 'job1')[0]['code'], 'crawl_boundary')

    def test_withdrawal_during_crawl_blocks_uncommitted_content(self):
        db = self.db

        class WithdrawingTransport(ScriptedTransport):
            def __init__(self):
                super().__init__({})
                self.revoked = False

            def __call__(self, url, headers):
                key = url[len(ORIGIN):]
                if key == '/sitemap.xml':
                    return page(SITEMAP_INDEX)
                if key == '/pages.xml':
                    return page(URLSET_TWO)
                if key == '/article/one':
                    return page('<main><p>alpha beta gamma delta epsilon zeta</p></main>')
                if key == '/article/two':
                    if not self.revoked:
                        sources.revoke(db, 'web', sources.get(db, 'web')['revision'], 'withdrawn mid-flight')
                        self.revoked = True
                    return page('<main><p>theta iota kappa lambda mu nu xi</p></main>')
                raise AssertionError(key)
        result = self.start('job1', WithdrawingTransport())
        self.assertEqual(result['state'], 'blocked')
        self.assertIn(ORIGIN + '/article/two', result['summary']['blocked'])
        one_id = urls.external_id_for(ORIGIN + '/article/one')
        self.assertIsNotNone(documents.head(db, 'web', one_id))
        self.assertIsNone(db.one("SELECT 1 FROM crawl_articles WHERE canonical_url LIKE '%/article/two'"))
        self.assertEqual(engine.failures(db, 'job1')[0]['code'], 'crawl_unauthorized')
        self.assertTrue(inspection.check(db)['ok'])

    def test_recrawl_by_original_url_returns_document_and_records_failures(self):
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta</p>'), '"a1"')}
        self.start('job1', self.transport(pages))
        result = engine.recrawl(self.db, 'web', ORIGIN + '/article/one?utm_source=x', self.ctx, transport=self.transport(pages))
        self.assertEqual(result['requested_url'], ORIGIN + '/article/one')
        self.assertIsNotNone(result['document_id'])
        # 越权重抓被拒
        sources.revoke(self.db, 'web', sources.get(self.db, 'web')['revision'], 'gone')
        with self.assertRaises(GateError) as caught:
            engine.recrawl(self.db, 'web', ORIGIN + '/article/one', self.ctx, transport=self.transport(pages))
        self.assertEqual(caught.exception.code, 'crawl_unauthorized')

    def test_parser_upgrade_reextracts_from_stored_pages(self):
        html = ('<html><head><link rel="canonical" href="/article/one"></head><body>'
                '<main><p>alpha beta gamma delta epsilon zeta eta</p>'
                '<aside>侧栏推广文本 lambda mu nu</aside></main></body></html>')
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET), '/article/one': page(html, '"a1"')}
        self.start('job1', self.transport(pages))
        before = engine.article(self.db, 'web', ORIGIN + '/article/one')
        self.assertNotIn('侧栏推广文本', before['document']['raw_text'])
        # 同定义新版本重提取：正文未变，不产生文档版本
        crawl_parsers.put(self.db, 'default', 1, {})
        report = engine.reextract(self.db, 'web')
        self.assertEqual(report['new_versions'], 0)
        self.assertEqual(before['document']['version'], 1)
        # 升级为不再跳过 aside 的解析器：main 内 aside 文本被纳入 -> 正文变化 -> 新版本证据
        crawl_parsers.put(self.db, 'default', 2, {'skip_tags': ['script', 'style', 'nav', 'footer', 'header', 'noscript', 'template', 'form', 'iframe', 'svg']})
        report = engine.reextract(self.db, 'web')
        self.assertEqual(report['new_versions'], 1)
        self.assertEqual(report['parser_revision'], 3)
        after = engine.article(self.db, 'web', ORIGIN + '/article/one')
        self.assertEqual([e['version'] for e in after['evidence']], [1, 2])
        self.assertIn('侧栏推广文本', after['document']['raw_text'])

    def test_unknown_url_lookup_and_job_idempotency(self):
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta</p>'))}
        transport = self.transport(pages)
        first = self.start('job1', transport)
        second = self.start('job1', transport)
        self.assertEqual(second['job_id'], first['job_id'])
        self.assertEqual(second['state'], 'complete')
        with self.assertRaises(GateError) as caught:
            engine.article(self.db, 'web', ORIGIN + '/never/crawled')
        self.assertEqual(caught.exception.code, 'crawl_url_unknown')

    def test_crawl_requires_authorization(self):
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta</p>'))}
        denied = {**self.ctx, 'purpose': 'marketing'}
        with self.assertRaises(GateError) as caught:
            engine.start(self.db, 'jobX', 'web', ORIGIN + '/sitemap.xml', denied, transport=self.transport(pages))
        self.assertEqual(caught.exception.code, 'crawl_unauthorized')

    def test_no_dangling_processing_batches_after_crawl_reextract_and_recrawl(self):
        pages = {'/sitemap.xml': page(SITEMAP_INDEX), '/pages.xml': page(URLSET),
                 '/article/one': page(article('<p>alpha beta gamma delta epsilon zeta eta</p>'), '"a1"')}
        transport = self.transport(pages)
        self.start('job1', transport)
        engine.reextract(self.db, 'web')
        engine.recrawl(self.db, 'web', ORIGIN + '/article/one', self.ctx, transport=self.transport(pages))
        states = {row['batch_id']: row['state'] for row in self.db.all('SELECT batch_id,state FROM batches')}
        self.assertTrue(states)
        self.assertTrue(all(state == 'complete' for state in states.values()), states)
        self.assertTrue(inspection.check(self.db)['ok'])


if __name__ == '__main__':
    import unittest
    unittest.main()
