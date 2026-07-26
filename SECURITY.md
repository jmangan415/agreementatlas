# Security policy

## Supported version

AgreementAtlas is pre-1.0 alpha software. Security fixes are applied to the latest
version on the default branch only.

## Reporting a vulnerability

After this repository is published, use GitHub’s private vulnerability
reporting/security-advisory feature. Do not open a public issue for:

- cross-session document or graph access;
- path traversal or arbitrary file access;
- upload parser or archive-expansion attacks;
- document text appearing in logs or another session;
- cross-site mutation of a visitor workspace;
- exposed model tokens or provider credentials; or
- a way to bypass deletion, expiry, quotas or model concurrency limits.

Before publication, the repository owner should enable private vulnerability
reporting and replace this paragraph with a monitored security contact.

Include the affected version, deployment mode, reproduction steps and impact.
Use only fictional sample agreements; never attach a real contract, token or
personal information.

## Threat model

AgreementAtlas assumes that uploaded files and extracted text are untrusted.

The local/demo controls include:

- random, isolated, short-lived workspaces;
- safe filenames and fixed supported extensions;
- file signatures, Office archive validation and an expanded-size ceiling;
- atomic ingestion and immediate whole-session deletion;
- no execution of uploaded content;
- no cloud model in the default profile;
- restrictive browser headers and safe DOM text insertion;
- same-origin mutation checks and sliding-window request limits;
- one concurrent model request by default; and
- API errors that exclude filesystem paths and internal exception details.

The current release does not claim hardened multi-tenant sandboxing. Document
conversion still occurs inside the application process. Do not expose
anonymous uploads to sustained untrusted traffic without separate sandboxed
workers, gateway limits, malware controls and an independent security review.

## Secrets and sensitive data

Never commit:

- `.env` or LM Studio/cloud-provider tokens;
- visitor workspaces under `data/`;
- generated `knowledge/` data;
- real customer agreements or source PDFs;
- questions, answers or extracted clauses from real documents; or
- logs from a real demonstration.

If a secret is committed, revoke it before removing it from history.

## Dependency and release practice

- Keep Python and LM Studio on supported security releases.
- Review MarkItDown and its format-specific transitive dependencies.
- Run the CI workflow before publishing a release.
- Pin deployment images by digest and scan them in the future hosted profile.
- Maintain an incident-response and data-breach assessment process for any
  public deployment.
