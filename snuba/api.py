import simplejson as json
from clickhouse_driver import Client
from dateutil.parser import parse as parse_datetime
from flask import Flask, render_template, request
from markdown import markdown
from raven.contrib.flask import Sentry

from snuba import settings, util, schemas


clickhouse = Client(
    host=settings.CLICKHOUSE_SERVER.split(':')[0],
    port=int(settings.CLICKHOUSE_SERVER.split(':')[1]),
    connect_timeout=1,
)

app = Flask(__name__)
app.testing = settings.TESTING
app.debug = settings.DEBUG

sentry = Sentry(app, dsn=settings.SENTRY_DSN)


@app.route('/')
def root():
    with open('README.md') as f:
        return render_template('index.html', body=markdown(f.read()))


@app.route('/health')
def health():
    assert settings.CLICKHOUSE_TABLE in clickhouse.execute('show tables')[0]
    return ('ok', 200, {'Content-Type': 'text/plain'})


# TODO if `issue` or `time` is specified in 2 places (eg group and where),
# we redundantly expand it twice
# TODO some aliases eg tags[foo] are invalid SQL
# and need escaping or otherwise sanitizing


@app.route('/query', methods=['GET', 'POST'])
@util.validate_request(schemas.QUERY_SCHEMA)
def query():
    body = request.validated_body

    to_date = parse_datetime(body['to_date'])
    from_date = parse_datetime(body['from_date'])
    assert from_date <= to_date

    conditions = body['conditions']
    conditions.extend([
        ('timestamp', '>=', from_date),
        ('timestamp', '<=', to_date),
        ('project_id', 'IN', util.to_list(body['project'])),
    ])

    aggregate_columns = [
        util.column_expr(col, body, alias, agg)
        for (agg, col, alias) in body['aggregations']
    ]
    groupby = util.to_list(body['groupby'])
    group_columns = [util.column_expr(gb, body) for gb in groupby]

    select_columns = group_columns + aggregate_columns
    select_clause = 'SELECT {}'.format(', '.join(select_columns))
    from_clause = 'FROM {}'.format(settings.CLICKHOUSE_TABLE)
    join_clause = 'ARRAY JOIN {}'.format(body['arrayjoin']) if 'arrayjoin' in body else ''

    where_clause = ''
    if conditions:
        where_clause = 'WHERE {}'.format(util.condition_expr(conditions, body))

    group_clause = ', '.join(util.column_expr(gb, body) for gb in groupby)
    if group_clause:
        group_clause = 'GROUP BY ({})'.format(group_clause)

    order_clause = ''
    desc = body['orderby'].startswith('-')
    orderby = body['orderby'].lstrip('-')
    if orderby in body.get('alias_cache', {}).values():
        order_clause = 'ORDER BY `{}` {}'.format(orderby, 'DESC' if desc else 'ASC')

    limit_clause = ''
    if 'limit' in body:
        limit_clause = "LIMIT {}, {}".format(body.get('offset', 0), body['limit'])

    sql = ' '.join([c for c in [
        select_clause,
        from_clause,
        join_clause,
        where_clause,
        group_clause,
        order_clause,
        limit_clause
    ] if c])

    result = util.raw_query(sql, clickhouse)
    return (json.dumps(result), 200, {'Content-Type': 'application/json'})


if app.debug or app.testing:
    # These should only be used for testing/debugging. Note that the database name
    # is hardcoded to 'test' on purpose to avoid scary production mishaps.
    TEST_TABLE = 'test'

    @app.route('/tests/insert', methods=['POST'])
    def write():
        from snuba.processor import process_raw_event
        from snuba.writer import row_from_processed_event, write_rows

        clickhouse.execute(util.get_table_definition(TEST_TABLE, 'Memory', settings.SCHEMA_COLUMNS))

        body = json.loads(request.data)

        rows = []
        for event in body:
            processed = process_raw_event(event)
            row = row_from_processed_event(processed)
            rows.append(row)

        write_rows(clickhouse, table=TEST_TABLE, columns=settings.WRITER_COLUMNS, rows=rows)
        return ('ok', 200, {'Content-Type': 'text/plain'})

    @app.route('/tests/drop', methods=['POST'])
    def drop():
        clickhouse.execute("DROP TABLE IF EXISTS %s" % TEST_TABLE)
        return ('ok', 200, {'Content-Type': 'text/plain'})
