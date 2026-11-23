"""Optional local viewer; Flask is imported only when explicitly opened."""
from pathlib import Path
from datetime import date
from threading import Timer
import secrets
import sqlite3
import webbrowser


def create_app(database, token=None):
    from flask import Flask, abort, jsonify, request, g
    from ..viewer import overview, detail
    database = Path(database).resolve()
    app = Flask(__name__)
    app.config.update(TRUSTED_HOSTS=['127.0.0.1', 'localhost'], MAX_CONTENT_LENGTH=16384,
                      VIEWER_TOKEN=token or secrets.token_urlsafe(32))

    @app.before_request
    def authenticate():
        from ..configuration import snapshot,resolve
        from ..extensions import effective
        g.behaviour=snapshot(effective(resolve()));g.behaviour.__enter__()
        if request.method not in {'GET', 'HEAD', 'POST'}:
            abort(405)
        if request.endpoint == 'static':
            return
        supplied = request.headers.get('X-Viewer-Token', '')
        if request.endpoint in {'index', 'dashboard'}:
            return  # The shell contains no records; the token stays in the URL fragment.
        if not secrets.compare_digest(supplied.encode(), app.config['VIEWER_TOKEN'].encode()):
            abort(401)
        if request.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
            abort(403)
        if request.method == 'POST':
            if request.headers.get('Origin') != request.host_url.rstrip('/'):
                abort(403)
            if not request.is_json:
                abort(415)

    @app.teardown_request
    def release_settings(error):
        scope=g.pop('behaviour',None)
        if scope:scope.__exit__(None,None,None)

    @app.after_request
    def private_response(response):
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                                 'Referrer-Policy':'no-referrer',
                                 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
        return response

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/dashboard')
    def dashboard():
        return app.send_static_file('dashboard.html')

    @app.get('/api/dashboard')
    def dashboard_data():
        from ..dashboard import read_window
        start = date.fromisoformat(request.args.get('start', ''))
        until = date.fromisoformat(request.args.get('until', ''))
        return jsonify(read_window(database, start, until))

    @app.get('/api/report/<run_id>')
    def dashboard_report(run_id):
        from ..dashboard import read_report
        report = read_report(database, run_id)
        if not report:
            abort(404)
        return jsonify(report)

    @app.get('/api/daily-reports/<day>')
    def dashboard_daily_reports(day):
        from ..dashboard import read_daily_reports
        return jsonify(read_daily_reports(database, date.fromisoformat(day)))

    @app.post('/api/local-events')
    @app.post('/api/appointment-ack')
    def dashboard_write():
        from contextlib import closing
        from ..core import Harness
        from ..dashboard import acknowledge_appointment, save_local_event
        operation = save_local_event if request.path == '/api/local-events' else acknowledge_appointment
        with closing(Harness(database)) as harness:
            return jsonify(operation(harness, request.get_json()))

    @app.get('/api/cards')
    def cards():
        return jsonify(overview(database))

    @app.post('/api/saved-items')
    def save_card():
        from contextlib import closing
        from ..core import Harness
        from ..card_input import save_item
        with closing(Harness(database)) as harness:
            return jsonify(save_item(harness, request.get_json()))

    @app.post('/api/calendar-visibility')
    def calendar_visibility():
        from contextlib import closing
        from ..core import Harness
        from ..card_input import set_calendar_visibility
        with closing(Harness(database)) as harness:
            return jsonify(set_calendar_visibility(harness, request.get_json()))

    @app.get('/api/card/<path:case_id>')
    def card(case_id):
        try:
            return jsonify(detail(database, case_id))
        except ValueError as error:
            return jsonify(error=str(error)), 404

    @app.post('/api/card/<path:case_id>/edit')
    def edit(case_id):
        from ..core import Harness
        from ..card_input import apply
        payload = request.get_json()
        if not isinstance(payload, dict) or payload.get('id') != case_id:
            raise ValueError('Card ID must match the request')
        h = Harness(database)
        try:
            result = apply(h, payload)
        finally:
            h.close()
        return jsonify(result)

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        return jsonify(error='Records are temporarily unavailable. Retry or check the configured database.'), 503

    @app.errorhandler(ValueError)
    def format_error(error):
        return jsonify(error=str(error)), 409

    return app


def serve(database, port=0, open_browser=True, dashboard=False, duration_minutes=None):
    if duration_minutes is not None and not 1 <= duration_minutes <= 1440:
        raise ValueError('Choose a viewer duration of 1–1440 minutes.')
    try:
        from waitress import create_server
        from ..viewer import overview
        overview(database)  # Fail before opening a browser; never initialise/migrate data.
        app = create_app(database)
    except ImportError:
        raise ValueError('Install the optional viewer with your project Python: -m pip install -r packaging/requirements/requirements-ui.lock') from None
    server = create_server(app, host='127.0.0.1', port=port, threads=4)
    page = '/dashboard' if dashboard else '/'
    url = f"http://127.0.0.1:{server.effective_port}{page}#token={app.config['VIEWER_TOKEN']}"
    print('Current threads workspace: ' + url, flush=True)
    print('Keep this process running while using the viewer; Ctrl+C stops it.', flush=True)
    if open_browser:
        webbrowser.open(url)
    expiry = Timer(duration_minutes * 60, server.close) if duration_minutes else None
    if expiry:
        expiry.daemon = True
        expiry.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if expiry:
            expiry.cancel()
        server.close()
