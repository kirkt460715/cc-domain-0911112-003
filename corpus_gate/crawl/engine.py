import json
from corpus_gate.errors import identifier, text, fail, canonical, digest, GateError
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate.crawl import urls, sitemap, extractor, fetcher, parsers
from corpus_gate.crawl.fetcher import urllib_transport
from corpus_gate import documents

DEFAULT_PROFILE_ID = 'default'


def _batch_id(job_id):
    return f'crawl:{job_id}'


def _ensure_batch(db, job_id, source_id, profile):
    """采集批次沿用既有 batches/intake 机制，使文档可经批次与校验链路追溯。"""
    batch_id = _batch_id(job_id)
    if db.one('SELECT 1 FROM batches WHERE batch_id=?', (batch_id,)):
        return batch_id
    source = sources.get(db, source_id)
    with db.transaction():
        db.execute("INSERT INTO batches(batch_id,source_id,profile_id,profile_revision,profile_hash,source_revision,state,revision,cursor,total,digest,created_at) VALUES(?,?,?,?,?,?,'processing',1,0,0,?,?)", (batch_id, source_id, profile['profile_id'], profile['revision'], profile['digest'], source['revision'], digest([]), db.clock()))
        db.log('crawl.batch_opened', {'batch_id': batch_id, 'source_id': source_id})
    return batch_id


def _close_batch(db, job_id):
    batch_id = _batch_id(job_id)
    current = db.one('SELECT state FROM batches WHERE batch_id=?', (batch_id,))
    if current is None or current['state'] == 'complete':
        return
    rows = db.all('SELECT payload FROM intake WHERE batch_id=? ORDER BY ordinal', (batch_id,))
    records = [json.loads(row['payload']) for row in rows]
    admitted = sum(1 for item in db.all('SELECT state FROM intake WHERE batch_id=?', (batch_id,)) if item['state'] in {'processed', 'duplicate'})
    with db.transaction():
        db.execute("UPDATE batches SET state='complete',revision=revision+1,total=?,digest=? WHERE batch_id=?", (len(records), digest(records), batch_id))
        db.log('crawl.batch_closed', {'batch_id': batch_id, 'records': len(records), 'admitted': admitted})


def _admit(db, job_id, source_id, record, profile):
    batch_id = _ensure_batch(db, job_id, source_id, profile)
    with db.transaction():
        ordinal = db.scalar('SELECT COUNT(*) FROM intake WHERE batch_id=?', (batch_id,))
        db.execute("INSERT INTO intake(batch_id,ordinal,payload,digest,state) VALUES(?,?,?,?,'pending')", (batch_id, ordinal, canonical(record), digest(record)))
        try:
            result = documents.ingest(db, batch_id, source_id, record, profile)
        except GateError:
            db.execute("UPDATE intake SET state='failed',error=? WHERE batch_id=? AND ordinal=?", (canonical({'code': 'crawl_admit', 'message': 'Document pipeline rejected crawled article'}), batch_id, ordinal))
            raise
        state = 'duplicate' if result['duplicate'] else 'processed'
        db.execute('UPDATE intake SET state=?,document_id=? WHERE batch_id=? AND ordinal=?', (state, result['document']['document_id'], batch_id, ordinal))
        db.execute('UPDATE batches SET cursor=cursor+1,total=total+1,revision=revision+1,digest=? WHERE batch_id=?', (digest([json.loads(item['payload']) for item in db.all('SELECT payload FROM intake WHERE batch_id=? ORDER BY ordinal', (batch_id,))]), batch_id))
    return result


