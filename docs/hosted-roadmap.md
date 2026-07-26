# Hosted service and model-provider roadmap

The local release intentionally keeps document processing, storage and model
inference on one operator-controlled machine. A professional hosted service is
not just a different model URL: it needs durable tenant isolation, identity,
operational controls and documented data-processing arrangements.

## Deployment profiles

### 1. Local portfolio profile — implemented

- AgreementAtlas binds to `127.0.0.1`.
- LM Studio binds to `127.0.0.1`.
- Files live in short-lived visitor workspaces.
- Model prompts never leave the machine.
- No account, analytics database or durable chat history exists.

### 2. Invite-only review profile — next safe demonstration step

- Keep AgreementAtlas and LM Studio loopback-bound.
- Put an authenticated Cloudflare Access policy in front of the app.
- Set `APP_MODE=public-demo`, secure cookies and real operator/privacy details.
- Preserve the six-hour absolute expiry, manual deletion and one-job model
  semaphore.
- Add gateway request-size limits and access logging that excludes query
  strings, filenames, agreement text and answers.
- Test restoration/restart behavior and incident response before sharing.

A temporary Cloudflare Tunnel should expose only AgreementAtlas:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Do not expose LM Studio or bind it to `0.0.0.0`.

### 3. Professional multi-tenant profile — future architecture

```text
Identity-aware edge
  ▼
Web/API service ──► tenant metadata database
  │
  ├─ encrypted object storage with per-tenant prefixes
  └─ durable job queue ──► isolated ingestion/inference workers
                              │
                              └─ approved model provider or private endpoint
```

Required engineering changes:

- authenticated users and organisations rather than anonymous cookies;
- server-side tenant identity on every record and storage operation;
- tenant-scoped encryption keys or a documented equivalent;
- object storage with lifecycle deletion and auditable erasure;
- durable database job state, idempotency and retry policy;
- separate conversion workers with CPU, memory, archive and time limits;
- malware scanning and content-disarm policy appropriate to the risk;
- central secrets management and key rotation;
- per-tenant quotas, budgets and model-usage accounting;
- audit events that record actions without document text;
- backups with matching retention/erasure behavior;
- regional deployment, monitoring, alerting and incident response; and
- authenticated export, deletion and data-subject-request workflows.

The current in-memory limiter and filesystem session store must not be scaled
across multiple application instances.

## Provider adapter direction

The local `LMStudioClient` already isolates the application from browser-side
model calls and uses a small OpenAI-compatible surface. The hosted release
should formalise this into a provider interface:

```python
class ModelProvider(Protocol):
    def status(self) -> ProviderStatus: ...
    def chat(self, request: ChatRequest) -> ChatResult: ...
    def structured_chat(self, request: StructuredRequest) -> dict: ...
```

Provider configuration should be server-managed per deployment, never supplied
as an arbitrary URL or token by an anonymous browser. The interface should
normalise:

- model discovery and allowlists;
- JSON-schema capability;
- timeouts, retries and provider rate limits;
- usage and cost metadata;
- safe public errors;
- prompt/response logging policy; and
- cancellation.

## Kimi/Moonshot assessment

Kimi’s international API is technically attractive because its official
documentation describes:

- an OpenAI-compatible base URL at `https://api.moonshot.ai/v1`;
- `POST /v1/chat/completions`;
- bearer-token authentication; and
- `json_schema` structured output.

Sources:

- [Kimi API overview](https://platform.kimi.ai/docs/api/overview)
- [Kimi chat and structured-output reference](https://platform.kimi.ai/docs/api/chat)

That does **not** make the standard API an approved destination for customer
agreements. At the time this decision record was written:

- the Kimi OpenPlatform privacy policy said user content includes prompts and
  files and described storage in Singapore; and
- the standard terms said content may be used to provide, maintain, develop,
  support and improve the service, with separate enterprise arrangements
  available for customers requiring restrictions.

Sources:

- [Kimi OpenPlatform privacy policy](https://platform.kimi.ai/docs/agreement/userprivacy)
- [Kimi OpenPlatform terms](https://platform.kimi.ai/docs/agreement/modeluse)

Therefore the repository does not enable Kimi by default.

## Cloud-provider approval gate

Before any cloud adapter can receive real agreement evidence, the operator
must record all of the following:

- provider legal entity, service and region;
- controller/processor roles and an Article 28-compliant DPA where applicable;
- binding no-training/no-improvement treatment for customer content;
- prompt, output, abuse-monitoring and backup retention;
- subprocessor list and change-notification process;
- deletion and data-subject-request support;
- encryption and access controls;
- UK/EU international-transfer mechanism and completed transfer assessment;
- acceptable-use and prohibited-data categories;
- model accuracy/evaluation results for the legal-rule schema; and
- a user-facing notice identifying the provider before upload.

If these cannot be established, use a locally hosted model or a private
regional inference endpoint instead.

## Rollout order

1. Complete the local evaluation set for permission, prohibition, definition,
   precedence, security, renewal and amendment questions.
2. Run an invite-only local-model demonstration.
3. Extract the provider protocol without changing local behavior.
4. Add a fake hosted provider and contract tests.
5. Complete security, privacy and supplier reviews.
6. Enable one approved provider in a non-production environment.
7. Compare citations and rule-schema validity against the local baseline.
8. Launch to a small authenticated cohort with cost and incident monitoring.
