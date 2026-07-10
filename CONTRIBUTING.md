# Contributing

Agent Recipes accepts focused changes that strengthen governed experience reuse
without weakening review gates, no-match behavior, source traceability, or
fail-closed execution.

## Development

```bash
python3 -m unittest discover -s tests
python3 -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist/wheels
./bin/verify-clean-install dist/wheels/agent_recipes_local-*.whl
```

Please keep changes small, add tests for behavioral changes, and document what
the evidence proves and what it does not prove. New adapters must remain
optional and may not bypass candidate review or write formal recipes directly.

## Pull requests

Describe the problem, the behavioral change, the verification performed, and
any remaining claim boundary. Do not include private project data, credentials,
runtime state, or unreviewed source material.