def start(db, job_id, source_id, sitemap_url, context, parser_id=None, profile_id=None, transport=None):
    identifier(job_id)
    text(sitemap_url, 'sitemap_url', 4096)
    transport = transport or urllib_transport()
    with db.transaction():
        existing = db.one('SELECT * FROM crawl_jobs WHERE job_id=?', (job_id,))
        if existing:
            return _job(db, job_id)
        source = sources.get(db, source_id)
        decision = sources.authorize(db, source_id, context)
        if not decision['allowed']:
            fail('crawl_unauthorized', 'Source authorization does not permit this crawl', authorization=decision)
        parser = parsers.ensure_default(db) if parser_id is None else parsers.get(db, parser_id)
        profile = profiles.get(db, profile_id or DEFAULT_PROFILE_ID)
        seed = urls.normalize(sitemap_url)
        urls.ensure_inside(urls.origin(seed), seed)
        db.execute("INSERT INTO crawl_jobs(job_id,source_id,seed_url,state,parser_id,parser_revision,summary,created_at) VALUES(?,?,?,'running',?,?,?,?)", (job_id, source_id, seed, parser['parser_id'], parser['revision'], canonical({'discovered': 0, 'ingested': 0, 'new_versions': 0, 'unchanged': 0, 'failed': 0, 'blocked': []}), db.clock()))
        db.log('crawl.job_started', {'job_id': job_id, 'source_id': source_id, 'seed_url': seed, 'profile_id': profile['profile_id'], 'profile_revision': profile['revision']})
    allowed_origin = urls.origin(seed)
    summary = {'discovered': 0, 'ingested': 0, 'new_versions': 0, 'unchanged': 0, 'failed': 0, 'blocked': []}
    try:
        discovered = sitemap.discover(lambda location: fetcher.fetch(transport, allowed_origin, location, max_bytes=parser['definition']['max_bytes']), seed, allowed_origin)
        summary['discovered'] = len(discovered)
        with db.transaction():
            for entry in discovered:
                db.execute('INSERT INTO crawl_urls(source_id,url,canonical_url,discovered_via) VALUES(?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,discovered_via=excluded.discovered_via', (source_id, entry['canonical_url'], entry['canonical_url'], f'sitemap:{seed}'))
                db.execute('INSERT INTO crawl_urls(source_id,url,canonical_url,discovered_via) VALUES(?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,discovered_via=excluded.discovered_via', (source_id, urls.normalize(entry['discovered_url']), entry['canonical_url'], f'sitemap:{seed}'))
        for entry in discovered:
            if not _still_authorized(db, source_id):
                return _block(db, job_id, source_id, summary, 'source authorization withdrawn during crawl')
            try:
                outcome = _crawl_article(db, job_id, source_id, entry, allowed_origin, parser, profile, transport)
                summary[outcome] += 1
                if outcome == 'new_versions':
                    summary['ingested'] += 1
            except GateError as exc:
                _record_failure(db, job_id, source_id, entry['canonical_url'], exc)
                if exc.code == 'crawl_unauthorized' or not _still_authorized(db, source_id):
                    summary['blocked'].append(entry['canonical_url'])
                    return _block(db, job_id, source_id, summary, 'source authorization withdrawn during crawl')
                summary['failed'] += 1
    except GateError as exc:
        return _finish(db, job_id, 'failed', summary, exc.as_dict())
    return _finish(db, job_id, 'complete', summary)


def _crawl_article(db, job_id, source_id, entry, allowed_origin, parser, profile, transport):
    definition = parser['definition']
    chain = []
    visited = set()
    current = entry['canonical_url']
    discovered_via = entry['discovered_url']
    article_root = current
    while True:
        urls.ensure_inside(allowed_origin, current)
        if current in visited:
            fail('crawl_pagination_loop', 'Pagination chain revisits an already fetched page', url=current)
        visited.add(current)
        if len(chain) >= definition['max_pages']:
            fail('crawl_pagination_limit', 'Pagination chain exceeds the configured page limit', url=current, limit=definition['max_pages'])
        stored = db.one('SELECT * FROM crawl_pages WHERE source_id=? AND url=?', (source_id, current))
        condition = {}
        if stored:
            if stored['etag']:
                condition['etag'] = stored['etag']
            if stored['last_modified']:
                condition['last_modified'] = stored['last_modified']
        response = fetcher.fetch(transport, allowed_origin, current, condition, definition['max_bytes'])
        if response.status == 304:
            if stored is None:
                fail('crawl_conditional', 'Conditional response arrived without a stored original page', url=current)
            html_text = stored['raw_body'].decode('utf-8')
            etag, last_modified, content_type, encoding_label, body_hash = stored['etag'], stored['last_modified'], stored['content_type'], stored['encoding'], stored['body_sha256']
            reused = True
        else:
            if response.status != 200:
                fail('crawl_http_status', 'Page retrieval returned an error status', url=current, status=response.status)
            html_text, encoding_label = fetcher.decode(response)
            etag, last_modified, content_type = response.header('etag'), response.header('last-modified'), response.content_type()
            body_hash = digest([response.body.hex()])
            reused = False
        parsed = extractor.extract(html_text, current, definition)
        if parsed['canonical_url']:
            page_canonical = urls.normalize(urls.join(current, parsed['canonical_url']))
            urls.ensure_inside(allowed_origin, page_canonical)
        else:
            page_canonical = article_root
        if len(chain) == 0 and parsed['canonical_url']:
            article_root = page_canonical
        chain.append({'url': current, 'ordinal': len(chain), 'reused': reused, 'etag': etag, 'last_modified': last_modified, 'encoding': encoding_label, 'body_sha256': body_hash, 'content_hash': parsed['content_hash'], 'status': response.status})
        if not reused:
            with db.transaction():
                db.execute('''INSERT INTO crawl_pages(source_id,url,canonical_url,ordinal,status_code,etag,last_modified,content_type,encoding,raw_body,body_sha256,parser_revision,fetched_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,ordinal=excluded.ordinal,status_code=excluded.status_code,etag=excluded.etag,last_modified=excluded.last_modified,content_type=excluded.content_type,encoding=excluded.encoding,raw_body=excluded.raw_body,body_sha256=excluded.body_sha256,parser_revision=excluded.parser_revision,fetched_at=excluded.fetched_at''',
                    (source_id, current, article_root, len(chain) - 1, response.status, etag, last_modified, content_type, encoding_label, html_text.encode('utf-8'), body_hash, parser['revision'], db.clock()))
                db.execute('INSERT INTO crawl_urls(source_id,url,canonical_url,discovered_via) VALUES(?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,discovered_via=excluded.discovered_via', (source_id, current, article_root, discovered_via))
        if not parsed['next_url']:
            break
        next_url = urls.normalize(urls.join(current, parsed['next_url']))
        urls.ensure_inside(allowed_origin, next_url)
        current, discovered_via = next_url, f'next:{article_root}'
    return _persist_article(db, job_id, source_id, chain, article_root, parser, profile)


