# RROS frontend

React + TypeScript + Vite client for the existing Revenue Recovery OS FastAPI API. The frontend intentionally renders only API responses; it does not seed or fabricate workspace records.

## Run locally

From `Rros/frontend`:

```bash
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:5173` and proxies `/api` to `http://localhost:8000`. Start the existing FastAPI app separately (for example, with the repository's documented development command). Set `VITE_API_BASE_URL` when the API is hosted on a different origin; cookies and CSRF still need to be configured by the API for cross-origin use.

## Checks

- `npm run typecheck` — TypeScript project check.
- `npm run build` — typecheck and production Vite build.

Authentication uses the API's HttpOnly `rros_access` cookie and readable `rros_csrf` cookie. Mutating requests automatically send `X-CSRF-Token`; access tokens are never persisted in local storage. Protected screens redirect to the sign-in view on a 401 response.

Implemented surfaces include command center, recovery opportunities, Customer 360, connector readiness/configuration, agent catalog and traces, playbooks, global search, onboarding/admin, usage and billing status, and explicit loading/empty/error states. Live actions remain approval-gated by the API; the client does not claim provider success when the API returns an error or queued status.
