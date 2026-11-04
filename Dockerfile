FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS node

FROM public.ecr.aws/docker/library/python:3.13-slim

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates ripgrep openjdk-21-jdk-headless maven \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

ENV CHAGENT_EXECUTION_PROFILE=containerized \
    CHAGENT_BOUNDARY=container \
    CHAGENT_CONTAINER_DECLARED=true \
    CHAGENT_WORKSPACE_ROOT=/workspaces \
    CHAGENT_SANDBOX=off \
    CHAGENT_SANDBOX_NETWORK=true \
    CHAGENT_SANDBOX_WRITABLE=true \
    CHAGENT_CODE_MODE=on \
    CHAGENT_ALLOW_HOST_COMMANDS=true \
    CHAGENT_APPROVAL=never

EXPOSE 8765 8080
CMD ["uv", "run", "chagent", "--no-open", "--host", "0.0.0.0", "--workspace", "/workspaces"]