def _persist_article(db, job_id, source_id, chain, canonical_url, parser, profile):
    ordered = sorted(chain, key=lambda page: page['ordinal'])
    combined_hash = digest([page['content_hash'] for page in ordered])
    external_id = urls.external_id_for(canonical_url)
    with db.transaction():
        if not _still_authorized(db, source_id):
            fail('crawl_unauthorized', 'Source authorization withdrawn before content could be admitted', url=canonical_url)
        article = db.one('SELECT * FROM crawl_articles WHERE source_id=? AND canonical_url=?', (source_id, canonical_url))
        if article and article['latest_content_hash'] == combined_hash:
            db.execute('UPDATE crawl_articles SET last_crawled=?,parser_revision=? WHERE source_id=? AND canonical_url=?', (db.clock(), parser['revision'], source_id, canonical_url))
            for page in ordered:
                db.execute('INSERT INTO crawl_urls(source_id,url,canonical_url,discovered_via) VALUES(?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,discovered_via=excluded.discovered_via', (source_id, page['url'], canonical_url, f'job:{job_id}'))
            db.log('crawl.unchanged', {'job_id': job_id, 'source_id': source_id, 'canonical_url': canonical_url, 'content_hash': combined_hash})
            return 'unchanged'
        prior = documents.head(db, source_id, external_id)
        version = (prior['version'] + 1) if prior else 1
        blocks = _blocks(db, source_id, ordered, parser)
        text_value = extractor.serialize(blocks)
        if len(text_value) > 2000000:
            fail('document_text', 'Extracted article exceeds the document text limit')
        metadata = {
            'kind': 'crawled_article',
            'canonical_url': canonical_url,
            'original_urls': [page['url'] for page in ordered],
            'job_id': job_id,
            'crawl_parser_revision': parser['revision'],
            'profile_id': profile['profile_id'],
        }
        record = {'external_id': external_id, 'version': version, 'text': text_value, 'metadata': metadata}
        result = _admit(db, job_id, source_id, record, profile)
        db.execute('''INSERT INTO crawl_articles(source_id,canonical_url,external_id,document_id,latest_version,latest_content_hash,parser_revision,first_seen,last_crawled)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,canonical_url) DO UPDATE SET external_id=excluded.external_id,document_id=excluded.document_id,latest_version=excluded.latest_version,latest_content_hash=excluded.latest_content_hash,parser_revision=excluded.parser_revision,last_crawled=excluded.last_crawled''',
            (source_id, canonical_url, external_id, result['document']['document_id'], version, combined_hash, parser['revision'], db.clock(), db.clock()))
        db.execute('INSERT INTO crawl_evidence(source_id,canonical_url,version,job_id,parser_revision,content_hash,pages,created_at) VALUES(?,?,?,?,?,?,?,?)', (source_id, canonical_url, version, job_id, parser['revision'], combined_hash, canonical(ordered), db.clock()))
        for page in ordered:
            db.execute('INSERT INTO crawl_urls(source_id,url,canonical_url,discovered_via) VALUES(?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET canonical_url=excluded.canonical_url,discovered_via=excluded.discovered_via', (source_id, page['url'], canonical_url, f'job:{job_id}'))
        db.log('crawl.article_versioned', {'job_id': job_id, 'source_id': source_id, 'canonical_url': canonical_url, 'external_id': external_id, 'version': version, 'document_id': result['document']['document_id'], 'content_hash': combined_hash, 'duplicate': result['duplicate']})
        return 'unchanged' if result['duplicate'] else 'new_versions'


