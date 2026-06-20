FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Install Claude CLI (claude -p is used by opus_bridge.py for planning/review).
# The npm install method works for most Linux base images; verify the exact
# package name/version against the Claude CLI release in use.
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || \
    echo "Claude CLI npm install skipped (npm not available)"

# Install superpowers skills for in-product brainstorming/writing-plans.
# NOTE: 'claude plugin install' subcommand is version-sensitive — verify it
# matches the installed CLI version before relying on this in production.
RUN claude plugin install superpowers || echo "superpowers install deferred to runtime"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY web/ web/
RUN uv pip install --system -e ".[dev]" --no-cache

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"]
