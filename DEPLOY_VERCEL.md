# Vercel backend with Supabase persistence

Deployment dependencies no longer include sentence-transformers, PyTorch,
Streamlit or local Tesseract. Local use remains available with
`pip install -r requirements-local.txt` and `STORAGE_BACKEND=local`.

Cloud mode stores PDFs in a private Supabase bucket and metadata/custom knowledge
base names in Postgres. It calls a configurable embeddings API and a
Mistral-compatible OCR API. Qdrant Cloud continues to store vectors.

## Setup

1. Create/select a Supabase project. Run [supabase/setup.sql](supabase/setup.sql)
   in its SQL Editor. It creates `qa_domains`, `qa_documents`, processing
   functions, and a private `knowledge-pdfs` bucket. RLS and grants restrict
   database access to the backend service role. Keep the bucket private.
2. Add all variables in [.env.cloud.example](.env.cloud.example) to the backend
   Vercel project. Set each blank value; never upload `.env` files to GitHub.
   `SUPABASE_SERVICE_ROLE_KEY` is the legacy **service_role JWT**, not the public
   anon/publishable key. Use separate production and preview Supabase/Qdrant
   resources if previews must not change production documents.
3. Choose an embeddings provider with an OpenAI-compatible `/embeddings` endpoint.
   Set its full endpoint, API key, exact model ID and default output dimension.
   The app validates vector dimensions. DeepSeek's chat key is not automatically
   an embeddings key. No embeddings provider is silently selected or billed.
   OCR defaults to Mistral's `/v1/ocr` contract; set its key for image/scanned PDFs.
4. Set `CORS_ORIGINS` to your exact frontend origin. In Vercel, keep the backend
   root at this repository, use the FastAPI preset, and remove custom build
   commands that install `requirements-local.txt` or `.[local]`. The entrypoint
   is `api_server:app` in `pyproject.toml`.
5. Deploy both repositories. Frontend **Connection settings** must contain the
   backend origin, without `/api`. Backend `/api/health` should show `ok: true`
   and `storage: supabase`. This checks settings, not connectivity.
6. Upload a small text PDF, ask a question, reload and verify persistence.
   Upload an image PDF to verify OCR. Delete one PDF and verify the other
   document's vectors and saved file remain intact.

## Processing and limits

Vercel uploads are limited here to **4,000,000 bytes per PDF**, leaving room for
multipart overhead below the platform request limit. Split larger PDFs first;
direct browser-to-Supabase uploads are not implemented in this version.

The UI starts a durable job and calls one `/documents/{hash}/step` request per
page. Each step performs OCR when needed, optional cleaning, embeddings and
Qdrant upsert. Chunks are persisted before embedding so retries use identical
point IDs/content. Postgres leases prevent concurrent processing of one PDF.
Leases expire after five minutes if a function is terminated.

Keep the tab open while processing. After a failure or closing the tab, select
the base and click **Resume saved documents**. Pending PDFs appear in the list
and can be deleted when not actively leased. Search excludes pending PDFs until
their final page commits. A very complex single page may still time out; split
or simplify that PDF. Function duration is configured to 300 seconds; verify
this fits your Vercel plan/settings. Cloud reset/force rebuild is disabled:
delete an individual PDF, then upload it again.

Cloud processing preserves all chunks instead of applying the local pipeline's
vector near-duplicate removal, keeping retries deterministic. Cleaning remains.

## Existing documents and vectors

This change does not delete old local PDFs, JSON registries or Qdrant collections.
Cloud collection names include a hash of the embeddings endpoint/model/dimension,
preventing incompatible embedding spaces from mixing, even at equal dimensions.
Changing those settings creates a new namespace; use stable endpoint/model IDs.

Recreate custom base names and re-upload PDFs through the cloud UI. They must be
indexed using the new model before cloud search works. Local registries and
vectors are not automatically migrated. Keep old collections until validation.
A base name cannot be removed while another cloud embedding namespace still has
documents in that base.

This preserves the app's shared-workspace access model. It does not add accounts
or per-user authorization; use the deployment access controls appropriate for
your workspace. CORS is browser configuration, not login.

## Validation

Offline tests exercise embeddings validation, resumable ingestion and deletion.
Supabase SQL, provider credentials and actual Linux bundle size still require
verification against the configured deployment.

References: [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[Vercel limits](https://vercel.com/docs/functions/limitations),
[Supabase REST API](https://supabase.com/docs/guides/api),
[Mistral OCR](https://docs.mistral.ai/studio/document-processing/basic_ocr).
