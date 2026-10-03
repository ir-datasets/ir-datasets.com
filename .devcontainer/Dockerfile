# docker build -t ghcr.io/ir-datasets/ir-datasets.com:0.0.1-dev -f .devcontainer/Dockerfile .
FROM python:3.11-slim

# System dependencies required to build ir_datasets' own dependencies (lxml
# needs libxml2/libxslt headers, pyoxigraph needs a C/Rust toolchain if no
# prebuilt wheel matches) plus git, needed to pip-install ir_datasets.v2
# straight from its branch below (same approach as
# ../approval-tests-ir-datasets/.devcontainer/Dockerfile, pointed at the
# upstream repo's v2 branch, where the v2 prototype currently lives).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

WORKDIR /workspaces/ir-datasets.com

# Installs this app's console script (`ir-datasets-site`) together with its
# only non-PyPI dependency, ir_datasets.v2 (pulled straight from its branch,
# pinned in webapp/pyproject.toml) -- editable, so local edits under
# ./webapp while this image is used as a dev container are picked up
# without reinstalling.
COPY webapp ./webapp
RUN pip install --no-cache-dir -e ./webapp

# Additional provider, not bundled with ir_datasets itself: adds the
# trec-browser dataset provider used by this webapp's `trec-browser` graph.
RUN pip install --no-cache-dir git+https://github.com/ir-datasets/trec-browser-provider.git
