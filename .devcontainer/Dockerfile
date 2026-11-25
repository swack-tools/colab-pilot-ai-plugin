# syntax=docker/dockerfile:1
FROM public.ecr.aws/docker/library/ubuntu:24.04@sha256:562111b88caa79c71391c0c4d3c2114a7d5c35915c20561fd893cb1e07929e6b

ARG TARGETARCH
ARG APT_SNAPSHOT=20261120T000000Z
ARG NODE_VERSION=24.22.3
ARG NODE_SHA256_AMD64=4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb
ARG NODE_SHA256_ARM64=6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c

LABEL org.opencontainers.image.title="Colab Pilot Dev & CI Container" \
      org.opencontainers.image.description="Multi-arch Ubuntu 24.04 development and GitHub Actions replay container for Colab Pilot." \
      org.opencontainers.image.source="https://github.com/swack-tools/colab-pilot-plugin" \
      org.opencontainers.image.licenses="Apache-2.0"

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get -o "Acquire::Snapshot=${APT_SNAPSHOT}" update \
    && apt-get -o "Acquire::Snapshot=${APT_SNAPSHOT}" install -y --no-install-recommends \
        ca-certificates=20260816ubuntu0.24.04.2 \
        curl=8.5.0-2ubuntu10.16 \
        git=1:2.43.0-1ubuntu7.4 \
        jq=1.7.1-3ubuntu0.24.04.2 \
        openssh-client=1:9.6p1-3ubuntu13.20 \
        python3=3.12.3-0ubuntu2.1 \
        sudo=1.9.15p5-3ubuntu5.24.04.4 \
        tar=1.35+dfsg-3ubuntu0.4 \
        unzip=6.0-28ubuntu4.1 \
        xz-utils=5.6.1+really5.4.5-1ubuntu0.4 \
        zip=3.0-13ubuntu0.2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'ubuntu ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

RUN case "${TARGETARCH}" in \
        amd64) NODE_ARCH="x64"; NODE_SHA256="${NODE_SHA256_AMD64}" ;; \
        arm64) NODE_ARCH="arm64"; NODE_SHA256="${NODE_SHA256_ARM64}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location --retry 3 \
        --output /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
    && printf '%s  /tmp/node.tar.xz\n' "${NODE_SHA256}" | sha256sum --check - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm -f /tmp/node.tar.xz \
    && node --version \
    && npm --version

WORKDIR /workspace
