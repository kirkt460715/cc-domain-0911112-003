import xml.etree.ElementTree as ET
from corpus_gate.errors import fail
from corpus_gate.crawl import urls

NAMESPACE = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
MAX_INDEX_DEPTH = 5
MAX_ENTRIES = 10000


def discover(fetch, seed_url, allowed_origin):
    """从 sitemap（或 sitemap 索引）发现文章地址，全部限定在授权来源内。"""
    queue = [(seed_url, 0)]
    seen_sitemaps = set()
    articles = []
    seen_articles = set()
    while queue:
        location, depth = queue.pop(0)
        urls.ensure_inside(allowed_origin, location)
        if location in seen_sitemaps:
            fail('crawl_sitemap_loop', 'Sitemap index references itself', url=location)
        seen_sitemaps.add(location)
        response = fetch(location)
        if response.status != 200:
            fail('crawl_sitemap_status', 'Sitemap retrieval failed', url=location, status=response.status)
        root = _parse(response, location)
        if root.tag == f'{NAMESPACE}sitemapindex' or root.tag == 'sitemapindex':
            if depth >= MAX_INDEX_DEPTH:
                fail('crawl_sitemap_depth', 'Sitemap index nesting exceeds limit', url=location)
            for entry in root.findall(f'{NAMESPACE}sitemap') or root.findall('sitemap'):
                loc = _loc(entry, location)
                urls.ensure_inside(allowed_origin, loc)
                queue.append((loc, depth + 1))
        elif root.tag == f'{NAMESPACE}urlset' or root.tag == 'urlset':
            for entry in root.findall(f'{NAMESPACE}url') or root.findall('url'):
                loc = _loc(entry, location)
                urls.ensure_inside(allowed_origin, loc)
                normalized = urls.normalize(loc)
                if normalized not in seen_articles:
                    seen_articles.add(normalized)
                    articles.append({'discovered_url': loc, 'canonical_url': normalized})
                    if len(articles) > MAX_ENTRIES:
                        fail('crawl_sitemap_size', 'Sitemap lists more entries than the crawl limit')
        else:
            fail('crawl_sitemap_shape', 'Document is neither a sitemap index nor a urlset', url=location, root=root.tag)
    if not articles:
        fail('crawl_sitemap_empty', 'Sitemap did not list any article URLs', url=seed_url)
    return articles


def _parse(response, location):
    try:
        return ET.fromstring(response.body)
    except ET.ParseError as exc:
        fail('crawl_sitemap_parse', 'Sitemap XML is malformed', url=location, detail=str(exc))


def _loc(entry, base):
    node = entry.find(f'{NAMESPACE}loc')
    if node is None:
        node = entry.find('loc')
    if node is None or not (node.text or '').strip():
        fail('crawl_sitemap_shape', 'Sitemap entry is missing loc')
    return urls.join(base, node.text.strip())
