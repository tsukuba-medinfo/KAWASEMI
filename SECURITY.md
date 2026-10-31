# Security policy

KAWASEMI is research software that is designed to run inside a hospital network and to handle
data derived from medical records. Please report security problems privately.

## Reporting a vulnerability

- Use GitHub's **private vulnerability reporting** for this repository
  ("Security" tab → "Report a vulnerability"). Do not open a public issue.
- Describe the problem, the affected version or commit, and how to reproduce it.
- **Do not include patient data, real hospital identifiers or credentials** in the report.
  Use synthetic data (for example the fictional scenario in `examples/e2e/`).

We will acknowledge the report and coordinate a fix and disclosure with you.

## Supported versions

Only the latest commit on `main` receives fixes.

## Deployment notes

- Keep `site/` (facility data and configuration) and `.env` out of version control of this
  repository; store secrets in environment variables referenced via `secret_ref: env:NAME`.
- Run LLM servers and the application inside the facility network; KAWASEMI does not need
  outbound internet access at runtime.
