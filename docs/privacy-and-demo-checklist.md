# Privacy and public-demo checklist

This is an engineering and governance checklist, not legal advice or a
guarantee of UK GDPR, EU GDPR or PECR compliance. The operator must obtain
professional advice for the actual deployment, users, documents, vendors and
jurisdictions.

The current in-product notice is a deployment template. Public mode fails
closed until an operator name, privacy email, public URL and secure-cookie flag
are configured, but configuration alone does not make a deployment compliant.

## Before collecting a document

- [ ] Identify the operator/controller and publish real contact details.
- [ ] Define the purposes and lawful basis for session identifiers, network
      security data, uploaded agreement content and generated output.
- [ ] Record whether the operator acts as controller, processor or both for
      each processing activity.
- [ ] Complete a record of processing activities where required.
- [ ] Assess whether agreements are likely to contain signatures, employee
      details, contact information, special-category data or criminal-offence
      data.
- [ ] Complete a DPIA if the deployment is likely to create high risk; record
      the screening decision even if a full DPIA is not required.
- [ ] Make the privacy notice concise and visible at upload time, not only in a
      footer.
- [ ] Require users to confirm authority to upload in public-demo mode.
- [ ] Prohibit confidential, privileged, health, children’s, credential and
      export-controlled material in an unauthenticated demonstration.

The ICO says privacy information should identify purposes, retention,
recipients, international transfers, rights and the responsible organisation,
and should be provided when personal information is collected:
[Right to be informed](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-be-informed/).

## Data minimisation and retention

- [x] Use an opaque random session identifier instead of an account profile.
- [x] Do not persist chat history.
- [x] Set an absolute six-hour application expiry.
- [x] Offer immediate whole-session deletion.
- [x] Avoid logging document text, questions, answers or filenames.
- [ ] Confirm that infrastructure logs, snapshots and backups follow the
      published retention statement.
- [ ] Test automatic expiry, manual deletion and backup expiry on the real host.
- [ ] Document exceptions such as security-incident evidence or legal holds.

The UK GDPR does not prescribe a universal retention period; the operator must
justify its period and erase or anonymise information when no longer needed:
[ICO storage limitation guidance](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/data-protection-principles/a-guide-to-the-data-protection-principles/storage-limitation/).

## Cookies and browser storage

- [x] Use one first-party session cookie for workspace isolation.
- [x] Mark it HTTP-only and `SameSite=Strict`.
- [x] Require `Secure` in public mode.
- [x] Do not add analytics, advertising or preference cookies.
- [ ] Keep a cookie inventory and update the notice if the deployment adds
      identity, analytics, embedded media or another storage technology.
- [ ] Add consent controls before setting anything that is not strictly
      necessary.

The ICO recognises a narrow exception for storage that is essential to provide
a user-requested service, including some security and authentication session
uses, but still recommends clear information:
[Cookies and similar technologies](https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guide-to-pecr/cookies-and-similar-technologies/).

## Security controls

- [x] Separate every visitor into a random filesystem workspace.
- [x] Enforce file count and total-byte limits.
- [x] Sanitize filenames and reject path components.
- [x] Check supported extensions, signatures and Office expansion size.
- [x] Rebuild uploads atomically.
- [x] Restrict model concurrency and request frequency.
- [x] Set CSP, frame, MIME, referrer and browser-permission headers.
- [x] Render extracted text with text nodes rather than HTML.
- [x] Require a non-form same-origin header for mutations.
- [ ] Run conversion in a sandboxed worker with CPU, memory and time limits
      before accepting anonymous internet traffic at scale.
- [ ] Add gateway limits, malware scanning and dependency/container scanning.
- [ ] Perform an independent security review and penetration test.
- [ ] Define patch, key-rotation and vulnerability-disclosure processes.

## Processors and international transfers

- [ ] List the host, CDN/security edge, model provider, logging provider,
      support tools and every relevant subprocessor.
- [ ] Put required processor clauses and DPAs in place.
- [ ] Document region, remote access, support access and subprocessor locations.
- [ ] Determine whether each disclosure is a restricted UK or EEA transfer.
- [ ] Where required, implement an appropriate safeguard and complete the
      relevant transfer assessment before transferring personal information.
- [ ] Describe recipients, transfer locations and safeguards in the privacy
      notice.

ICO guidance explains that UK restricted transfers require adequacy,
appropriate safeguards or a valid exception, and safeguards generally require
a transfer-risk/data-protection assessment:
[International transfers](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/international-transfers/).

## Individual rights and incidents

- [ ] Define how a person can request access, erasure, correction, restriction
      or objection when AgreementAtlas has no named account.
- [ ] Explain the limits of identifying a session after its cookie is lost.
- [ ] Train the operator to preserve only the minimum incident evidence.
- [ ] Maintain a breach log and documented risk-assessment process.
- [ ] Define supervisory-authority and affected-person notification decisions.
- [ ] Test the process with a simulated cross-session disclosure.

The ICO notes that reportable personal-data breaches must be reported without
undue delay and within 72 hours:
[ICO breach response guidance](https://ico.org.uk/for-organisations/advice-for-small-organisations/personal-data-breaches/72-hours-how-to-respond-to-a-personal-data-breach/).

## Launch decision

An anonymous Quick Tunnel is not a professional production deployment. Prefer
screenshots for broad public sharing and Cloudflare Access for hands-on
employer review. Do not open a cloud-model route until the provider approval
gate in `docs/hosted-roadmap.md` is complete.
