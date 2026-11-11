import json
from corpus_gate.errors import identifier, integer, digest, canonical, fail
from corpus_gate.crawl.extractor import validate_definition, DEFAULT_DEFINITION

DEFAULT_PARSER_ID = 'default'


def put(db, parser_id, expected_revision, definition):
    identifier(parser_id)
    integer(expected_revision, 'expected_revision')
    normalized = validate_definition(definition)
    sha = digest(normalized)
    with db.transaction():
        previous = db.one('SELECT revision FROM crawl_parsers WHERE parser_id=?', (parser_id,))
        actual = previous['revision'] if previous else 0
        if expected_revision != actual:
            fail('crawl_parser_stale', 'Parser revision changed')
        revision = actual + 1
        db.execute('INSERT INTO crawl_parsers VALUES(?,?,?,?) ON CONFLICT(parser_id) DO UPDATE SET revision=excluded.revision,definition=excluded.definition,digest=excluded.digest', (parser_id, revision, canonical(normalized), sha))
        db.execute('INSERT INTO crawl_parser_history VALUES(?,?,?,?)', (parser_id, revision, canonical(normalized), sha))
        db.log('crawl.parser_saved', {'parser_id': parser_id, 'revision': revision, 'sha256': sha})
        return get(db, parser_id)


def get(db, parser_id, revision=None):
    if revision is None:
        row = db.one('SELECT * FROM crawl_parsers WHERE parser_id=?', (parser_id,))
    else:
        integer(revision, 'revision', 1)
        row = db.one('SELECT * FROM crawl_parser_history WHERE parser_id=? AND revision=?', (parser_id, revision))
    if row is None:
        fail('crawl_parser_missing', 'Crawl parser version does not exist')
    row['definition'] = json.loads(row['definition'])
    if digest(row['definition']) != row['digest']:
        fail('crawl_parser_corrupt', 'Crawl parser digest mismatch')
    return row


def listing(db):
    return [get(db, row['parser_id']) for row in db.all('SELECT parser_id FROM crawl_parsers ORDER BY parser_id')]


def ensure_default(db):
    if not db.one('SELECT parser_id FROM crawl_parsers WHERE parser_id=?', (DEFAULT_PARSER_ID,)):
        put(db, DEFAULT_PARSER_ID, 0, DEFAULT_DEFINITION)
    return get(db, DEFAULT_PARSER_ID)
