# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's security advisory
workflow. Do not open a public issue containing credentials, private source
material, exploit details, or project runtime data.

## Security boundary

Agent Recipes stores project state under `.recipes/` and may install local agent
configuration when explicitly requested. Optional cloud adapters use credentials
from environment variables. Never commit `.recipes/`, user configuration,
credentials, or adapter caches.

The project is pre-1.0. Security fixes target the latest released version.

