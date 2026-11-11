import codecs
import re
import urllib.error
import urllib.request
from corpus_gate.errors import fail
from corpus_gate.crawl import urls

MAX_REDIRECTS = 5
MAX_BYTES = 50000000
CHARSET = re.compile(r'charset=([A-Za-z0-9._-]+)', re.IGNORECASE)
META_CHARSET = re.compile(rb'<meta[^>]+charset=["\']?([A-Za-z0-9._-]+)', re.IGNORECASE)


class Response:
    def __init__(self, status, headers, body, final_url):
        self.status = status
        self.headers = {key.lower(): value for key, value in headers.items()}
        self.body = body
        self.final_url = final_url

    def header(self, name):
        return self.headers.get(name.lower())

    def content_type(self):
        return self.header('content-type') or ''

    def declared_encoding(self):
        match = CHARSET.search(self.content_type())
        if match:
            return match.group(1)
        head = self.body[:4096]
        match = META_CHARSET.search(head)
        return match.group(1).decode('ascii', 'replace') if match else None


def urllib_transport(timeout=15):
    def transport(request_url, headers):
        request = urllib.request.Request(request_url, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as reply:
                return Response(reply.status, dict(reply.headers), reply.read(MAX_BYTES + 1), reply.geturl())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers), exc.read(MAX_BYTES + 1), exc.geturl())
    return transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(transport, allowed_origin, url, condition=None, max_bytes=MAX_BYTES):
    """单跳抓取：重定向手工逐跳校验，绝不离开授权来源。"""
    condition = condition or {}
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        headers = {'User-Agent': 'corpus-gate-crawl/1.0', 'Accept': 'text/html,application/xml'}
        if condition.get('etag'):
            headers['If-None-Match'] = condition['etag']
        if condition.get('last_modified'):
            headers['If-Modified-Since'] = condition['last_modified']
        response = transport(current, headers)
        if response.status in (301, 302, 303, 307, 308):
            location = response.header('location')
            if not location:
                fail('crawl_redirect', 'Redirect response lacks Location header', url=current, status=response.status)
            target = urls.join(current, location)
            urls.ensure_inside(allowed_origin, target)
            current = target
            continue
        if hop >= MAX_REDIRECTS:
            fail('crawl_redirect_limit', 'Too many redirects', url=url)
        if len(response.body) > max_bytes:
            fail('crawl_too_large', 'Response body exceeds the configured byte limit', url=current, limit=max_bytes)
        response.final_url = current
        return response
    fail('crawl_redirect_limit', 'Too many redirects', url=url)


def decode(response):
    """按 Content-Type/meta 声明解码；声明缺失时回退 UTF-8，混杂编码不可解会报错。"""
    declared = response.declared_encoding()
    candidates = [declared] if declared else []
    candidates.append('utf-8')
    last_error = None
    for encoding in candidates:
        try:
            label = codecs.lookup(encoding).name
        except LookupError:
            last_error = encoding
            continue
        try:
            return response.body.decode(label), label
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = str(exc)
    fail('crawl_encoding', 'Response body cannot be decoded with declared encodings', encoding=declared, detail=last_error)
