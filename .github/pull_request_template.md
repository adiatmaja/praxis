## Summary

What does this PR change and why?

## Changes

-
-

## Testing

How was this verified?

```
uv run pytest --cov=orchestrator --cov-report=term-missing
```

## Checklist

- [ ] Tests pass (`uv run pytest`)
- [ ] Lint/format clean (`uv run ruff check src/ tests/` and `uv run ruff format --check src/ tests/`)
- [ ] Type check clean (`uv run mypy src/orchestrator/ --ignore-missing-imports`)
- [ ] Docs updated if behavior changed
- [ ] No secrets or credentials committed
