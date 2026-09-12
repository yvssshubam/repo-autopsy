# Repo Autopsy

Repo Autopsy maps a GitHub repository's structure and its import graph so you
can get your bearings in an unfamiliar codebase without cloning it and reading
files at random.

Paste a repository URL and you get four views:

- **Architecture** shows how the project is laid out, folder by folder, and you
  drill into it one level at a time.
- **Dependencies** takes a single file and shows what it imports and what
  imports it, with repository files kept separate from third-party packages.
- **Task impact** takes a change you describe in a sentence and works out which
  files it would touch, with a row of suggested changes above the box so you do
  not have to invent one.
- **Overview** carries the repository's metrics and a description of what the
  project is for.

Task impact and the written descriptions need a Groq API key. Without one, the
graphs work exactly the same, the Overview metrics still fill in, and the rest
is simply absent.

## Why it works this way

GitHub's API gives you 5,000 requests an hour, and reading one file costs one
request. A repository like `facebook/react` has over 7,000 files, so analysing
everything up front would burn the whole budget on a single repo and take
minutes to finish.

So nothing expensive happens until you ask for it. The entire Architecture view
is built from one request for the repository tree, no matter how large the repo
is. Source files are only fetched when you open a specific file. Every file that
does get read is remembered, so exploring the same area twice costs nothing the
second time, and the answer to "what imports this" gets better the more of the
repository you have explored.

## Running it

You need Python 3.10 or newer and a GitHub personal access token. The token
needs no scopes for public repositories.

