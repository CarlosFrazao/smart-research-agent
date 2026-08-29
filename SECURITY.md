# Security Policy

## Supported Versions

The project is currently under active development. Security patches are applied to the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | ✅ Yes             |
| < 1.0   | ❌ No              |

## Reporting a Vulnerability

We take security vulnerabilities seriously. Please report them responsibly.

### How to Report

**Do not open public GitHub issues for security vulnerabilities.**

Instead, please report security vulnerabilities through one of these channels:

1. **GitHub Security Advisory** (preferred): Use the [GitHub Security Advisories](https://github.com/owner/smart-research-agent/security/advisories) "Report a Vulnerability" button.
2. **Email**: Send details to `security@smart-research-agent.dev` (replace with actual contact).

### What to Include

When reporting a vulnerability, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept)
- The version/branch affected
- Any suggested mitigations or fixes (optional)

### Response Timeline

- **Initial response**: Within 48 hours
- **Triage and assessment**: Within 1 week
- **Patch development**: Depends on severity; critical issues prioritized
- **Public disclosure**: Coordinated with the reporter, typically after a patch is released

## Security Practices

### API Authentication

- Set `SRA_API_KEY` in `.env` for production use
- Without `SRA_API_KEY`, the REST API runs without authentication (dev mode only)
- In production (`SRA_ENV=production`), `validate_production()` enforces API key presence

### CORS

- Development: `["*"]` (allows all origins)
- Production: `[]` (no origins by default — configure `CORS_ALLOWED_ORIGINS`)

### Rate Limiting

- Configurable via `SRA_RATE_LIMIT` (default: `10/minute` per IP)
- Applied to all research endpoints

### Docker Compose

- Redis requires `REDIS_PASSWORD`
- Grafana requires `GRAFANA_ADMIN_PASSWORD`
- ChromaDB uses `CHROMA_AUTH_SECRET`

### Environment Variables

Never commit `.env` files. All secrets must be provided via environment variables or Docker secrets.

## Third-Party Dependencies

We use [Dependabot](https://docs.github.com/en/code-security/dependabot) to track and patch known vulnerabilities in dependencies. Security updates are reviewed and applied regularly.

## Acknowledgements

We appreciate responsible disclosure and the work of security researchers who help keep the project secure.