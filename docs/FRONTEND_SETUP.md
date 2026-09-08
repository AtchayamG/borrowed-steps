# Frontend Setup — Borrowed Steps (M1 & M2A Web Client)

This document provides setup, development, testing, and build instructions for the Borrowed Steps M1 & M2A React frontend under `apps/web`.

## Environment Prerequisites & Pinned Tool Versions

- **OS**: Windows 10/11 (or macOS / Linux)
- **Node.js**: `v22.22.3`
- **npm**: `10.9.8` (reproducible `package-lock.json` committed)
- **Vite**: `v7.3.6` (directly declared in devDependencies)
- **Vitest**: `v3.2.7`
- **Prettier**: `v3.5.2`
- **Backend Dependency**: Requires the Borrowed Steps FastAPI backend running at `http://127.0.0.1:8000` supporting M1 lifecycle routes and frozen M2A route `POST /api/intake/interpret`. Vite dev server proxies `/api` requests to `127.0.0.1:8000`. If backend is stopped, the client renders an actionable Connection Error banner with a Retry button.

## Directory Navigation

All commands should be executed from the `apps/web` directory:

```powershell
cd "D:\Work\Codex\Hackathon Projects\Agents For Humans\00_PROGRAM_CONTROL\worktrees\BS-004-agy\apps\web"
```

Prefix shell commands with `rtk` (e.g. `rtk proxy npm ...`) per repository conventions.

## Installation

Install exact dependencies using `npm ci`:

```powershell
rtk proxy npm ci
```

## Development Server

Start Vite local development server:

```powershell
rtk proxy npm run dev
```

- Local URL: `http://localhost:5173`
- Proxies `/api/*` HTTP requests to `http://127.0.0.1:8000/api/*`.
- Same-origin credentials (`bs_session` cookie) are transmitted across the proxy.

## M2A Agent Modes & Intake Configuration

The frontend reports agent mode as configuration metadata without claiming live agent health on load:
- `strands_ollama`: Active local Strands agent framework with Ollama model provider.
- `disabled`: Assistant is intentionally disabled; manual structured request entry is always available.
- `not_implemented`: Legacy/unconfigured mode; manual structured request entry is always available.

Intake endpoint `POST /api/intake/interpret` is an advisory read-only call:
- Exempt from `Idempotency-Key` (does not mutate domain database or create entities).
- Returns unsaved draft suggestion with validated execution provenance (`framework`, `provider`, `model`, `inventory_tool_calls`, `completed_at`).
- Populate form requires deliberate human action ("Use Draft (Populate Form)"); explicit submission remains separate (`POST /api/requests`).

## Verification Pipeline

### 1. Code Formatting Check
Verify formatting across all files using Prettier:
```powershell
rtk proxy npm run format:check
```
To auto-format:
```powershell
rtk proxy npm run format
```

### 2. Code Quality & Linting
Run ESLint:
```powershell
rtk proxy npm run lint
```

### 3. Strict Type Checking
Execute TypeScript compiler without emitting files:
```powershell
rtk proxy npm run typecheck
```

### 4. Unit & Interaction Test Suite (Vitest & Testing Library)
Run the 87 automated tests across 10 suites with test-only network interception:
```powershell
rtk proxy npm test
```

### 5. Production Build
Compile TypeScript and bundle assets with Vite:
```powershell
rtk proxy npm run build
```
Production assets are output to `apps/web/dist/`. Zero test fixtures or mocks are bundled into production.

### 6. Focused Real-Browser Verification
Run automated headless Edge/Chrome checks measuring desktop and 390px mobile layout, keyboard focus trapping, Escape key modal dismissal, focus restoration, backend-offline handling, and M2A synthetic intake flow:
```powershell
rtk proxy npm run test:browser
```
Screenshots and verification evidence are stored in `apps/web/test-evidence/`.

## Architectural Invariants

- **Execution Guards**: Synchronous mutex locks prevent duplicate submissions during intake, allocation, pickup, return, inspection, and assistant interpretation.
- **Dialog & Input Preservation**: Dialogs only close on confirmed server success. On 409, 422, or network failures, dialogs stay open and inputs are preserved.
- **Affirmation Binding**: Changing the selected equipment or determination outcome immediately resets human volunteer approval checkboxes to prevent accidental affirmations.
- **Uncertain Retry Integrity**: Network interruptions preserve the exact original `Idempotency-Key` and request payload. Replays are isolated and cannot be overwritten by other operations.
- **Human-in-the-Loop Intake**: Model interpretation produces ephemeral advisory drafts only; never auto-creates requests or reservations. Form population requires a deliberate human action, and final request submission is explicit. Missing/null fields require human clarification.
- **Instant Preservation**: Datetime conversions between UTC ISO strings and HTML `datetime-local` inputs preserve exact epoch instants without timezone shifts.
- **Session Scoping (401)**: When a session expires, active intake text is preserved and an actionable re-initialization prompt is rendered in active flow without automatic reset.
