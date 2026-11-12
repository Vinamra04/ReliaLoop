# deploy/

`versions.env` records which image version of each demo service Compose runs.

- The CLI passes it to Compose (`--env-file deploy/versions.env`), so
  `relialoop/orders:${ORDERS_VERSION}` resolves to the deployed version.
- The deploy tool (`python -m tools.cli deploy …`) rewrites it after passing
  the error-budget gate.
- The agent's `rollback_service` action rewrites it too, so a later
  `docker compose up` does not silently redeploy the bad version.

Orders image tags and what they simulate:

| Tag | Release profile | Behaviour |
|---|---|---|
| `v1` | `good` | normal |
| `v2-bad` | `bad` | 25% of requests fail with 500 and every request is 300 ms slower |
| `v3-crash` | `crash` | the process exits about 8 s after start, so the container crash-loops |

`v2-bad` and `v3-crash` carry the label `relialoop.demo=fault-release`.
