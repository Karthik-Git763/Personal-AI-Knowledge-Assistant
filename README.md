<p align="center">
  <img src=".docs/img/logo_img.png" alt="Cognolith - connected notes, graph, search, and local AI" width="100%" />
</p>

# Cognolith

Cognolith is a self-hosted knowledge workspace for connected notes, document ingestion, relationship graphs, hybrid search, and local AI chat. It runs on Next.js, FastAPI, PostgreSQL with pgvector, and optional Ollama models.

## Features

- Markdown notes with Obsidian-style live preview
- `[[Wiki links]]`, backlinks, and automatic graph relationships
- Full and local knowledge graph views
- Nested note folders and tags
- PDF, DOCX, Markdown, and text document ingestion
- Hybrid full-text and semantic search across documents, notes, and chats
- Conversational chat with grounded answers from documents and notes when relevant
- Authenticated, user-isolated workspaces
- Six-digit email verification with automatic sign-in
- Optional Google Drive workspace backups and cross-platform recovery
- Optional local model integration through Ollama

<!--## Screenshots

| Dashboard | Notes |
| --- | --- |
| ![Dashboard overview in dark mode](docs/img/dashboard.png) | ![Markdown notes workspace in dark mode](docs/img/notes.png) |
| Documents | Search |
| ![Document library in dark mode](docs/img/documents.png) | ![Workspace search in dark mode](docs/img/search.png) |
| Chat | Knowledge Graph |
| ![Chat sessions in dark mode](docs/img/chat.png) | ![Knowledge graph in dark mode](docs/img/knowledge-graph.png) |-->

## Quick Start

### Requirements

- Docker Desktop or Docker Engine with Compose
- Git

### Run the stack

```bash
git clone https://github.com/itxkarthik/cognolith.git
cd cognolith
docker compose --profile ai up --build -d
```

Open:

- Application: [http://localhost:8080](http://localhost:8080)
- Swagger API documentation: [http://localhost:3000/docs](http://localhost:3000/docs)
- ReDoc API documentation: [http://localhost:3000/redoc](http://localhost:3000/redoc)
- Backend health: [http://localhost:3000/health/ready](http://localhost:3000/health/ready)
- Development email inbox: [http://localhost:8025](http://localhost:8025)

The frontend runs on port `8080`; FastAPI and its documentation run on port `3000`. Therefore, `http://localhost:8080/docs` returns `404` by design. Use `http://localhost:3000/docs`.

Check service health:

```bash
docker compose ps
```

Stop the stack:

```bash
docker compose down
```

Database data is stored in the `postgres_data` Docker volume and is preserved between restarts.

To run the core application without local AI, omit the profile:

```bash
docker compose up --build -d
```

## Configuration

For Docker, copy the root example and set local secrets:

```bash
cp .env.example .env
```

For backend-only development outside Docker, copy the backend example:

```bash
cp backend/.env.example backend/.env
```

Use `.env.docker.example` when you prefer an explicit Compose env file:

```bash
cp .env.docker.example .env.docker
docker compose --env-file .env.docker --profile ai up --build -d
```

The main settings are:

| Variable | Purpose |
| --- | --- |
| `POSTGRES_USER` | PostgreSQL user |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `POSTGRES_DB` | Database name |
| `SECRET_KEY` | Token-signing secret |
| `FRONTEND_HOST` | Allowed frontend origin |
| `SMTP_HOST` | SMTP server used for verification emails |
| `SMTP_PORT` | SMTP server port (`1025` for local Mailpit) |
| `SMTP_USER` | SMTP username; for Gmail this is the sending Gmail address |
| `SMTP_PASSWORD` | SMTP password; for Gmail this is a Google app password |
| `EMAILS_FROM_EMAIL` | Sender address for verification emails |
| `GOOGLE_DRIVE_CLIENT_ID` | Google OAuth Web client ID; leave blank to disable Drive backups |
| `GOOGLE_DRIVE_CLIENT_SECRET` | Google OAuth Web client secret |
| `GOOGLE_DRIVE_REDIRECT_URI` | Exact backend OAuth callback registered with Google |
| `GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY` | Fernet key used to encrypt stored Google refresh tokens |
| `OLLAMA_BASE_URL` | Optional Ollama endpoint |
| `NEXT_PUBLIC_API_URL` | Browser API base URL for standard requests |
| `NEXT_PUBLIC_STREAM_API_URL` | SSE-capable public backend URL; local Docker uses `http://localhost:3000/api/v1` |

Local Docker uses Mailpit, so verification emails stay on the machine and can be read at `http://localhost:8025`. Production deployments must provide a real SMTP host and sender address.

### Google Drive backups

Google Drive backup is optional and remains disabled when its credentials are blank. Cognolith requests only Google's [`drive.appdata`](https://developers.google.com/workspace/drive/api/guides/appdata) scope and stores snapshots in the hidden application-data folder. The app cannot browse or modify the user's normal Drive files, and snapshots do not appear in the standard Drive interface.

A Google account may back one Cognolith account per installation; reconnecting that same Cognolith account on another installation remains supported.

Configure Google OAuth:

1. Create or select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
3. Configure the OAuth consent screen. Add intended accounts as test users while the app remains in testing.
4. Create an [OAuth client](https://support.google.com/cloud/answer/15549257) with application type **Web application**.
5. Add the exact authorized redirect URI. Local Docker uses:

   ```text
   http://localhost:3000/api/v1/users/me/google-drive/callback
   ```

   Production must use the public backend origin with the same path, for example `https://api.example.com/api/v1/users/me/google-drive/callback`.

6. Set `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, and `GOOGLE_DRIVE_REDIRECT_URI` in the uncommitted `.env`.
7. Generate a dedicated Fernet key and set `GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY`:

   ```bash
   docker compose run --rm --no-deps backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

Keep the Fernet key stable and secret. Losing or rotating it without a migration prevents Cognolith from decrypting existing Google refresh tokens. Restart the backend after changing OAuth configuration.

Each snapshot includes note content and hierarchy, tags and note links, note templates, original uploaded documents and their metadata, chat sessions and messages, and workspace preferences. It excludes passwords, sessions, verification codes, Google credentials, backup history, logs, Ollama models, and derived data such as embeddings, document chunks, extracted previews, summaries, and chat diagnostics. Derived document content is rebuilt after restore.

After Drive is connected, Cognolith schedules a backup every 24 hours. A backend that was offline catches up after startup; opening backup settings also starts an overdue backup. Users can start a manual backup at any time when no backup or restore is active. Normal retention keeps the newest five completed snapshots. A restore first creates an additional safety snapshot of the current workspace.

Restore is an explicit replacement operation: after preview and `RESTORE` confirmation, the selected snapshot replaces the current notes, documents, chats, relationships, and preferences. To recover on another device or Cognolith installation, sign in to Cognolith, connect the same Google account, select a compatible snapshot, and restore it. Stable workspace identifiers preserve relationships across installations.

Troubleshooting:

- **Not configured:** set the client ID, client secret, and Fernet key, verify the redirect URI, then restart the backend.
- **Redirect mismatch:** make the Cloud Console redirect URI and `GOOGLE_DRIVE_REDIRECT_URI` identical, including scheme, host, port, and path.
- **Authorization failed:** confirm the Drive API is enabled, the account is an allowed consent-screen test user, and reconnect from Settings.
- **Reconnect required:** the refresh token was revoked or expired; reconnect the same Google account.
- **Temporary provider error:** retry after network or Google API availability recovers.
- **Snapshots are not visible in Drive:** this is expected for the hidden `appDataFolder`.
- **Restore is rejected:** use an unmodified, compatible Cognolith snapshot. Checksum, ownership, and archive validation failures are intentionally reported without provider credentials or response bodies.

### Database migrations

Alembic is the authoritative schema manager. The backend applies pending migrations before Uvicorn starts. Before the first upgrade of an existing deployment, create a database backup:

```bash
docker compose exec db pg_dump -U postgres knowledge_assistant > knowledge_assistant-backup.sql
```

Inspect and apply migrations manually when needed:

```bash
docker compose exec backend alembic current
docker compose exec backend alembic history
docker compose exec backend alembic upgrade head
```

Create a reviewed schema revision after changing SQLModel metadata:

```bash
docker compose exec backend alembic revision --autogenerate -m "describe schema change"
```

Downgrade only after reviewing the target revision and restoring from backup if necessary:

```bash
docker compose exec backend alembic downgrade -1
```

Ollama is optional for notes, document extraction, authentication, and graph features. The default stack stays lightweight and reports AI as unavailable without writing placeholder chat messages.

### Local AI

Start the stack with the optional Ollama profile:

```bash
docker compose --profile ai up --build -d
```

The profile starts Ollama and installs `llama3.2:1b` for chat plus `nomic-embed-text` for document and note embeddings. The first startup takes longer while those models download. Follow progress with:

```bash
docker compose logs -f ollama-models
```

Model data is retained in the `ollama_data` volume. Override `OLLAMA_CHAT_MODEL`, `OLLAMA_EMBEDDING_MODEL`, or `OLLAMA_BASE_URL` when using a different local runtime.

### Change models

Signed-in users can change their chat model from **Settings > Local AI model**:

1. Install the model in Ollama if it is not already available:

   ```bash
   docker compose exec ollama ollama pull qwen3:4b
   ```

2. Open **Settings > Local AI model** in the application.
3. Select **Refresh** to reload the installed model list.
4. Choose the model and select **Save model**.

The preference is saved per account and applies to new chat responses. `qwen3:4b` is the recommended local starting point; larger models generally improve multi-source synthesis but require more memory. The embedding model remains separate because changing its dimensions requires rebuilding the vector indexes.

`OLLAMA_CHAT_MODEL` and `OLLAMA_EMBEDDING_MODEL` set the Docker defaults for users who have no saved preference. Put overrides in `.env.docker`:

```bash
OLLAMA_CHAT_MODEL=qwen3:4b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
```

Start Compose with that environment file:

```bash
docker compose --env-file .env.docker --profile ai up --build -d
```

You can also assign a model from the command line. First install it in the shared Ollama volume:

```bash
docker compose exec ollama ollama pull qwen3:4b
```

Then save the per-user preference by email:

```bash
docker compose exec backend python -m scripts.set_user_model --email you@example.com --chat-model qwen3:4b
```

The account preference overrides the Docker default. Omit `--embedding-model` to keep `nomic-embed-text`; changing embedding dimensions requires rebuilding the vector indexes. Confirm the active Docker defaults and installed models with:

```bash
curl http://localhost:3000/health/ready
docker compose exec ollama ollama list
```

## Search and Chat

Search combines PostgreSQL full-text matching with pgvector semantic retrieval. The Documents, Notes, and Chats filters are applied independently, so a result does not need to repeat the exact query wording to match.

Chat uses three response paths:

- Casual conversation is answered directly without searching the workspace.
- General knowledge questions use the selected chat model when workspace context is unnecessary.
- Questions about personal notes, documents, or projects remain grounded in retrieved workspace context.

Answers stream from Ollama as they are generated. **Stop response** cancels generation and preserves the partial answer; **Retry** regenerates from the same user turn without duplicating it. Grounded answers receive one citation-repair pass when needed. Retrieval diagnostics are disabled by default and can be enabled under **Settings > Local AI model**.

If the correct source appears but the answer is weak, use a stronger chat model. If the correct source does not appear, review the embedding model, chunking settings, and similarity threshold.

## Notes and Graphs

Notes are stored as Markdown. Link notes by title:

```markdown
This idea extends [[Distributed Systems]].
```

Saving the note creates a directed graph relationship. Renaming or deleting the wiki link updates that relationship. The graph also supports manually created `related`, `parent`, and `child` links.

## Development

Install dependencies from the repository root:

```bash
pnpm install
python -m pip install -r requirements.txt
```

Run the frontend:

```bash
pnpm --dir frontend dev
```

Run the backend in another terminal:

```bash
cd backend
python run.py
```

The local frontend expects PostgreSQL and the backend to be available. Docker Compose is the recommended development path when working across the full stack.

## Verification

```bash
docker compose build frontend backend
docker compose --profile ai up -d
docker compose --profile ai ps
curl http://localhost:3000/health/ready
```

Audit the pinned production dependencies with the same thresholds used by CI:

```bash
pnpm audit --prod --audit-level high
docker compose exec backend pip-audit -r /usr/src/app/requirements.txt
```

Frontend audit fixes must update both `frontend/package.json` and
`pnpm-lock.yaml`. Run `pnpm install` from the repository root after changing
dependency versions or workspace overrides, then commit the regenerated
lockfile. Keep audit tooling in development dependencies so it is not installed
in production images.

## Continuous Integration

Pull requests and pushes run three validation layers:

- **Quality** runs backend tests, Ruff, BasedPyright, Python and pnpm audits, frontend tests, linting, type checks, and production image builds on GitHub-hosted runners.
- **Full Stack** is manually dispatched when needed. It starts PostgreSQL, Mailpit, Cognolith, and real Ollama models, then runs Playwright on desktop and mobile Chromium.
- **Release Images** publishes versioned backend and frontend images to GHCR for tags matching `v*`.

The full-stack workflow requires a trusted self-hosted GitHub Actions runner with Docker, Git, Node.js, curl, and the labels `self-hosted` and `cognolith-ai`. Keep the runner workspace and Docker volumes between jobs so Ollama models are reused. Do not expose this runner to pull requests from untrusted forks. Without a matching online runner, a manually started job remains queued; cancel it from the Actions page or bring the runner online.

Install browser dependencies locally and run the same end-to-end suite with:

```bash
pnpm --dir frontend run test:e2e:install
docker compose --profile ai up --build -d
pnpm --dir frontend run test:e2e
```

Failed CI runs upload Playwright traces, screenshots, videos, and Docker service logs.

## Self-Hosted Releases

Local development continues to use `docker-compose.yml`. Deployment uses `docker-compose.production.yml`, published images, an external TLS-enabled PostgreSQL database, real SMTP, persistent uploads, and persistent Ollama models.

Create the production environment file and replace every placeholder:

```bash
cp .env.production.example .env.production
docker compose --env-file .env.production -f docker-compose.production.yml config
docker compose --env-file .env.production -f docker-compose.production.yml up -d
```

Run smoke checks after startup or upgrade:

```bash
sh scripts/release/smoke.sh
# Windows PowerShell
./scripts/release/smoke.ps1
```

For the bundled local PostgreSQL service, create a compressed backup with a checksum before migrations or upgrades:

```bash
sh scripts/release/backup.sh
```

Restore into a clean local database volume, then apply current migrations:

```bash
sh scripts/release/restore.sh backups/cognolith-YYYYMMDDTHHMMSSZ.sql.gz
```

For production external PostgreSQL, use the provider's snapshot and point-in-time recovery tools. Test restore procedures before relying on them. Roll back application images by pinning `COGNOLITH_BACKEND_IMAGE` and `COGNOLITH_FRONTEND_IMAGE` to the previous release tag; restore the database first when a migration is not backward compatible.

To publish a release, verify `main`, create an annotated `v*` tag, and push it:

```bash
git tag -a v0.2.0 -m "Cognolith v0.2.0"
git push origin v0.2.0
```

## Architecture

```mermaid
flowchart LR
    User["Browser"]

    subgraph Frontend["Cognolith frontend :8080"]
        Next["Next.js 16 app"]
        Shell["Authenticated workspace shell"]
        UI["Dashboard, notes, documents, search, chat, graph, settings"]
        APIClient["API client and /api/v1 rewrite"]
        Next --> Shell --> UI --> APIClient
    end

    subgraph Backend["Cognolith API :3000"]
        Migrations["Alembic bootstrap"]
        FastAPI["FastAPI routes"]
        Auth["Session auth, CSRF, email verification"]
        Services["Workspace services"]
        Notes["Notes, folders, tags, wiki links"]
        Documents["Document upload, extraction, chunking, summaries"]
        Search["Hybrid search"]
        Chat["RAG chat orchestration"]
        Graph["Knowledge graph relationships"]
        Backups["Backup export, validation, and restore"]
        Reprocessing["Document extraction and embedding rebuild"]
        Migrations --> FastAPI
        FastAPI --> Auth --> Services
        Services --> Notes
        Services --> Documents
        Services --> Search
        Services --> Chat
        Services --> Graph
        Services --> Backups
        Backups --> Reprocessing --> Documents
    end

    subgraph Database["PostgreSQL + pgvector :5432"]
        Core["Users, sessions, notes, documents, chats"]
        Links["Note links and graph edges"]
        FullText["Full-text indexes"]
        Vectors["Document and note embeddings"]
        OTP["Hashed email OTP records"]
        BackupState["Encrypted Drive tokens, schedules, and operations"]
    end

    subgraph AI["Optional Ollama :11434"]
        ChatModel["Per-user chat model"]
        EmbedModel["Embedding model"]
    end

    subgraph Google["Google OAuth and Drive API"]
        OAuth["Scoped OAuth authorization"]
        AppData["Hidden appDataFolder snapshots"]
        OAuth --> AppData
    end

    SMTP["SMTP or Mailpit :8025"]
    Files["Uploaded file storage"]

    User -->|"HTTP"| Next
    APIClient -->|"Docker network"| FastAPI
    Auth --> OTP
    Auth -->|"Verification email"| SMTP
    Backups --> BackupState
    Backups -->|"Scoped OAuth and snapshot upload"| OAuth
    Backups -->|"Export and restore originals"| Files
    AppData -->|"Verified snapshot download"| Backups
    Notes --> Core
    Notes --> Links
    Documents --> Files
    Documents --> Core
    Documents -->|"Create embeddings"| EmbedModel
    Search --> FullText
    Search --> Vectors
    Graph --> Links
    Chat -->|"Retrieve context"| Search
    Chat -->|"Grounded prompt"| ChatModel
    EmbedModel --> Vectors
    ChatModel -->|"Answer with sources"| Chat
```

The browser communicates with the Next.js application on port `8080`. API calls go through the `/api/v1` rewrite to FastAPI on port `3000`, where authentication, CSRF checks, email verification, and account ownership are enforced before any workspace data is touched.

PostgreSQL stores users, sessions, notes, document metadata, chat history, graph links, full-text indexes, pgvector embeddings, encrypted Google refresh tokens, backup schedules, and operation history. Uploaded files live on the application filesystem. Google Drive snapshots are checksum-validated ZIP archives stored in the connected account's hidden application-data folder. Restore replaces the workspace from a verified snapshot, then rebuilds derived document text, chunks, summaries, and embeddings. Alembic runs before the backend starts so schema changes are versioned instead of inferred at runtime. Ollama is optional: Cognolith can run without local AI, while chat, semantic search, summaries, and embeddings become richer when the configured models are available.

Key directories:

```text
frontend/app/          Next.js routes
frontend/components/   UI and feature components
frontend/lib/          API clients, hooks, and utilities
frontend/store/        Zustand stores
backend/app/api/       FastAPI routes
backend/app/services/  Application services
backend/app/models/    SQLModel database models
backend/app/schemas/   Request and response schemas
```

## Contributing

Keep pull requests focused, include verification steps, and document any environment or schema changes. Use clear commit messages that describe behavior rather than implementation detail.

Use [GitHub Issues](https://github.com/itxkarthik/cognolith/issues) for bug reports, support questions, and feature requests.

## License

This project is licensed under the [MIT License](LICENSE). Copyright (c) 2026 Karthik Das.
