import hashlib
import re
from html.parser import HTMLParser
from corpus_gate.errors import fail, shape, integer
from corpus_gate.crawl import urls

DEFAULT_DEFINITION = {
    'content_selector': 'main',
    'skip_tags': ['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript', 'template', 'form', 'iframe', 'svg'],
    'max_pages': 50,
    'max_bytes': 5000000,
}
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
HEADINGS = {'h1': 1, 'h2': 2, 'h3': 3, 'h4': 4, 'h5': 5, 'h6': 6}
SPACE = re.compile(r'\s+')


def validate_definition(value):
    value = {} if value is None else value
    shape(value, set(), set(DEFAULT_DEFINITION))
    result = {**DEFAULT_DEFINITION, **value}
    if result['content_selector'] != 'main':
        fail('crawl_parser_config', 'Only the main content container selector is supported')
    tags = result['skip_tags']
    if not isinstance(tags, list) or not tags or len(tags) > 50 or any(not isinstance(tag, str) or not re.fullmatch(r'[a-z0-9]{1,20}', tag) for tag in tags):
        fail('crawl_parser_config', 'skip_tags must be a bounded lowercase tag name list')
    if 'main' in tags:
        fail('crawl_parser_config', 'The main content container cannot be skipped')
    result['skip_tags'] = sorted(set(tags))
    integer(result['max_pages'], 'max_pages', 1, 500)
    integer(result['max_bytes'], 'max_bytes', 1, 50000000)
    return result


def content_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def serialize(blocks):
    return '\n\n'.join(_render(block) for block in blocks)


def _render(block, depth=0):
    indent = '  ' * depth
    kind = block['type']
    if kind == 'heading':
        return indent + '#' * block['level'] + ' ' + block['text']
    if kind == 'paragraph':
        return indent + block['text']
    if kind == 'list':
        lines = []
        for index, item in enumerate(block['items'], 1):
            marker = f'{index}. ' if block['ordered'] else '- '
            if isinstance(item, str):
                lines.append(indent + marker + item)
            else:
                lines.append(indent + marker + item['text'])
                lines.append(_render(item['sublist'], depth + 1))
        return '\n'.join(lines)
    if kind == 'table':
        return '\n'.join(indent + ' | '.join(row) for row in block['rows'])
    fail('crawl_block', 'Unknown extracted block type', type=kind)


def extract(html_text, base_url, definition=None):
    """仅提取 main 容器内可见内容，按文档顺序输出段落、列表与表格块。"""
    definition = validate_definition(definition)
    parser = _MainExtractor(base_url, set(definition['skip_tags']))
    parser.feed(html_text)
    parser.close()
    if not parser.main_seen:
        fail('crawl_no_content', 'Page has no main content container', url=base_url)
    if not parser.blocks:
        fail('crawl_no_content', 'Main content container is empty or fully hidden', url=base_url)
    text = serialize(parser.blocks)
    return {
        'blocks': parser.blocks,
        'text': text,
        'content_hash': content_hash(text),
        'canonical_url': parser.canonical,
        'next_url': parser.next_url,
        'main_seen': True,
    }


