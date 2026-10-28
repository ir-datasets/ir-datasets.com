# attest: the attestation server

Behind `https://attest.ir-datasets.com`. A small WSGI app over SQLite (stdlib only, apart from
`ir_datasets`, whose `ir_datasets.v2.attest` it shares record validation and signature checking
with). The client, CLI and graph provider live in core ir_datasets (`ir_datasets.v2.attest`).

```
POST /register                       create an IdentityAttestation (self-signed)
POST /{hash,visual,evaluation}       create an attestation; body {record, signature?, signed_by?}
GET  /[?full=1&after=N&limit=M]      attestations after seq N: id+type, or whole records with full=1
GET  /<id>,<id>,...                  those records
GET  /<prefix:name>                  every attestation about a node
```

A record carries its own `kind` and `subject`; a signature (Ed25519) covers the record as canonical
JSON. Visual attestations must be signed. Bodies are limited to 64 KB. There is no rate limiting or
auth beyond signatures, and `wsgiref` serves one request at a time: fine to start, put a real WSGI
server (`App` in `server.py`) in front of it for deployment.

```bash
pip install -e .
ir-datasets-attest-server --db attest.db --port 8000
IR_DATASETS_ATTEST_URL=http://127.0.0.1:8000 ir_datasets attest register alice
python -m unittest discover -s tests
```
