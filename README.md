# Query Assistant | Backend

For the lightweight Vercel + Supabase deployment, follow [DEPLOY_VERCEL.md](DEPLOY_VERCEL.md).
The instructions below describe local mode; install `requirements-local.txt` for local embeddings/OCR/Streamlit.

An OCR-enabled retrieval-augmented generation (RAG) backend for asking questions about PDFs with document-level isolation and page citations.

The pipeline extracts text and image content, cleans it, creates page-aware passages, and stores embeddings in Qdrant. Questions retrieve relevant passages before DeepSeek generates a grounded answer.

## Capabilities

- Text extraction with PyMuPDF and image OCR with Tesseract.
- Optional DeepSeek normalization with rule-based cleanup fallback.
- Heading-aware chunking and near-duplicate embedding removal.
- SHA-256 ingestion registry to avoid processing unchanged PDFs again.
- Separate Qdrant collections and PDF folders for each knowledge base.
- Built-in Customer Support and Legal Assistant personas.
- Persistent custom knowledge bases with user-defined names and rename support.
- Conversational follow-up queries, cited answers, and standalone passage search.
- FastAPI endpoints, a command-line interface, and an alternative Streamlit UI.
- English answers to English questions and Roman Urdu answers to Roman Urdu questions.

## Architecture

```text
PDF -> Text extraction + OCR -> Cleanup -> Chunking -> Embeddings -> Qdrant
                                                                        |
Question + chat history -> Query rewrite -> Query embedding -> Retrieval -+
                                                              |
                                                   DeepSeek -> Answer + sources
```

PDF ingestion is separate from question answering. Once a PDF is indexed, queries search its stored vectors rather than re-extracting the document.

## Technology

| Component | Implementation |
| --- | --- |
| HTTP API | FastAPI, Uvicorn, Pydantic |
| PDF extraction | PyMuPDF |
| OCR | Pytesseract and the Tesseract executable |
| Cleanup and answers | DeepSeek through the OpenAI-compatible SDK |
| Embeddings | `intfloat/multilingual-e5-small` via Sentence Transformers |
| Vector database | Qdrant, embedded locally or accessed over HTTP |
| Alternative interface | Streamlit |

## Requirements

- Python 3.10 or later with compatible dependency wheels.
- Tesseract OCR installed separately for scanned pages and embedded image text.
- A DeepSeek API key for generated answers and model-assisted cleanup.
- Writable, persistent storage for uploaded PDFs and runtime data.
- Internet access for initial embedding-model download and DeepSeek requests.

Without a DeepSeek key, rule-based ingestion and passage search remain available. OCR image text is skipped when Tesseract is unavailable.

## Quick start

Run these commands from the backend directory.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and enter your real `DEEPSEEK_API_KEY`. Set `TESSERACT_CMD` if Tesseract is not discoverable on your system. Then start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn api_server:app --port 8000
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env before starting the server.
python -m uvicorn api_server:app --port 8000
```

Install Tesseract using your operating system's package manager. On non-Windows systems, remove the Windows-specific `TESSERACT_CMD` value from `.env` when Tesseract is already on PATH.

| Resource | Local URL |
| --- | --- |
| API health | http://localhost:8000/api/health |
| Interactive API documentation | http://localhost:8000/docs |
| OpenAPI schema | http://localhost:8000/openapi.json |

Start the [frontend](../ocr-frontend/README.md) in a second terminal. Its default origin, `http://localhost:3000`, is already allowed by CORS. Use **`api_server.py`** for the current frontend; `api.py` is an older API with different routes.

## First document

Choose Customer Support or Legal Assistant in the frontend and click **Use bundled documents**. Alternatively, upload a PDF and wait for ingestion to finish. Shipping the sample PDFs does not automatically index them.

For any other topic, create a named knowledge base in the frontend or call `POST /api/domains`. Upload related PDFs to its returned key. Each knowledge base has an independent collection, and answers are retrieved only from that collection.

Custom names accept 1-80 characters. Names must be unique, ignoring case. Renaming a custom knowledge base preserves its key, PDFs, and vectors. Built-in assistants cannot be renamed through this endpoint.

## Configuration

