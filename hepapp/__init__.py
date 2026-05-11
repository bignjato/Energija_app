"""HEP + SMA Energy Dashboard — Flask app factory."""

import logging
import os

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .core import init_limiter, register_before_request
from .db import init_db

log = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _secret = os.environ.get('SECRET_KEY', '').strip()
    if not _secret or _secret in ('promijenite-ovo', 'change-me'):
        raise RuntimeError(
            'SECRET_KEY nije postavljen u .env (ili je placeholder). '
            'Postavi 64-hex random key.'
        )
    if len(_secret) < 32:
        raise RuntimeError(f'SECRET_KEY prekratak ({len(_secret)} chars). Treba ≥32.')
    app.secret_key = _secret

    app.config['SESSION_COOKIE_SECURE']      = os.environ.get('SESSION_COOKIE_SECURE', '1') != '0'
    app.config['SESSION_COOKIE_HTTPONLY']    = True
    app.config['SESSION_COOKIE_SAMESITE']    = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    init_limiter(app)
    register_before_request(app)

    with app.app_context():
        try:
            init_db()
        except Exception as e:
            app.logger.exception(f'Init DB error: {e}')

    # Register blueprints
    from .auth import bp as auth_bp
    from .blueprints.views    import bp as views_bp
    from .blueprints.data     import bp as data_bp
    from .blueprints.stats    import bp as stats_bp
    from .blueprints.tarifa   import bp as tarifa_bp
    from .blueprints.racuni   import bp as racuni_bp
    from .blueprints.postavke import bp as postavke_bp
    from .blueprints.setup    import bp as setup_bp
    from .blueprints.wizard   import bp as wizard_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(tarifa_bp)
    app.register_blueprint(racuni_bp)
    app.register_blueprint(postavke_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(wizard_bp)

    return app