```bash
cd backend
cp .env.example .env          # then paste your keys into it
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000.

The backend serves the frontend as well, so there is one server, one port and no
CORS to configure. The startup banner reports whether each key was picked up.

## The views

**Architecture.** Folders are boxes and the lines mean "contains". Click a
folder to open it, use the breadcrumb to jump back to any level, drag and scroll
to pan and zoom. Loose config files collapse into one node so they don't bury
the structure; click it to show them. A repository root with six folders and
twenty-seven dotfiles draws as eleven nodes rather than thirty-four.

**Dependencies.** Pick a file and the graph shows what it imports on the right
and what imports it on the left. Paths are relative to the file you are looking
at, because where a dependency lives usually tells you more than its filename
does. External packages get their own column and a dashed outline, grouped by
package so eighteen imports from one crate draw as one node with a count.

Finding what imports a file means reading other files, so the search covers that
file's folder by default. A toggle in the inspector widens it to the whole
repository.

**Task impact.** Describe a change in a sentence, like "add support for
contesting duplicate-charge disputes". A model gets three tools built from the
endpoints above: search the tree, read a file's structure and opening lines, and
trace a file's dependencies. It explores, then returns a ranked list of files
with a one-line reason and a confidence level for each, plus what it could not
determine.

Expect twenty to forty seconds. Every step it took is listed, and every file in
the answer opens straight into its dependency graph, so you can check the
reasoning rather than take it on faith. It will sometimes be wrong; that is why
the steps are visible.

**Overview.** Four cards: file count, declared dependencies, symbols, and how
many source files have a test named after them. Two of those are deliberately
partial and say so underneath. The symbol count only covers files you have
opened, because counting all of them means reading all of them, so it grows as
you explore and shows its denominator. The coverage card shows nothing at all on
projects that keep their tests in one file rather than one per module, because
the only number it could compute there measures the naming convention rather
than the coverage.

## Where the task suggestions come from

The task box asks the hardest question of whoever knows the repository least, so
there is a row of suggested changes above it. Clicking one fills the box rather
than running it, since the wording is what the agent searches on and you usually
want to edit it first.

They come from three places and each chip says which:

- **Requested** is an open issue labelled as a feature. The app asks the
  repository which labels it actually uses rather than guessing at
  "enhancement", then queries that label and sorts by discussion. These link out
  to the issue so you can check them yourself.
- **No tests** and **largest file** are read off the tree, with no model
  involved, so they cannot be invented. Vendored and generated files are
  excluded, which matters more than it sounds: without that filter the largest
  file in Datasette is a minified CodeMirror bundle, and the suggestion is to
  split it.
- **Guess** is a model proposal, used only when the first two come up short.
  Every path it names is checked against the tree before the chip appears.

There is deliberately no check for whether a suggested feature already exists.
Comparing the words in a task against the symbol names in the files it points at
sounds like it would work and does not. Tried against `psf/requests`, it got one
verdict right out of four: it dropped a genuinely missing feature because "http"
and "adapter" appear everywhere in an HTTP library, and it passed two features
that were already built because "redirects" does not match `resolve_redirects`.
Knowing whether a feature exists means reading the code, which is what task
impact does and what a suggestion cannot afford.

## Summaries

A line under the repository name saying what the project is for, and a line
under each file saying what it does.

Prompts carry structure rather than raw source: the imports, function names and
class names the backend already extracted for the dependency graph, plus the
first 600 characters. A list of exports usually describes a file better than
eighty lines of imports and licence header, and costs a fraction of the tokens.

Each summary is cached against the file's blob SHA, so a version is described
once and an edit invalidates itself.

## Notes on the models

These are reasoning models: the token ceiling covers thinking as well as the
answer, so setting it too low returns an empty response with no error at all.

For the agent, tool calling has to be reliable. `gpt-oss` intermittently writes
reasoning where a tool call belongs, which Groq's parser rejects with a 400, so
`qwen/qwen3.8-27b` is the default. If a tool call does come back unparseable,
the run asks for its answer as plain JSON with no tools attached, which sidesteps
the parser entirely.

The limit that bites is input tokens per minute, counted across every call in
that minute rather than per request. An agent resends its conversation on each
step, so old tool results are compressed to their first line while recent ones
stay intact. A mid-run rate limit pauses and retries rather than abandoning the
run.

Every failure path is silent by design. No key, a rate limit, a network error or
an exhausted budget leaves the graphs working and the model-written parts
absent.

## Rate limiting

Two budgets are worth protecting: the model quota, and the shared GitHub quota
that breaks the app for everyone when it runs out.

Limits apply per client, per minute and per day, grouped by what each endpoint
actually costs:

| Group | Default | Endpoints |
| --- | --- | --- |
| `agent` | 3/min, 25/day | task impact, a dozen model calls per run |
| `summary` | 12/min, 120/day | the two summary endpoints, task suggestions |
| `github` | 45/min, 800/day | anything that reads source |
| `cheap` | 120/min, 4000/day | tree, architecture and metrics, served from cache |

Once GitHub's hourly remaining drops below `GITHUB_RESERVE`, source analysis
returns 503 while the structural views keep working.

Client identity is the peer IP. `TRUST_PROXY` makes the server read
`X-Forwarded-For` instead, which is right behind a reverse proxy and wrong
everywhere else, since anyone can set that header themselves.

## What it can resolve

| Language | Resolves |
| --- | --- |
| Python | relative imports, absolute imports against common source roots (`src/`, `lib/`, `app/`, `backend/`) |
| JavaScript, TypeScript | relative imports, monorepo and alias specifiers such as `shared/ReactSymbols`, workspace entry points |
| Rust | workspace crates to their `lib.rs`, and `crate::` / `self::` / `super::` module paths |
| C, C++ | quoted includes, relative and from the repository root |
| Java | package paths mapped to files |

Go imports are extracted but not resolved to files, so they show as external.
TypeScript `paths` mappings from `tsconfig.json` aren't read either, so aliases
that only exist there will look external.

## API

Everything lives under `/api`. The endpoints that cost nothing beyond the cached
tree:

```
POST /api/analyze                              repository metadata
GET  /api/repository/tree                      every path in the repo
GET  /api/repository/architecture              top level
GET  /api/repository/architecture/expand       one level down
GET  /api/repository/metrics                   the overview cards
GET  /api/health                               status and rate limit
```

The ones that read source:

```
GET  /api/repository/file                      one file's contents
GET  /api/repository/file/dependencies         imports and importers
GET  /api/repository/file/tests                tests covering one file
GET  /api/repository/diff                      what a range of commits touches
GET  /api/repository/architecture/dependencies a folder's imports
```

The ones that call a model:

```
GET  /api/repository/summary                   what the project is for
GET  /api/repository/file/summary              what one file does
GET  /api/impact/suggestions                   changes worth asking about
POST /api/impact                               which files a change touches
```

`file/dependencies` takes a `scope` of `none`, `directory` or `repository`.
`architecture/dependencies` takes a `depth` of `shallow` or `deep`; deep recurses
and costs one request per file, so it is opt-in. `impact/suggestions` only
reaches a model when a repository has no labelled feature requests, so most of
the time it costs two GitHub reads and nothing else.

Interactive docs are at http://127.0.0.1:8000/docs.

## Layout

```
backend/
  main.py             the whole API
  .env.example        copy to .env and add your keys
frontend/
  index.html
  css/style.css       base design
  css/graph-ui.css    graph, controls, tabs, suggestions
  js/script.js        state, rendering, navigation
```

## Not built yet

Symbol counts for files nobody has opened, which needs a cheaper source than
reading every one of them. Test coverage that means something on projects that
do not name tests per file, since name matching scores `psf/requests` at 32% and
that repository is thoroughly tested. And architecture boundaries derived from
actual imports rather than from folder names.