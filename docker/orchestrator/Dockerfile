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

# Node.js is required for the Claude CLI (claude -p is used by opus_bridge.py
# for planning/review). The slim base has no node/npm, so install it first —
# otherwise the claude-code install below silently no-ops and every Opus call
# fails at runtime with FileNotFoundError.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI (claude -p is used by opus_bridge.py for planning/review).
RUN npm install -g @anthropic-ai/claude-code

# Install superpowers skills for in-product brainstorming/writing-plans.
# NOTE: 'claude plugin install' subcommand is version-sensitive — verify it
# matches the installed CLI version before relying on this in production.
RUN claude plugin install superpowers || echo "superpowers install deferred to runtime"

# Configure a global git credential helper so any `git push` made inside the
# container — including pushes claude performs from a brainstorm workspace —
# authenticates with the runtime GITHUB_TOKEN without baking it into the image.
# Also set a default identity so claude's commits don't fail on a fresh repo.
RUN git config --global credential.helper \
    '!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f' \
    && git config --global user.email "praxis@orchestrator.local" \
    && git config --global user.name "Praxis Orchestrator"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY web/ web/
RUN uv pip install --system -e ".[dev]" --no-cache

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"]