Copy `.env.example` to `.env`. Existing process environment variables can also provide configuration. Restart the server after changing settings.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | Empty | Provider key for answers and LLM cleanup |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Generation model |
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-small` | Passage and query embeddings |
| `QDRANT_URL` | Empty | Empty selects local embedded storage |
| `QDRANT_API_KEY` | Empty | Optional remote Qdrant credentials |
| `CHUNK_CHARS` | `1000` | Target passage size |
| `CHUNK_OVERLAP_CHARS` | `150` | Passage overlap |
| `TOP_K` | `5` | Default retrieval count |
| `NEAR_DUPLICATE_THRESHOLD` | `0.97` | Embedding similarity cutoff |
| `TESSERACT_CMD` | Auto-detected | Path to the OCR executable |
| `OCR_LANG` | `eng` | Installed Tesseract language data |
| `OCR_PSM` | `6` | OCR page segmentation mode |
| `MIN_IMAGE_PIXELS` | `120` | Small-image filtering threshold |

**Local Qdrant has a single-process lock.** Run the API, Streamlit, or CLI against the embedded folder one at a time. Do not start multiple API workers with embedded storage. Use a remote Qdrant server when multiple processes need database access; file-based registries still require coordinated writes.

## API reference

All routes below are relative to the server origin.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | API health and configuration flags |
| GET | `/api/domains` | Built-in and saved custom knowledge bases |
| POST | `/api/domains` | Create a custom knowledge base |
| PATCH | `/api/domains/{key}` | Rename a custom knowledge base |
| GET | `/api/domains/{key}/status` | Indexed passage count and processed files |
| POST | `/api/domains/{key}/ingest` | Process an uploaded PDF or existing PDFs |
| POST | `/api/domains/{key}/ask` | Answer with retrieval and citations |
| POST | `/api/domains/{key}/search` | Retrieve passages without answer generation |
| POST | `/api/domains/{key}/reset` | Clear indexed content and its processing registry |

Create or rename a knowledge base with JSON:

```json
{"name": "Research Notes"}
```

Creation returns HTTP 201 and an internal key such as `custom_<uuid>`. Use that returned key for subsequent requests. Display names do not determine filesystem paths.

Ask a question:

```json
{
  "question": "What are the main conclusions?",
  "history": [],
  "top_k": 5
}
```

`history` contains previous messages as `{"role": "user", "content": "..."}` or assistant equivalents. Answers include `answer`, cited `sources`, the retrieval `query`, and raw `hits`. A source contains `text`, `metadata`, and a similarity `score`. Metadata includes document name, page, and heading where available.

Search with `{"query": "refund policy", "top_k": 5}`. The response contains `hits`.

### Ingestion stream

Use multipart form fields: optional `file`, `force` (default `false`), and `use_llm` (default `true`). Omitting `file` processes PDFs already present in the selected knowledge base's folder.

The response is `application/x-ndjson`, with one event per line:

```json
{"type": "log", "message": "STEP 5    chunking"}
{"type": "done", "report": {"name": "notes.pdf", "status": "ingested", "chunks": 22}}
```

An ingestion failure produces an `error` event with a `message`. Clients must inspect stream events even after an HTTP 200 response. When processing multiple existing PDFs, the final `done` event reports the last PDF.

Reset deletes the selected collection and ingestion-registry entries. It keeps the PDF files and custom knowledge-base name; process saved PDFs again to restore searchable content.

To permanently remove a custom knowledge base, select it in the frontend and click **Delete this knowledge base**, then confirm. This deletes only that base's saved PDF folder, vector collection, ingestion records, and saved name. Other bases are preserved. The API endpoint is `DELETE /api/domains/{key}`; built-in assistants cannot be deleted.

To remove just one PDF from any knowledge base (including Customer Support), click the trash icon beside that document and confirm. Only its saved PDF copies with matching content, vectors in the selected collection, and ingestion entry are removed. Other PDFs and knowledge bases are preserved. The selected base's browser conversation is cleared to avoid showing old citations. The API endpoint is `DELETE /api/domains/{key}/documents/{file_hash}`.

## Storage and project structure

```text
ocr-backend/
|-- api_server.py          # Current frontend API
|-- api.py                 # Legacy API
|-- app.py                 # Alternative Streamlit interface
|-- cli.py                 # Built-in assistant CLI
|-- src/rag/
|   |-- config.py          # Environment and filesystem configuration
|   |-- domains.py         # Personas and custom knowledge-base persistence
|   |-- extract.py         # PDF extraction and image OCR
|   |-- clean.py           # Normalization and fallback cleanup
|   |-- chunk.py           # Page-aware passages
|   |-- embed.py           # Embeddings and near-duplicate removal
|   |-- vectorstore.py     # Qdrant operations
|   |-- ingest.py          # Processing pipeline and SHA-256 registry
|   |-- llm.py             # Provider client
|   `-- answer.py          # Retrieval and generated answers
|-- pdfs/                  # Bundled and uploaded PDFs
|-- data/                  # Generated runtime state, ignored by Git
|-- scripts/               # Sample PDF generation
|-- tests/                 # Offline validation
|-- .env.example
`-- requirements.txt
```

Runtime data includes `data/registry.json`, `data/custom_domains.json`, `data/cache/`, and `data/qdrant_db/`. Back up `data/` and `pdfs/` together if indexed documents and custom names must survive machine changes. Remote Qdrant does not store the local JSON registries or original PDFs.

## CLI and alternative UI

With the Python environment activated, run one process at a time when using embedded Qdrant:

```bash
python cli.py ingest
python cli.py ingest -d legal_assistant --pdf path/to/contract.pdf
python cli.py ingest -d customer_support --no-llm
python cli.py ask -d customer_support "What is the delivery policy?"
python cli.py search -d customer_support "refund timeline"
python cli.py chat -d legal_assistant
python cli.py status
```

The current CLI argument choices and Streamlit dropdown initialize from the built-in assistants. Create and manage custom knowledge bases through the current HTTP API or frontend.

For the alternative built-in-assistant interface, stop the API before running:

```bash
streamlit run app.py
```

## Tests

```bash
python tests/test_custom_domains.py
python tests/test_pipeline_offline.py
python tests/test_app_offline.py
```

Custom-domain tests use the standard library and temporary storage. Pipeline tests use a fake embedder; Streamlit tests use a fake embedder and stubbed LLM. The latter two need installed project dependencies, but no provider key or real embedding-model download. These checks do not establish live OCR or DeepSeek service availability.

## Deployment considerations

Run the backend on a Python host with Tesseract, sufficient resources for the embedding model, and persistent storage for `pdfs/` and `data/`. A typical server entry point is:

```bash
python -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Use the port expected by your host. Keep one API worker for the current local-storage design. Serve a public backend over HTTPS and add the frontend's exact origin to `allow_origins` in `api_server.py`.