def _blocks(db, source_id, ordered, parser):
    blocks = []
    for page in ordered:
        stored = db.one('SELECT raw_body FROM crawl_pages WHERE source_id=? AND url=?', (source_id, page['url']))
        if stored is None:
            fail('crawl_evidence_missing', 'Stored original page is missing for re-extraction', url=page['url'])
        html_text = stored['raw_body'].decode('utf-8')
        blocks.extend(extractor.extract(html_text, page['url'], parser['definition'])['blocks'])
    return blocks


def reextract(db, source_id, parser_id=None, profile_id=None):
    """解析配置升级后，用原始抓取页重提取并留下新版本证据；正文未变则不产生版本。"""
    source = sources.get(db, source_id)
    if source['state'] != 'active':
        fail('source_revoked', 'Cannot re-extract pages for a revoked source')
    parser = parsers.ensure_default(db) if parser_id is None else parsers.get(db, parser_id)
    profile = profiles.get(db, profile_id or DEFAULT_PROFILE_ID)
    rows = db.all('SELECT canonical_url FROM crawl_articles WHERE source_id=? ORDER BY canonical_url', (source_id,))
    if not rows:
        fail('crawl_reextract_empty', 'Source has no crawled articles to re-extract')
    updated, unchanged = 0, 0
    for row in rows:
        canonical_url = row['canonical_url']
        pages = db.all('SELECT * FROM crawl_pages WHERE source_id=? AND canonical_url=? ORDER BY ordinal', (source_id, canonical_url))
        if not pages:
            pages = db.all('SELECT * FROM crawl_pages WHERE source_id=? AND url=? ORDER BY ordinal', (source_id, canonical_url))
        ordered = [{'url': page['url'], 'ordinal': page['ordinal'], 'reused': True, 'etag': page['etag'], 'last_modified': page['last_modified'], 'encoding': page['encoding'], 'body_sha256': page['body_sha256'], 'content_hash': extractor.extract(page['raw_body'].decode('utf-8'), page['url'], parser['definition'])['content_hash'], 'status': page['status_code']} for page in pages]
        chain_hash = digest([item['content_hash'] for item in ordered])
        article = db.one('SELECT * FROM crawl_articles WHERE source_id=? AND canonical_url=?', (source_id, canonical_url))
        if article['latest_content_hash'] == chain_hash:
            db.execute('UPDATE crawl_articles SET parser_revision=?,last_crawled=? WHERE source_id=? AND canonical_url=?', (parser['revision'], db.clock(), source_id, canonical_url))
            unchanged += 1
            continue
        outcome = _persist_article(db, f'reextract:{parser["parser_id"]}:{parser["revision"]}', source_id, ordered, canonical_url, parser, profile)
        if outcome == 'new_versions':
            updated += 1
        else:
            unchanged += 1
    _close_batch(db, f'reextract:{parser["parser_id"]}:{parser["revision"]}')
    db.log('crawl.reextracted', {'source_id': source_id, 'parser_id': parser['parser_id'], 'parser_revision': parser['revision'], 'updated': updated, 'unchanged': unchanged})
    return {'source_id': source_id, 'parser_id': parser['parser_id'], 'parser_revision': parser['revision'], 'articles': len(rows), 'new_versions': updated, 'unchanged': unchanged}


def _still_authorized(db, source_id):
    source = sources.get(db, source_id)
    if source['state'] != 'active':
        return False
    policy = source['policy']
    return db.clock() >= policy['valid_from'] and db.clock() < policy['valid_until']


def _record_failure(db, job_id, source_id, url, exc):
    with db.transaction():
        db.execute('INSERT INTO crawl_failures(job_id,source_id,url,stage,code,reason,at) VALUES(?,?,?,?,?,?,?)', (job_id, source_id, url, 'article', exc.code, canonical(exc.as_dict()), db.clock()))
        db.log('crawl.failure', {'job_id': job_id, 'url': url, 'code': exc.code})


