import contextlib
import io
import json
import pathlib
import tempfile
import threading
import unittest
from pathlib import Path

from tests.support import Case, authorization
from corpus_gate.ingest import sources
from corpus_gate.cli import main
from synthetic_exchange.server import make_server

SCENARIO = json.loads((Path(__file__).resolve().parents[2] / 'synthetic_exchange' / 'scenario.json').read_text())


class CrawlCliTests(Case):
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

    def test_crawl_commands_through_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = pathlib.Path(folder) / 'gate.sqlite3'
            context_path = pathlib.Path(folder) / 'context.json'
            context_path.write_text(json.dumps({'purpose': 'research', 'region': 'cn', 'at': '2026-11-11T00:00:00Z', 'fulfilled_obligations': ['attribution']}))
            # CLI 使用独立文件库，先在该库内建立来源与默认 profile
            from corpus_gate.service import CorpusGate
            from corpus_gate.text import profiles
            with CorpusGate(db_path) as setup:
                sources.create(setup.db, 'web', authorization())
                profiles.put(setup.db, 'default', 0, {})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(['--db', str(db_path), 'crawl', 'cli-job', '--source', 'web',
                               '--sitemap', self.base + '/sitemap.xml', '--context', str(context_path)])
            self.assertEqual(status, 0)
            job = json.loads(output.getvalue())
            self.assertEqual(job['state'], 'complete')
            self.assertGreaterEqual(job['summary']['new_versions'], 1)

            job_output = io.StringIO()
            with contextlib.redirect_stdout(job_output):
                status = main(['--db', str(db_path), 'crawl-job', 'cli-job'])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(job_output.getvalue())['job_id'], 'cli-job')

            article_output = io.StringIO()
            with contextlib.redirect_stdout(article_output):
                status = main(['--db', str(db_path), 'crawl-article', '--source', 'web', '--url', self.base + '/article/a?utm_source=manual'])
            self.assertEqual(status, 0)
            article = json.loads(article_output.getvalue())
            self.assertIsNotNone(article['document_id'])

    def test_parser_put_and_operations_listing(self):
        with tempfile.TemporaryDirectory() as folder:
            definition = pathlib.Path(folder) / 'parser.json'
            definition.write_text(json.dumps({'max_pages': 25}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(['--db', ':memory:', 'crawl-parser', 'custom', '--revision', '0', '--input', str(definition)])
            self.assertEqual(status, 0)
            parser = json.loads(output.getvalue())
            self.assertEqual(parser['parser_id'], 'custom')
            self.assertEqual(parser['revision'], 1)
            self.assertEqual(parser['definition']['max_pages'], 25)

            listing = io.StringIO()
            with contextlib.redirect_stdout(listing):
                main(['--db', ':memory:', 'operations'])
            operations = {row['operation'] for row in json.loads(listing.getvalue())}
            self.assertIn('crawl.start', operations)
            self.assertIn('crawl.reextract', operations)


if __name__ == '__main__':
    unittest.main()
