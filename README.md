# Repo Autopsy

Repo Autopsy maps a GitHub repository's structure and its import graph so you
can get your bearings in an unfamiliar codebase without cloning it and reading
files at random.

Paste a repository URL and you get two views. The Architecture view shows how
the project is laid out, folder by folder, and you drill into it one level at a
time. The Dependencies view takes a single file and shows what it imports and
what imports it, with repository files kept separate from third-party packages.

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
picked up.

## Using it

Analysing a repository loads its metadata and its tree, and drops you in the
Architecture view.

**Architecture** is the structural map. Folders are boxes, and the lines mean
"contains". Click a folder to open it, use the breadcrumb to jump back to any
level, and drag or scroll to pan and zoom. Loose config files at a level collapse
into a single node so they don't bury the structure; click it to show them.

**Dependencies** is per file. Pick a file from the list, and the graph shows what
it imports on the right and what imports it on the left. Paths are shown relative
to the file you are looking at, because where a dependency lives usually tells you
more than its filename does. External packages get their own column and a dashed
outline.

Finding what imports a given file means reading other files, so by default the
search covers the folder that file lives in. There is a toggle in the inspector
to widen it to the whole repository when you need a complete answer.

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

`file/dependencies` takes a `scope` of `none`, `directory` or `repository`, which
controls how far the search for importers goes.
`architecture/dependencies` takes a `depth` of `shallow` or `deep`. Deep recurses
through subfolders and costs one request per file, so it is opt-in.

Interactive docs are at http://127.0.0.1:8000/docs.

## Layout

```
backend/
  main.py             the whole API
  .env.example        copy to .env and add your token
frontend/
  index.html
  css/style.css       base design
  css/graph-ui.css    graph, controls, tabs
  js/script.js        state, rendering, navigation
```

## Not built yet

Symbol-level analysis, task impact ("if I change this, what breaks"), test
relationships, and architecture boundaries derived from actual imports rather
than from folder names. The Overview and Task impact tabs are still placeholders.