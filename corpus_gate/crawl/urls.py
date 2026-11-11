import ipaddress
import urllib.parse
from corpus_gate.errors import fail

TRACKING_PREFIXES = ('utm_',)
TRACKING_EXACT = {'gclid', 'fbclid', 'spm', 'ref', 'ref_src', 'ref_url', 'source', 'cmpid', 'campaign', 'mc_cid', 'mc_eid'}
DEFAULT_PORTS = {'http': 80, 'https': 443}


def as_url(value, field='url'):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        fail('crawl_url', 'URL must be a bounded nonempty string', field=field)
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError as exc:
        fail('crawl_url', 'Malformed URL', field=field)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        fail('crawl_url', 'URL requires an http(s) host', field=field)
    return parsed


def normalize(value):
    """剥离跟踪参数并归一化默认端口；不跨主机归并。"""
    parsed = as_url(value)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, val) for key, val in query if not _is_tracking(key)]
    host = parsed.hostname.lower()
    port = ''
    if parsed.port is not None and parsed.port != DEFAULT_PORTS.get(parsed.scheme):
        port = f':{parsed.port}'
    netloc = f'{host}{port}'
    if parsed.username:
        credentials = parsed.username + (f':{parsed.password}' if parsed.password else '') + '@'
        netloc = credentials + netloc
    path = parsed.path or '/'
    rebuilt = urllib.parse.urlunsplit((parsed.scheme, netloc, path, urllib.parse.urlencode(kept), ''))
    return urllib.parse.urlsplit(rebuilt).geturl()


def _is_tracking(key):
    folded = key.lower()
    return folded in TRACKING_EXACT or any(folded.startswith(prefix) for prefix in TRACKING_PREFIXES)


def join(base, reference):
    try:
        return urllib.parse.urljoin(base, reference)
    except ValueError:
        fail('crawl_url', 'Malformed relative reference', reference=reference)


def origin(value):
    parsed = as_url(value)
    port = f':{parsed.port}' if parsed.port is not None and parsed.port != DEFAULT_PORTS.get(parsed.scheme) else ''
    return f'{parsed.scheme}://{parsed.hostname.lower()}{port}'


def ensure_inside(allowed_origin, url):
    """越界（不同 scheme/主机/端口，或指向公网字面 IP 地址）一律拒绝。"""
    target = origin(url)
    if target != allowed_origin:
        fail('crawl_boundary', 'Address leaves the authorized site origin', allowed=allowed_origin, target=target)
    host = as_url(url).hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if address.is_global:
        fail('crawl_boundary', 'Authorized site host must be a domain name or private address, not a public IP', host=host)


def external_id_for(canonical_url):
    """canonical 地址 -> 稳定外部标识（仅字母数字与 . _ : -，首字符为字母）。"""
    parsed = as_url(normalize(canonical_url))
    host = parsed.hostname.replace('.', '-')
    path = parsed.path.strip('/').replace('/', '-') or 'root'
    query = urllib.parse.parse_qsl(parsed.query)
    suffix = ('-' + '-'.join(f'{k}-{v}' for k, v in query)) if query else ''
    identity = f'web-{host}-{path}{suffix}'
    cleaned = ''.join(ch if (ch.isalnum() or ch in '._:-') else '-' for ch in identity)
    return cleaned[:119]
