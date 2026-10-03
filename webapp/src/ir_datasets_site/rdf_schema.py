"""The RDF mapping shared by ``build_graph_db.py`` (writer) and
``graph_queries.py`` (reader) for materializing ``ir_datasets.v2``'s graph
(see ``Graph.export_triples()``) into a pyoxigraph store.

A node's qualified name (``irds:antique-docs``) becomes its IRI's local part
verbatim -- recovering it is just stripping a fixed prefix, no lookup table
needed. Two more namespaces keep "this is an edge to another node" and "this
is a literal property" visually and mechanically distinct, so a triples-table
query never needs to filter rdf:type/rdfs:subClassOf out by hand:

    NODE  http://ir-datasets.com/id/<qualified-name>       -- a node
    TYPE  http://ir-datasets.com/ns/type#<qualified-type>  -- a node's class
    EDGE  http://ir-datasets.com/ns/edge#<qualified-kind>  -- edge-kind predicate
    META  http://ir-datasets.com/ns/meta#<field-name>      -- literal-property predicate

``export_triples()``'s triples come as ``(subject, predicate, object)`` where
``predicate`` is either ``'type'``/``'subClassOf'`` (special-cased below) or a
qualified edge kind (when ``object`` is a plain qualified name) or a plain
field name (when ``object`` is a ``v2.Literal`` -- ir_datasets's own RDF-style
literal marker, unrelated to ``typing.Literal`` -- mapped under META).

Every quad also gets a **named graph**: a triple's own subject's provider
prefix (``irds``, ``hf``, ``clirmatrix``, ``legacy``), so the store's native
partitioning mechanism (not a predicate/property) is what the
provider-filter checkboxes query against. (CLIRMatrix is ~620k of the ~623k
nodes in the catalog -- large enough that it needing its own real
``clirmatrix:`` provider, split out from ``irds``, rather than being lumped
into it, is exactly what makes "hide CLIRMatrix by default" possible here
with no special-casing.)
"""
import logging

import pyoxigraph as ox
from ir_datasets.v2 import Literal as _V2Literal

logger = logging.getLogger(__name__)

NODE = 'http://ir-datasets.com/id/'
TYPE = 'http://ir-datasets.com/ns/type#'
EDGE = 'http://ir-datasets.com/ns/edge#'
META = 'http://ir-datasets.com/ns/meta#'
GRAPH = 'http://ir-datasets.com/provider/'

RDF_TYPE = ox.NamedNode('http://www.w3.org/1999/02/22-rdf-syntax-ns#type')
RDFS_SUBCLASS_OF = ox.NamedNode('http://www.w3.org/2000/01/rdf-schema#subClassOf')
XSD_INTEGER = ox.NamedNode('http://www.w3.org/2001/XMLSchema#integer')

#: Manifest-row fields that describe *attestation* (freeze --verify output),
#: not descriptive metadata -- kept in their own bucket so ``node_data()``
#: can hand templates the same shape ``Node._frozen()`` always did.
FROZEN_FIELDS = {'count', 'content_sha256', 'hash_scheme', 'record_schema',
                 'samples', 'commit', 'hashes_confirmed', 'score_counts'}


def node_iri(name):
    return ox.NamedNode(NODE + name)


def node_name(iri):
    """A ``NamedNode`` under ``NODE`` back to its qualified name, or ``None``
    if it isn't one (e.g. a type IRI turned up where a node was expected)."""
    value = iri.value if hasattr(iri, 'value') else str(iri)
    return value[len(NODE):] if value.startswith(NODE) else None


def type_iri(qualified_type):
    return ox.NamedNode(TYPE + qualified_type)


def type_name(iri):
    value = iri.value if hasattr(iri, 'value') else str(iri)
    return value[len(TYPE):] if value.startswith(TYPE) else None


def edge_iri(qualified_kind):
    return ox.NamedNode(EDGE + qualified_kind)


def edge_kind(iri):
    value = iri.value if hasattr(iri, 'value') else str(iri)
    return value[len(EDGE):] if value.startswith(EDGE) else None


def meta_iri(field):
    return ox.NamedNode(META + field)


def meta_field(iri):
    value = iri.value if hasattr(iri, 'value') else str(iri)
    return value[len(META):] if value.startswith(META) else None


def graph_iri(name):
    return ox.NamedNode(GRAPH + name)


def graph_name(iri):
    value = iri.value if hasattr(iri, 'value') else str(iri)
    return value[len(GRAPH):] if value.startswith(GRAPH) else None


def graph_for(qualified_name):
    """Which named graph ``qualified_name`` (a node, or -- for a
    ``subClassOf`` row -- a type) belongs to: its provider prefix."""
    return qualified_name.split(':', 1)[0]


def to_quads(triples):
    """``export_triples()``'s ``(subject, predicate, object)`` triples ->
    pyoxigraph ``Quad`` objects, per the mapping above -- each placed in its
    subject's named graph (``graph_for``).

    A malformed triple (e.g. a provider yielding a name/value that isn't a
    valid IRI component, such as a raw space -- see the ``trec-browser``
    provider) would otherwise surface as a bare ``ValueError: Invalid IRI
    code point ...`` with no indication of *which* triple, out of
    potentially hundreds of thousands, caused it. Logging + re-raising with
    the offending ``(subject, predicate, object)`` attached turns that into
    something actually actionable. Fetching the *next* triple can itself
    raise (a provider's own parsing logic failing before it ever produces a
    triple), so that's guarded too, separately from the conversion below."""
    it = iter(triples)
    i = 0
    while True:
        try:
            subject, predicate, obj = next(it)
        except StopIteration:
            return
        except Exception as e:
            logger.error('Failed to fetch triple #%d from the provider (%s: %s)',
                         i, type(e).__name__, e)
            raise
        try:
            s = node_iri(subject)
            g = graph_iri(graph_for(subject))
            if predicate == 'type':
                yield ox.Quad(s, RDF_TYPE, type_iri(obj), g)
            elif predicate == 'subClassOf':
                yield ox.Quad(type_iri(subject), RDFS_SUBCLASS_OF, type_iri(obj), g)
            elif isinstance(obj, _V2Literal):
                value = obj.value
                literal = ox.Literal(str(value), datatype=XSD_INTEGER) if isinstance(value, int) \
                    else ox.Literal(str(value))
                yield ox.Quad(s, meta_iri(predicate), literal, g)
            else:
                yield ox.Quad(s, edge_iri(predicate), node_iri(obj), g)
        except Exception as e:
            logger.error('Failed to convert triple #%d to a quad: subject=%r, predicate=%r, '
                         'object=%r (%s: %s)', i, subject, predicate, obj,
                         type(e).__name__, e)
            raise ValueError(
                f'Failed to convert triple #{i} to a quad: subject={subject!r}, '
                f'predicate={predicate!r}, object={obj!r} -- likely malformed data '
                f"from the {graph_for(subject)!r} provider, not a bug in this repo's "
                'own code (see rdf_schema.py docstring)'
            ) from e
        i += 1
