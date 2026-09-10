# Repo Autopsy

Repo Autopsy maps a GitHub repository's structure and its import graph so you
can get your bearings in an unfamiliar codebase without cloning it and reading
files at random.

Paste a repository URL and you get two views. The Architecture view shows how
the project is laid out, folder by folder, and you drill into it one level at a
time. The Dependencies view takes a single file and shows what it imports and
what imports it, with repository files kept separate from third-party packages.

If you supply a Groq API key, it also writes a short description of what the
repository is for, and a one-line description of each file as you open it.

## Why it works this way

GitHub's API gives you 5,000 requests an hour, and reading one file costs one
request. A repository like `facebook/react` has over 7,000 files, so analysing
everything up front would burn the whole budget on a single repo and take
minutes to finish.

So nothing expensive happens until you ask for it. The entire Architecture view
is built from one request for the repository tree, no matter how large the repo
is. Source files are only fetched when you open a specific file. Every file that
does get read is remembered, so exploring the same area twice costs nothing the
second time.

The summaries follow the same rule. One per repository, one per file you open,
each cached against the file's blob SHA so a given version is described once and
an edit invalidates itself.

## Running it

You need Python 3.10 or newer and a GitHub personal access token. The token
needs no scopes for public repositories; it just raises your rate limit from 60
requests an hour to 5,000.

```bash
cd backend
cp .env.example .env          # then paste your token into it
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000.

The backend serves the frontend as well, so there is only one server, one port
and no CORS to configure. The startup banner tells you whether your token was
picked up and whether summaries are switched on.

## Using it

Analysing a repository loads its metadata and its tree, and drops you in the
Architecture view.

**Architecture** is the structural map. Folders are boxes, and the lines mean
"contains". Click a folder to open it, use the breadcrumb to jump back to any
level, and drag or scroll to pan and zoom. Loose config files at a level collapse
into a single node so they don't bury the structure; click it to show them.

**Dependencies** is per file. Pick a file from the list, and the graph shows what
it imports on the right and what imports it on the left. Paths are shown relative
to the file you are looking at, because where a dependency lives usually tells
you more than its filename does. External packages get their own column and a
dashed outline.

Finding what imports a given file means reading other files, so by default the
search covers the folder that file lives in. There is a toggle in the inspector
to widen it to the whole repository when you need a complete answer.

## Summaries

Optional. Without `GROQ_API_KEY` set, everything above works exactly the same and
no summary lines appear.

Prompts carry structure rather than raw source: the imports, function names and
class names the backend already extracted for the dependency graph, plus the
first 600 characters of the file. A list of exports usually describes a file
better than eighty lines of imports and licence header, and costs a fraction of
the tokens.

Two models, both configurable:

| Setting | Default | Used for |
| --- | --- | --- |
| `SUMMARY_MODEL` | `openai/gpt-oss-20b` | one line per file, so speed matters |
| `REPO_MODEL` | `openai/gpt-oss-120b` | one paragraph per repository, cached |

These are reasoning models, so the token ceiling covers thinking as well as the
answer. Setting it too low returns an empty response with no error at all, which
is worth knowing if you swap in a different model.

Every failure path is silent by design. No key, a rate limit, a network error or
an exhausted budget all leave the graph working and the summary line absent.

## Rate limiting

Two budgets are worth protecting: the model quota, and the shared GitHub quota
that breaks the app for everyone when it runs out.

Limits apply per client, per minute and per day. Endpoints are grouped by what
they actually cost:

| Group | Default | Endpoints |
| --- | --- | --- |
| `summary` | 12/min, 120/day | the two summary endpoints |
| `github` | 45/min, 800/day | anything that reads source |
| `cheap` | 120/min, 4000/day | tree and architecture, served from cache |

There is also a reserve: once GitHub's hourly remaining drops below
`GITHUB_RESERVE`, source analysis returns 503 while the structural views keep
working. That stops one expensive scan from breaking the app for the rest of the
hour.

Client identity is the peer IP. `TRUST_PROXY` makes the server read
`X-Forwarded-For` instead, which is right behind a reverse proxy and wrong
everywhere else, since anyone can set that header themselves.

All of it is tunable from `.env`. The defaults suit one person on a laptop. If
you put this on the public internet, tighten the summary numbers first.

## What it can resolve

| Language | Resolves |
| --- | --- |
| Python | relative imports, absolute imports against common source roots (`src/`, `lib/`, `app/`, `backend/`) |
| JavaScript, TypeScript | relative imports, monorepo and alias specifiers such as `shared/ReactSymbols`, workspace entry points |
| C, C++ | quoted includes, relative and from the repository root |
| Java | package paths mapped to files |

Go and Rust imports are extracted but not yet resolved to files, so they show up
as external. TypeScript `paths` mappings from `tsconfig.json` aren't read yet
either, so aliases that only exist there will look external.

## API

Everything lives under `/api`. The endpoints that cost nothing beyond the cached
tree:

```
POST /api/analyze                              repository metadata
GET  /api/repository/tree                      every path in the repo
GET  /api/repository/architecture              top level
GET  /api/repository/architecture/expand       one level down
GET  /api/health                               status and rate limit
```

The ones that read source:

```
GET  /api/repository/file                      one file's contents
GET  /api/repository/file/dependencies         imports and importers
GET  /api/repository/architecture/dependencies a folder's imports
```

The ones that call a model:

```
GET  /api/repository/summary                   what the project is for
GET  /api/repository/file/summary              what one file does
```

`file/dependencies` takes a `scope` of `none`, `directory` or `repository`, which
controls how far the search for importers goes.
`architecture/dependencies` takes a `depth` of `shallow` or `deep`. Deep recurses
through subfolders and costs one request per file, so it is opt-in.

Interactive docs are at http://127.0.0.1:8000/docs.

## Layout

```
backend/
  main.py             the whole API
  .env.example        copy to .env and add your keys
frontend/
  index.html
  css/style.css       base design
  css/graph-ui.css    graph, controls, tabs
  js/script.js        state, rendering, navigation
```

## Not built yet

Task impact analysis ("if I change this, what breaks") is the next feature, and
the one place where an agent loop earns its complexity: searching the tree,
reading candidates, following imports, and returning a ranked list of files to
change. Symbol-level analysis and test relationships come after that. The
Overview and Task impact tabs are still placeholders.