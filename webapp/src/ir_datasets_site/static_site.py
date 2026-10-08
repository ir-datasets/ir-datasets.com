"""Bakes this site down into plain static HTML + assets -- for hosting
anywhere a live Flask process isn't an option (GitHub Pages, S3, a plain
nginx static root, ...), at the cost of the two things that genuinely need a
live backend:

* Free-text/regex search (``/search?q=...&regex=1``) -- unbounded input,
  can't be enumerated into finitely many pages. Dropped entirely; the static
  build instead ships one pre-rendered page per type filter (``/search`` and
  ``/search/type/<type>``, see ``_search_url``), replacing the dropdown with
  plain links (see search.html's ``{% if rows is not none %}`` branch) and
  hiding the live navbar search box (see base.html's ``static_build``
  check).
* A node missing from the materialized snapshot (freshest hf: repos the
  last ``build-graph-db`` run missed) -- app.py's ``node()`` falls back to a
  live ``ir_datasets.v2`` resolution for that; a static export only ever
  knows what's in the store, so that fallback is simply never reached here
  (same node names it would have needed, just not actually written out).

Everything else (home page, every node page, every provider page, and the
type-filtered search pages above) is rendered once, up front, exactly as the
live app would, and written to disk -- see ``render_to_static_page`` (the
generic one-URL-to-one-file primitive) and ``build`` (the orchestrator that
calls it, and app.py's templates directly, for every page this site has).
"""
import shutil
from pathlib import Path

from flask import render_template
from tqdm import tqdm

from . import graph_queries as gq
from .app import (
    DEFAULT_HIDDEN_PROVIDERS, DUMP_PATH, TYPE_LABELS, TYPE_LABELS_PLURAL,
    _node_url, _row_dict, _selected_providers, _type_graph, app, store, v2,
)

#: Providers excluded from a static build unless explicitly asked for (via
#: ``--providers``) -- same ones hidden by default on the live home page
#: (see app.py's DEFAULT_HIDDEN_PROVIDERS), but enforced harder here:
#: hidden-by-default there is just an unchecked checkbox a visitor can flip
#: back on; here, enumerating CLIRMatrix's ~620k nodes would mean ~620k
#: individual HTML files, so it's left out of node/provider pages entirely,
#: not just the default listing.
DEFAULT_STATIC_PROVIDERS = tuple(
    p for p in sorted(v2.graph.providers) if p not in DEFAULT_HIDDEN_PROVIDERS)


def _url_to_out_path(out_dir, url):
    """Where a URL's response body is written under ``out_dir`` -- every
    page is ``<out_dir>/<url path, minus leading/trailing slashes>/index.html``
    (``/`` itself -> ``<out_dir>/index.html``), the same directory-per-page,
    extension-less-link convention static site generators (and Frozen-Flask)
    use, so every link this app already renders (``url_for(...)``, no
    trailing slash) keeps working unmodified against a plain static file
    server -- including ones like ``python -m http.server`` that resolve an
    extension-less request to that directory's ``index.html``."""
    url = url.split('#', 1)[0].split('?', 1)[0]
    trimmed = url.strip('/')
    return out_dir / trimmed / 'index.html' if trimmed else out_dir / 'index.html'


def _write_page(out_dir, url, html):
    path = _url_to_out_path(out_dir, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding='utf-8')
    return path


def render_to_static_page(client, url, out_dir):
    """The generic, reusable primitive this whole module is built on: GET
    ``url`` through ``client`` (a ``Flask.test_client()``) and write the
    response body to disk under ``out_dir`` (see ``_url_to_out_path`` for
    where) -- i.e. render whatever that URL would show a live visitor, once,
    to a plain file. Raises on anything but a 200 -- a static build should
    never silently drop a broken page instead of failing the build."""
    resp = client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f'{url} -> HTTP {resp.status_code} (static build cannot '
                            f'continue past a broken page)')
    return _write_page(out_dir, url, resp.get_data(as_text=True))


def _search_url(type_filter=''):
    """A type-filtered search page's static path -- distinct *path*
    segments, not the live app's ``?type=...`` query string, because a
    static file server can't tell two query strings on the same path apart
    (they'd resolve to the exact same file on disk). Passed into search.html
    as ``static_search_url`` (its Type filter links) and used directly here
    to name each rendered page."""
    return '/search' if not type_filter else f'/search/type/{type_filter}'


def _index_url_for(endpoint, **values):
    """Shadows the real ``url_for`` for index.html's own render only (passed
    in as a same-named context variable -- Jinja resolves a name from the
    render context before falling back to an environment global, for any
    reference the template body makes directly, which is all index.html
    does; see module docstring). Every endpoint except ``search`` -- the one
    query-string link a static export can't reuse unmodified -- is left to
    the real ``url_for`` (still needed, unaffected, for ``provider``/
    ``static`` links on the same page and for base.html's own ``index``/
    ``static`` links, since ``{% extends %}`` shares this same context)."""
    from flask import url_for
    if endpoint == 'search':
        return _search_url(values.get('type', ''))
    return url_for(endpoint, **values)


