# syntax=docker/dockerfile:1
# Stack «java»: JDK 25 (Temurin) + Node 24 + python3 + Docker CLI (buildx, compose) + the g9s runner.
# Built for linux/amd64 only (the nodes it runs on are x86_64). Base images are
# pinned by digest; bump the digest and the comment together.

# node:24-bookworm-slim, digest taken 2026-10-21 (linux/amd64 manifest list)
FROM node:24-bookworm-slim@sha256:91acba91b842bec0bf7ea6f5e09c644c7ccad72c4996ed9616d861d1c7cd60d4 AS node

# docker:28-cli, digest taken 2026-10-21 (linux/amd64 manifest list)
FROM docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS dockercli

# eclipse-temurin:25-jdk-noble, digest taken 2026-10-21 (linux/amd64 manifest list)
FROM eclipse-temurin:25-jdk-noble@sha256:ba0f0e1a33864105e1d454e88105c617ab40f71062db1f306bf58be32a3f8e5e

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl python3 tar gzip xz-utils unzip \
 && rm -rf /var/lib/apt/lists/*

# Node: the official image's binary and npm, no package manager repo needed.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node /usr/local/include/node /usr/local/include/node
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Docker CLI + buildx plugin; the daemon is a sibling container (DOCKER_HOST).
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# UTF-8 locale: Maven Enforcer rules in some projects reject POSIX file-name encoding.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    G9S_RUNNER_DATA=/data \
    G9S_RECIPES=/opt/g9s/recipes \
    G9S_RECIPE_FILES=/etc/g9s/files \
    G9S_RUNNER_PORT=8181

COPY runner /opt/g9s/runner

VOLUME /data
EXPOSE 8181
ENTRYPOINT ["python3", "-I", "/opt/g9s/runner/g9s_runner.py"]
