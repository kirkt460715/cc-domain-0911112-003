import unittest
from corpus_gate.errors import GateError
from corpus_gate.crawl import extractor


class ExtractorTests(unittest.TestCase):
    BASE = 'http://docs.example.com/article/a'

    def extract(self, html, definition=None):
        return extractor.extract(html, self.BASE, definition)

    def test_preserves_paragraph_list_table_order_and_skips_noise(self):
        html = (
            '<html><head><link rel="canonical" href="/article/a"></head><body>'
            '<nav>站点导航 首页 关于</nav>'
            '<main>'
            '<h1>冷却系统维护</h1>'
            '<p>关闭进水阀后检查密封。</p>'
            '<ul><li>记录压力</li><li>记录温度</li></ul>'
            '<table><tr><th>部件</th><th>周期</th></tr>'
            '<tr><td>滤芯</td><td>三个月</td></tr></table>'
            '<span hidden>内部隐藏提示</span>'
            '<div style="display:none">样式隐藏块</div>'
            '</main>'
            '<footer>过时页脚 版权所有</footer>'
            '<script>var nav="should not appear";</script>'
            '<style>.x{color:red}</style>'
            '<a rel="next" href="/article/a/2">下一页</a>'
            '</body></html>')
        result = self.extract(html)
        kinds = [block['type'] for block in result['blocks']]
        self.assertEqual(kinds, ['heading', 'paragraph', 'list', 'table'])
        text = result['text']
        for noise in ('站点导航', '过时页脚', 'should not appear', '内部隐藏提示', '样式隐藏块', 'color:red'):
            self.assertNotIn(noise, text)
        self.assertIn('# 冷却系统维护', text)
        self.assertIn('- 记录压力', text)
        self.assertIn('部件 | 周期', text)
        self.assertIn('滤芯 | 三个月', text)
        # 结构稳定：同样输入产生同样内容哈希
        self.assertEqual(result['content_hash'], self.extract(html)['content_hash'])
        self.assertEqual(result['canonical_url'], 'http://docs.example.com/article/a')
        self.assertEqual(result['next_url'], 'http://docs.example.com/article/a/2')

    def test_ordered_and_nested_lists(self):
        html = '<main><ol><li>第一步<ul><li>子步骤甲</li><li>子步骤乙</li></ul></li><li>第二步</li></ol></main>'
        text = self.extract(html)['text']
        self.assertEqual(text.splitlines(), ['1. 第一步', '  - 子步骤甲', '  - 子步骤乙', '2. 第二步'])

    def test_missing_main_or_empty_main_is_rejected_not_empty_article(self):
        with self.assertRaises(GateError) as caught:
            self.extract('<html><nav>维护中</nav></html>')
        self.assertEqual(caught.exception.code, 'crawl_no_content')
        with self.assertRaises(GateError) as caught:
            self.extract('<main><script>only script</script></main>')
        self.assertEqual(caught.exception.code, 'crawl_no_content')

    def test_navigation_change_keeps_content_hash_but_body_change_moves_it(self):
        old = '<main><p>同一段稳定正文 alpha beta gamma delta epsilon。</p></main>'
        new_nav = '<nav>换了导航栏</nav><main><p>同一段稳定正文 alpha beta gamma delta epsilon。</p></main>'
        changed = '<main><p>同一段稳定正文 alpha beta gamma delta epsilon。</p><p>新增第二段 zeta eta theta。</p></main>'
        self.assertEqual(self.extract(old)['content_hash'], self.extract(new_nav)['content_hash'])
        self.assertNotEqual(self.extract(old)['content_hash'], self.extract(changed)['content_hash'])

    def test_image_alt_and_br_are_handled(self):
        text = self.extract('<main><p>图前<img alt="压力表读数">图后<br>换行内容</p></main>')['text']
        self.assertIn('压力表读数', text)
        self.assertIn('换行内容', text)

    def test_parser_definition_validation(self):
        with self.assertRaises(GateError):
            extractor.validate_definition({'content_selector': 'article'})
        with self.assertRaises(GateError):
            extractor.validate_definition({'skip_tags': ['main']})
        with self.assertRaises(GateError):
            extractor.validate_definition({'max_pages': 0})
        defaulted = extractor.validate_definition(None)
        self.assertEqual(defaulted['content_selector'], 'main')
        self.assertIn('nav', defaulted['skip_tags'])


if __name__ == '__main__':
    unittest.main()