def _rows_for(type_filter=None, name_prefix=None, providers=None):
    rows, _total = gq.list_nodes(store, type_filter=type_filter, name_prefix=name_prefix,
                                 providers=providers, limit=None)
    return [_row_dict(name, types) for name, types in rows]


def build(out='dist', providers=None):
    """Writes a complete static copy of the site to ``out`` (removed first,
    if present, so stale pages from a previous run never linger). Pages:

    * ``/`` -- the home page, via ``render_to_static_page`` (a plain, already
      static-safe Flask route -- no live-only feature involved).
    * ``/n/<qualified>`` for every node in ``providers`` -- same mechanism.
    * ``/search`` and ``/search/type/<type>`` -- one pre-rendered listing per
      type filter (plus "all types"), rows inlined via search.html's static
      branch (rendered directly, not through the live ``/search`` route,
      since that route always passes ``rows=None`` -- see app.py's
      ``search()``).
    * ``/types`` and ``/type/<qualified>`` for every known node type.
    * ``/provider/<prefix>`` for every provider in ``providers`` -- same
      inlined-rows approach, via provider.html's static branch.
    * ``static/`` -- copied as-is (CSS; ``browse.js``/``nav.js`` are left out
      of the pages that would reference them below, so copying them too, if
      present, is harmless, not that anything still links to them from a
      static page).
    * ``graph.nq.gz`` -- copied as-is from alongside ``graph.db`` (see
      app.py's DUMP_PATH), if build-graph-db wrote one, so the same download
      link (base.html's footer) and the same URL (``/graph.nq.gz``) work
      whether this ends up served live or as a plain static file.

    ``providers``: which provider-graphs to actually enumerate into node and
    provider pages (and count towards the type-filtered listings) --
    defaults to ``DEFAULT_STATIC_PROVIDERS`` (every registered provider
    except CLIRMatrix/legacy; see its own docstring for why). Pass an
    explicit list (e.g. including ``'clirmatrix'``) to override -- understand
    that doing so means one HTML file per node in that provider, so
    CLIRMatrix's ~620k nodes become ~620k files."""
    out_dir = Path(out).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    selected = list(providers) if providers is not None else list(DEFAULT_STATIC_PROVIDERS)
    available = set(gq.available_providers(store))
    selected = [p for p in selected if p in available]

    with app.test_request_context():
        index_html = render_template(
            'index.html', counts=gq.counts_by_type(store, list(TYPE_LABELS), providers=selected),
            has_sidebar=False, providers=sorted(selected, key=lambda p: (p != 'irds', p)),
            type_labels=TYPE_LABELS_PLURAL, static_build=True, url_for=_index_url_for)
        _write_page(out_dir, '/', index_html)

        type_filters = [''] + list(TYPE_LABELS)
        for type_filter in type_filters:
            rows = _rows_for(type_filter=type_filter or None, providers=selected)
            html = render_template(
                'search.html', has_sidebar=True, type_filter_labels=TYPE_LABELS,
                type_filter=type_filter, query='', regex=False,
                available_providers=selected, selected_providers=selected,
                total=len(rows), rows=rows, static_search_url=_search_url,
                static_build=True)
            _write_page(out_dir, _search_url(type_filter), html)

        for prefix in tqdm(selected, desc='provider pages', unit='provider'):
            p = v2.graph.providers[prefix]
            rows = _rows_for(name_prefix=f'{prefix}:', providers=_selected_providers(scope={prefix}))
            html = render_template(
                'provider.html', provider=p, prefix=prefix, manifest=p.manifest(),
                type_labels=TYPE_LABELS, rows=rows, static_build=True)
            _write_page(out_dir, f'/provider/{prefix}', html)

    with app.test_request_context():
        names = [name for name, _type in gq.list_nodes(store, providers=selected, limit=None)[0]]
        # _node_url (-> url_for) percent-encodes whatever needs it (a
        # literal space, '%', '+', ...) -- a raw f'/n/{name}' doesn't, so a
        # name containing any of those wouldn't round-trip back to the same
        # string once a URL parser (Werkzeug's routing here, a browser's
        # address bar later) decodes it again. Built up front, in this one
        # request context, rather than inside the loop below -- test_client
        # requests each push/pop their own request context per call, which
        # doesn't nest cleanly with one already left open around them.
        node_urls = [(name, _node_url(name)) for name in names]

    with app.test_request_context():
        # The overview and one page per known node type (see app.py's types()/type_page()).
        type_urls = ['/types'] + [url_for('type_page', qualified=t) for t in sorted(_type_graph()[0])]

    with app.test_client() as client:
        for name, url in tqdm(node_urls, desc='node pages', unit='page'):
            render_to_static_page(client, url, out_dir)
        for url in type_urls:
            render_to_static_page(client, url, out_dir)

    static_src = Path(__file__).resolve().parent / 'static'
    if static_src.is_dir():
        shutil.copytree(static_src, out_dir / 'static', dirs_exist_ok=True)

    if DUMP_PATH.exists():
        # Same URL (/graph.nq.gz) works whether this ends up served by
        # app.py's route or, here, as a plain file -- see base.html's
        # footer link, shared by both.
        shutil.copyfile(DUMP_PATH, out_dir / 'graph.nq.gz')

    return out_dir
