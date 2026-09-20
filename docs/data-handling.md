# Honesty about data

Where your books sit, what is and is not protected, and what leaves the machine. The short
version is in the [README](../README.md#rules-it-does-not-break); this is the whole of it.

- **Everything sits on the machine you run it on** — database, uploaded documents,
  exports, logs. No replica, no point-in-time recovery, no managed anything.
- **No encryption at rest beyond what your OS gives you.** Stored integration
  credentials are encrypted with a key derived from your own secrets; the database files
  and the uploaded documents are not. Whole-disk encryption is your job.
- **Back it up yourself.** This is the only copy of your books; losing the disk loses the
  data. `scripts/local/backup-aa.ps1` is a `pg_dump`-plus-storage wrapper, and it writes
  to the same machine unless you point it elsewhere. It also copies `.env` into the
  backup as `env.txt`, in clear text, because a database restored without
  `AUTH_JWT_SECRET`, `STORAGE_URL_SECRET` and `CREDENTIAL_ENCRYPTION_SECRET` is not a
  restore. That makes the backup folder as sensitive as `.env` itself — restrict it,
  and do not sync it anywhere you would not put `.env`.
- **Your documents reach a model.** Prompts and page images go wherever `LLM_BASE_URL`
  points. Point it at a server on your own machine or LAN and nothing leaves; point it at
  a remote provider, or fill in `GEMINI_API_KEY` / `OPENAI_API_KEY`, and your financial
  documents are sent to that provider under their terms.
  An empty value in `.env` means **off**, even when the shell that starts the server
  exports that key: `.env` is loaded with `override=True` precisely so a stray
  `OPENAI_API_KEY` in your environment cannot quietly send your documents to a paid
  provider. `GET /health` shows which provider an upload would actually reach.
- **No outbound request from the browser.** The app ships no webfonts and renders in the
  system font stack, so loading a page fetches nothing from anyone else, and the
  Content-Security-Policy allows no external origin except Google's: its OAuth endpoint,
  reached only if you sign in with Google, and `*.googleusercontent.com` images, fetched
  only to show the profile picture of an account created that way. There is no analytics,
  error-tracking or telemetry SDK anywhere in this project — the log files on your disk
  are the only record of what happened.

## Before exposing an instance

The defaults — one owner, loopback only, registration closed — are what this was reviewed
for. Read [`backlog.md`](backlog.md) → *Security hardening* before publishing it through a
tunnel or opening registration to strangers: upload bodies parsed before authentication,
Google sign-in joining accounts by e-mail, a default rate limit that is declared but not
enforced, and session tokens stored unhashed are listed there and not fixed.