class _MainExtractor(HTMLParser):
    def __init__(self, base_url, skip_tags):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_tags = skip_tags
        self.blocks = []
        self.stack = []
        self.skip_depth = 0
        self.main_depth = 0
        self.main_seen = False
        self.loose = False
        self.canonical = None
        self.next_url = None
        self.base_href = None

    def _resolve(self, href):
        return urls.join(self.base_href or self.base_url, href)

    @staticmethod
    def _hidden(attrs):
        attr = dict(attrs)
        if 'hidden' in attr:
            return True
        style = attr.get('style', '').lower().replace(' ', '').replace('\t', '')
        return 'display:none' in style or 'visibility:hidden' in style

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == 'base' and attr.get('href'):
            self.base_href = self._resolve(attr['href'])
        if tag in ('a', 'link') and attr.get('href'):
            relations = attr.get('rel', '').lower().split()
            if 'canonical' in relations:
                self.canonical = self._resolve(attr['href'])
            if 'next' in relations:
                self.next_url = self._resolve(attr['href'])
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag in self.skip_tags or self._hidden(attrs):
            self.skip_depth = 1
            return
        if tag == 'main':
            self.main_depth += 1
            self.main_seen = True
            return
        if not self.main_depth:
            return
        if tag in HEADINGS:
            self._close_loose()
            self.stack.append({'kind': 'heading', 'level': HEADINGS[tag], 'parts': []})
        elif tag == 'p':
            self._close_loose()
            self.stack.append({'kind': 'paragraph', 'parts': []})
        elif tag in ('ul', 'ol'):
            self._close_loose()
            self.stack.append({'kind': 'list', 'ordered': tag == 'ol', 'items': []})
        elif tag == 'li':
            self.stack.append({'kind': 'item', 'parts': []})
        elif tag == 'table':
            self._close_loose()
            self.stack.append({'kind': 'table', 'rows': []})
        elif tag == 'tr':
            self.stack.append({'kind': 'row', 'cells': []})
        elif tag in ('td', 'th'):
            self.stack.append({'kind': 'cell', 'parts': []})
        elif tag == 'br':
            self._text('\n')
        elif tag == 'img' and attr.get('alt'):
            self._text(attr['alt'])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and not self.skip_depth:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == 'main':
            if self.main_depth:
                self.main_depth -= 1
                self._close_loose()
            return
        if not self.main_depth or not self.stack:
            return
        current = self.stack[-1]
        if tag in HEADINGS and current['kind'] == 'heading':
            text = _clean(current['parts'])
            if text:
                self.blocks.append({'type': 'heading', 'level': current['level'], 'text': text})
            self.stack.pop()
        elif tag == 'p' and current['kind'] == 'paragraph':
            self._emit_paragraph(current)
            self.stack.pop()
            self.loose = False
        elif tag == 'li' and current['kind'] == 'item':
            self._finish_item(current)
        elif tag in ('ul', 'ol') and current['kind'] == 'list':
            self._finish_list(current)
        elif tag == 'table' and current['kind'] == 'table':
            if current['rows']:
                self.blocks.append({'type': 'table', 'rows': current['rows']})
            self.stack.pop()
        elif tag == 'tr' and current['kind'] == 'row':
            self.stack.pop()
            parent = self._nearest('table')
            if parent is not None and current['cells']:
                parent['rows'].append(current['cells'])
        elif tag in ('td', 'th') and current['kind'] == 'cell':
            self.stack.pop()
            parent = self._nearest('row')
            if parent is not None:
                parent['cells'].append(_clean(current['parts']))

    def handle_data(self, data):
        self._text(data)

    def _text(self, data):
        if self.skip_depth or not self.main_depth:
            return
        if not self.stack:
            if not data.strip():
                return
            self.stack.append({'kind': 'paragraph', 'parts': []})
            self.loose = True
        self.stack[-1].setdefault('parts', []).append(data)

    def _close_loose(self):
        if self.loose and self.stack and self.stack[-1]['kind'] == 'paragraph':
            self._emit_paragraph(self.stack[-1])
            self.stack.pop()
        self.loose = False

    def _emit_paragraph(self, context):
        text = _clean(context['parts'])
        if text:
            self.blocks.append({'type': 'paragraph', 'text': text})

    def _finish_item(self, context):
        self.stack.pop()
        parent = self._nearest('list')
        if parent is None:
            return
        text = _clean(context['parts'])
        sublist = context.get('sublist')
        if sublist is not None:
            parent['items'].append({'text': text, 'sublist': sublist})
        elif text:
            parent['items'].append(text)

    def _finish_list(self, context):
        self.stack.pop()
        if context['items']:
            block = {'type': 'list', 'ordered': context['ordered'], 'items': context['items']}
            if self.stack and self.stack[-1]['kind'] == 'item':
                self.stack[-1]['sublist'] = block
            else:
                self.blocks.append(block)

    def _nearest(self, kind):
        for context in reversed(self.stack):
            if context['kind'] == kind:
                return context
        return None


def _clean(parts):
    return SPACE.sub(' ', ''.join(parts)).strip()
