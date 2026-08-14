FROM node:20-bookworm-slim

# git, gh, curl, python3 for the entrypoint helpers.
#
# python3-pytest and python-is-python3 go into the SYSTEM interpreter on
# purpose. The verify command a worker is handed is `python -m pytest -q`, and
# it is also what a worker types by reflex, so pytest has to be importable by
# whatever `python` resolves to. Debian provides no `python` name at all
# without python-is-python3, and neither uv path gives one: `uv tool install
# pytest` hides it in an isolated venv `python -m pytest` cannot see, and
# `uv run pytest` needs a project venv a freshly cloned repo does not have.
# Measured live 2026-08-14 on psf__requests-2148: the worker spent turns on
# `uv run pytest`, then `uv run python -m pytest`, then gave up and edited
# source files instead of running tests.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg python3 python-is-python3 python3-pytest \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install OpenCode CLI globally
RUN npm install -g opencode-ai

# Dev toolchain so the worker can self-lint/test what the verify gate lints
# (ruff/mypy/pytest via uv). Installed to /usr/local/bin (root-writable here).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root agent user; workspace under its home (matches aider-agent pattern)
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
RUN chmod +x /home/agent/entrypoint.sh

COPY --chown=agent:agent extract_session.py /usr/local/bin/extract_session.py
ENV XDG_DATA_HOME=/home/agent/.local/share

# Create the session directory HERE, as the agent user, because the orchestrator
# mounts a fresh per-task named volume over it (OPENCODE_SESSIONS_VOLUME).
# Docker only copies image content and OWNERSHIP into an empty volume when the
# mount point already exists in the image; when it does not, Docker creates the
# path root-owned and this container runs as uid 1001, so OpenCode dies on
#   EACCES: permission denied, mkdir '/home/agent/.local/share/opencode/log'
# before it does any work at all. Measured live on 2026-08-08: every run failed
# in about 15 seconds with worker_tokens 0.
RUN mkdir -p /home/agent/.local/share/opencode/log

ENTRYPOINT ["/home/agent/entrypoint.sh"]
