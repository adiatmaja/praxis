FROM debian:bookworm-slim

# git, gh, curl, ca-certificates, python3 for entrypoint helpers.
# gnupg needed to verify the GitHub CLI apt key.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg python3 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Dev toolchain so the worker can self-lint/test what the verify gate lints
# (ruff/mypy/pytest via uv). Installed to /usr/local/bin (root-writable here).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root agent user; workspace under its home.
RUN useradd -m -s /bin/bash agent
USER agent
WORKDIR /home/agent

# Install agy (Google Antigravity CLI) via the official Linux installer.
# The Go binary lands at ~/.local/bin/agy; add that to PATH.
# The installer may also write to ~/.config/agy — all within the agent home.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

ENV PATH="/home/agent/.local/bin:${PATH}"

COPY --chown=agent:agent entrypoint.sh /home/agent/entrypoint.sh
RUN chmod +x /home/agent/entrypoint.sh

# extract_session.py is invoked as `python3 /usr/local/bin/extract_session.py`
# (never executed directly), so it only needs to be readable by the agent user;
# COPY runs as root internally regardless of the active USER, so writing under
# /usr/local/bin here is fine even after the USER agent switch above.
COPY --chown=agent:agent extract_session.py /usr/local/bin/extract_session.py

ENTRYPOINT ["/home/agent/entrypoint.sh"]
