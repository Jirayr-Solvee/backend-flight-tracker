# SES synchronous intake: contract and operator rollout

The handler accepts exactly the direct SES `Event` and `RequestResponse` action
dictionaries for its configured function ARN. No other type, missing field, extra
action field, legacy S3 event, or changed function ARN is accepted. Invocation
mode is not added to the backend proof: all sender, DMARC/scan, recipient,
freshness, bucket/key, content digest, ETag/version and consent checks are unchanged.

## Responses and timing

Every intentional terminal return is exactly
`{"disposition":"STOP_RULE_SET"}`. For synchronous SES invocation this stops
subsequent rule processing; it is not an SMTP bounce, S3 rollback or proof of an
import. A bounded accepted log means only backend HTTP scheduling. Event-mode
callers ignore the returned object, so existing asynchronous intake remains
compatible while the source is rolled out.
[AWS action contract](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-action-lambda.html)

Synchronous work uses a monotonic 20-second soft budget starting at handler entry,
limited further by the current Lambda execution time remaining minus a two-second
reserve. It checks before network stages and before/after 64 KiB body chunks,
preserving the 20 MiB plus one-byte limit, exact declared length, closed stream,
single SDK attempt and bounded socket timeouts. Event mode retains its prior
transport behavior and does not require the context's remaining-time callback.

Exhaustion is an operational failure, never a permanent sender/receipt rejection.
Synchronous socket timeouts are capped by the remaining budget, but one blocking
read, DNS lookup or HTTP exchange can outlast a soft check. The proposed
25-second Lambda runtime limit is a separate hard process backstop and applies to
both modes. Neither it nor the handler guarantees cleanup, a controlled response,
or completion within SES's 30-second invocation limit, which also includes time
outside the handler. No signal timer or background thread is installed.
[AWS synchronous timeout](https://docs.aws.amazon.com/ses/latest/APIReference/API_LambdaAction.html)

HTTP timeout or response-close failure may happen after backend scheduling.
HTTP 202 precedes the durable claim: a repeated request may schedule another
background task, while only one task may win the claim for authorized processing.
A crash before claiming can lose an import; scheduling is not durable delivery.
Preserve any durable receipt claim; do not retry POST inside the handler or
delete/reclaim the old receipt. Transient exceptions contain only the fixed
operational error. The invoking service controls synchronous retries; the
inspected AWS contracts do not establish exact SES retry or SMTP behavior after
FunctionError/timeout. Do not infer it from Lambda's asynchronous retries or an
Invoke API status of 200/202.
[AWS retry behavior](https://docs.aws.amazon.com/lambda/latest/dg/invocation-retries.html)

## Two-phase operator procedure

This document is a proposal, not deployment evidence. Obtain explicit deployment
authority. No new resource, permission, queue, retention change, backend wire
field, migration, or app consent is required for the mode change. Preserve the
current secure backend; do not rerun an old deployment helper that expects Event
mode or changes additional resources.

1. Read and pin the exact current Lambda code hash, configuration revision,
   environment, runtime, execution role, full resource policy, SES active rule set
   and every rule/action, protected-prefix bucket policy, and legacy-trigger
   absence. Stop on unexpected state. Preserve existing secrets in memory only.
2. Package the reviewed `lambda/lambda_function.py` with a verbatim copy of
   `core/email_ingress_contract.py` at ZIP root as `email_ingress_contract.py`.
   Update only that Lambda code and the separately approved timeout to 25 seconds,
   using revision preconditions. Keep the SES action Event during this phase.
   Wait for successful update completion; read back the exact code/configuration
   and unchanged environment/IAM invariants. On uncertain writes, inspect rather
   than blindly repeating the write.
3. Reread the exact receipt rule and compare it to the pinned expected state.
   Change only the existing Lambda action's invocation type to RequestResponse;
   preserve its function ARN, S3-before-Lambda order, bucket/prefix/role, recipient,
   scan/TLS behavior and every unrelated rule/action. Read back the exact result
   and all trust-boundary invariants. A stop disposition cannot undo prior S3
   storage. [AWS S3 ordering requirement](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-action-lambda-example-functions.html)
4. Only after separate send authorization, use a new synthetic message from an
   address proved to have no app identity, mailbox proof or enabled email grant.
   Verify public mail routing, one exact SES object, Lambda execution/outcome and
   bounded backend rejection evidence independently. No private mail, fabricated
   consent, old-message replay, or Google processing is required or authorized by
   this smoke. A fabricated empty-event runtime probe is not SES provenance proof.

An existing receipt retains its receipt-time rule configuration; changing the
rule is not a recovery operation for an old dropped event. Keep original evidence
windows separate. On a failed gate, stop and repair forward with the current
privacy boundaries intact; never restore consent-less code, the legacy S3-only
handler, broader invocation access, or an old database over later user facts.
[SES receipt-time configuration](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-metrics.html)

Local mocked tests prove parser, transport, deadline, cleanup and replay behavior;
they do not prove live SES, IAM, backend readiness, Google egress, imported flights,
APNs delivery or a rendered device notification.
