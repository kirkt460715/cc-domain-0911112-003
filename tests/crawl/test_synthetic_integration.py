import json
import threading
import unittest
import urllib.request
from pathlib import Path

from tests.support import Case, authorization, context
from corpus_gate.crawl import engine
from corpus_gate.ingest import sources
from corpus_gate.crawl.fetcher import urllib_transport
from synthetic_exchange.server import make_server

SCENARIO = json.loads((Path(__file__).resolve().parents[2] / 'synthetic_exchange' / 'scenario.json').read_text())


class SyntheticServerCrawlTests(Case):
    def setUp(self):
        super().setUp()
        self.server = make_server(SCENARIO)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        sources.create(self.db, 'web', authorization())

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_end_to_end_against_synthetic_server(self):
        transport = urllib_transport(timeout=5)
        result = engine.start(self.db, 'synthetic-job', 'web', self.base + '/sitemap.xml', context(), transport=transport)
        # /article/a 的两个地址（含 utm 跟踪参数）归并为一篇；5 个唯一文章中 a 与 GB18030 成功
        self.assertEqual(result['summary']['discovered'], 5)
        self.assertEqual(result['summary']['new_versions'], 2)
        # 缺正文、分页环、越界跳转等异常路径都留下原因而非空文章
        codes = {row['code'] for row in engine.failures(self.db, 'synthetic-job')}
        self.assertEqual(result['summary']['failed'], 3)
        self.assertEqual(codes, {'crawl_no_content', 'crawl_boundary', 'crawl_pagination_loop'})
        # GB18030 文章按声明编码正确解码并入正文，没有产生空文档
        gb = engine.article(self.db, 'web', self.base + '/article/gb')
        self.assertIn('压差表读数需要人工确认。', gb['document']['raw_text'])
        info = engine.article(self.db, 'web', self.base + '/article/a?utm_source=manual')
        self.assertEqual(info['canonical_url'], self.base + '/article/a')
        # 分页被串到同一篇文章，段落/列表/表格顺序保留且导航脚本被剔除
        text = info['document']['raw_text']
        self.assertIn('# 冷却系统维护', text)
        self.assertIn('恢复进水并确认没有渗漏。', text)
        self.assertIn('滤芯 | 三个月', text)
        self.assertNotIn('站点导航', text)
        self.assertNotIn('void 0', text)
        self.assertEqual([page['url'] for page in info['evidence'][0]['pages']], [self.base + '/article/a', self.base + '/article/a/2'])

    def test_second_crawl_uses_conditional_headers_and_makes_no_new_version(self):
        transport = urllib_transport(timeout=5)
        engine.start(self.db, 'first', 'web', self.base + '/sitemap.xml', context(), transport=transport)
        second = engine.start(self.db, 'second', 'web', self.base + '/sitemap.xml', context(), transport=transport)
        self.assertEqual(second['summary']['new_versions'], 0)
        self.assertGreaterEqual(second['summary']['unchanged'], 1)
        requests = json.load(urllib.request.urlopen(self.base + '/_requests', timeout=5))
        self.assertTrue(any(req['if_none_match'] for req in requests))
        info = engine.article(self.db, 'web', self.base + '/article/a')
        self.assertEqual(len(info['evidence']), 1)


if __name__ == '__main__':
    unittest.main()