The frontend can be hosted separately on Vercel. This backend's persistent filesystem and model/OCR requirements call for additional adaptation before a serverless deployment; see [Vercel runtime documentation](https://vercel.com/docs/functions/runtimes).

The current API is a shared workspace without authentication, per-user authorization, or upload quotas. Restrict access appropriately before exposing private documents publicly. CORS controls browser origins; it is not authentication. Keep secrets in server environment configuration.

## Troubleshooting

| Problem | Action |
| --- | --- |
| Qdrant storage is already in use | Stop another API, Streamlit, or CLI process using the same local folder. |
| Missing DeepSeek key | Configure `DEEPSEEK_API_KEY` and restart the server. |
| Image text is missing | Check Tesseract installation, executable path, and language data. |
| First ingestion takes longer | Allow the embedding model to download; inspect processing logs. |
| Empty knowledge base | Complete ingestion before asking questions. |
| Vectors fail after changing models | Reset the affected knowledge base, then re-ingest its PDFs. |
| Frontend cannot connect | Check the API URL, HTTPS configuration, CORS allowlist, and backend logs. |

## Repository hygiene

The `.gitignore` excludes environment secrets, virtual environments, caches, runtime databases, and uploaded PDFs. The two bundled sample PDFs and `.env.example` remain versionable. Files already tracked by Git are not removed by adding ignore rules; review staged changes before publishing.
