# syntax=docker/dockerfile:1

# Build the wheel in one stage so the runtime image carries no build tooling.
FROM python:3.14-slim AS build

WORKDIR /build
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install build

COPY pyproject.toml README.md LICENSE ./
COPY mqtt_atmowx_bridge ./mqtt_atmowx_bridge
RUN python -m build --wheel --outdir /dist


FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 bridge

COPY --from=build /dist/*.whl /tmp/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /tmp/*.whl && rm /tmp/*.whl

# The session file lives here; mount a volume so a restart does not have to
# re-authenticate with the app password.
RUN mkdir -p /data && chown bridge:bridge /data
VOLUME ["/data"]

USER bridge
WORKDIR /home/bridge

ENV ATP_SESSION_FILE=/data/session.json

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["mqtt-atmowx-bridge"]
CMD ["run", "--config", "/config/config.yaml"]
