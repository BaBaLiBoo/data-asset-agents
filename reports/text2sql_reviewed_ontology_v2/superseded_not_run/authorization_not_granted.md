# Superseded T-A through T-H not-run evidence

This historical record describes the execution state before the explicit
external-processing authorization received on 2026-07-30.

At that time, the three required ontology sources had been independently
published in PostgreSQL, but the formal 640-case T-A through T-H experiment was
not started because authorization to send fictional MiniBank benchmark and
ontology text to DeepSeek and DashScope had not been granted.

That authorization blocker is now superseded. The current execution state is
recorded in `../preflight.json`: authorization is present, DashScope Embedding
passed, and the configured DeepSeek account returned HTTP 402
`Insufficient Balance` during the first LIVE_PREFLIGHT case.

No formal T-A through T-H Case rows were created during the historical attempt.
