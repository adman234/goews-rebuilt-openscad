# Self-hosted GOEWS part generator.
#
#   docker compose up -d --build
#
# then open http://localhost:8000/

# OpenSCAD nightly (the server uses the Manifold backend, which the
# distribution packages do not have yet).
ARG OPENSCAD_IMAGE=openscad/openscad:trixie.2026-11-02
# The full (non-slim) Node image includes git, which npm needs below.
ARG NODE_IMAGE=node:22-bookworm

#
# Frontend
#
FROM ${NODE_IMAGE} AS frontend
# One dependency (three-perf) is pinned to a GitHub commit over SSH; fetch it
# over HTTPS instead so the build doesn't need SSH keys.
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

#
# Server
#
FROM ${OPENSCAD_IMAGE}

# BOSL2 has no versioned releases; pin a known-good commit.
ARG BOSL2_REF=2d23674aeb9509ec57e740d648b22ca966fb7afc
# Set to false to skip STEP export and the ~300 MB OpenCascade dependency.
ARG WITH_STEP=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

ADD https://github.com/BelfrySCAD/BOSL2/archive/${BOSL2_REF}.tar.gz /tmp/bosl2.tar.gz
RUN mkdir -p /usr/local/share/openscad/libraries/BOSL2 \
    && tar -xzf /tmp/bosl2.tar.gz --strip-components=1 \
        -C /usr/local/share/openscad/libraries/BOSL2 \
    && rm /tmp/bosl2.tar.gz

WORKDIR /srv/goews
COPY server/requirements.txt server/requirements.txt
RUN python3 -m venv /srv/venv \
    && /srv/venv/bin/pip install --no-cache-dir -r server/requirements.txt \
    && if [ "${WITH_STEP}" = "true" ]; then \
         /srv/venv/bin/pip install --no-cache-dir cadquery-ocp; \
       fi

COPY *.scad ./
COPY server/ server/
COPY --from=frontend /src/frontend/dist frontend/dist

# Nothing under the app directory is writable by the service user.
RUN useradd --system --no-create-home goews
USER goews

# Large stacks (and the optional STEP export) can take a few minutes to
# build; Sanic's default 60 s response timeout is too short for them.
ENV PATH=/srv/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SANIC_RESPONSE_TIMEOUT=1800
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

ENTRYPOINT []
CMD ["sanic", "server.server", "--host", "0.0.0.0", "--port", "8000"]