def _block(db, job_id, source_id, summary, reason):
    return _finish(db, job_id, 'blocked', summary, {'code': 'source_revoked', 'message': reason})


def _finish(db, job_id, state, summary, fatal=None):
    _close_batch(db, job_id)
    with db.transaction():
        db.execute('UPDATE crawl_jobs SET state=?,summary=?,finished_at=? WHERE job_id=?', (state, canonical(summary), db.clock(), job_id))
        db.log('crawl.job_finished', {'job_id': job_id, 'state': state, 'summary': summary, 'fatal': fatal})
    return _job(db, job_id)


def _job(db, job_id):
    row = db.one('SELECT * FROM crawl_jobs WHERE job_id=?', (job_id,))
    if row is None:
        fail('crawl_job_missing', 'Crawl job does not exist')
    row['summary'] = json.loads(row['summary'])
    return row


def job(db, job_id):
    return _job(db, job_id)


def jobs(db):
    return [_job(db, row['job_id']) for row in db.all('SELECT job_id FROM crawl_jobs ORDER BY created_at,job_id')]


def failures(db, job_id):
    _job(db, job_id)
    rows = db.all('SELECT failure_id,job_id,source_id,url,stage,code,reason,at FROM crawl_failures WHERE job_id=? ORDER BY failure_id', (job_id,))
    for row in rows:
        row['reason'] = json.loads(row['reason'])
    return rows


def article(db, source_id, url):
    normalized = urls.normalize(url)
    mapping = db.one('SELECT * FROM crawl_urls WHERE source_id=? AND (url=? OR canonical_url=?)', (source_id, normalized, normalized))
    if mapping is None:
        article_row = db.one('SELECT * FROM crawl_articles WHERE source_id=? AND canonical_url=?', (source_id, normalized))
        if article_row is None:
            fail('crawl_url_unknown', 'No crawled document corresponds to this address', source_id=source_id, url=url)
        canonical_url = article_row['canonical_url']
    else:
        canonical_url = mapping['canonical_url']
    article_row = db.one('SELECT * FROM crawl_articles WHERE source_id=? AND canonical_url=?', (source_id, canonical_url))
    document = documents.head(db, source_id, article_row['external_id']) if article_row else None
    evidence = db.all('SELECT version,job_id,parser_revision,content_hash,pages,created_at FROM crawl_evidence WHERE source_id=? AND canonical_url=? ORDER BY version', (source_id, canonical_url))
    for item in evidence:
        item['pages'] = json.loads(item['pages'])
    return {
        'source_id': source_id,
        'requested_url': url,
        'canonical_url': canonical_url,
        'external_id': article_row['external_id'] if article_row else None,
        'document_id': document['document_id'] if document else None,
        'document': document,
        'evidence': evidence,
        'known_urls': [row['url'] for row in db.all('SELECT url FROM crawl_urls WHERE source_id=? AND canonical_url=? ORDER BY url', (source_id, canonical_url))],
    }


def articles(db, source_id):
    sources.get(db, source_id)
    rows = db.all('SELECT canonical_url,external_id,document_id,latest_version,latest_content_hash,parser_revision,first_seen,last_crawled FROM crawl_articles WHERE source_id=? ORDER BY canonical_url', (source_id,))
    for row in rows:
        row['document'] = documents.head(db, source_id, row['external_id'])
    return rows


def recrawl(db, source_id, url, context, parser_id=None, profile_id=None, transport=None):
    source = sources.get(db, source_id)
    decision = sources.authorize(db, source_id, context)
    if not decision['allowed']:
        fail('crawl_unauthorized', 'Source authorization does not permit this recrawl', authorization=decision)
    parser = parsers.ensure_default(db) if parser_id is None else parsers.get(db, parser_id)
    profile = profiles.get(db, profile_id or DEFAULT_PROFILE_ID)
    normalized = urls.normalize(url)
    allowed_origin = urls.origin(normalized)
    urls.ensure_inside(allowed_origin, normalized)
    entry = {'discovered_url': normalized, 'canonical_url': normalized}
    transport = transport or urllib_transport()
    batch_id = f'recrawl:{digest([source_id, normalized])[:16]}'
    try:
        outcome = _crawl_article(db, batch_id, source_id, entry, allowed_origin, parser, profile, transport)
    except GateError as exc:
        _record_failure(db, batch_id, source_id, normalized, exc)
        _close_batch(db, batch_id)
        raise
    _close_batch(db, batch_id)
    db.log('crawl.recrawled', {'source_id': source_id, 'url': normalized, 'outcome': outcome})
    return article(db, source_id, normalized)
