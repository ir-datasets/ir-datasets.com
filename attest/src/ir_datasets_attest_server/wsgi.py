"""WSGI entry point for production servers: ``gunicorn ir_datasets_attest_server.wsgi:application``.

The SQLite file is taken from ``ATTEST_DB`` (default ``attest.db``).
"""
import os

from . import server, store

application = server.App(store.Store(os.environ.get('ATTEST_DB', 'attest.db')))
