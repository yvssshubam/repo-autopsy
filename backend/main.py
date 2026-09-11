"""
Repo Autopsy — backend

Progressive repository intelligence:

    metadata  ->  tree  ->  architecture level  ->  file dependencies

Nothing expensive happens until the user asks for it. Every endpoint
below is either a tree operation (zero GitHub file fetches) or a
bounded, explicitly-scoped scan.
"""

import os
import re
import json
import ast
import time
import base64
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ============================================================
# ENVIRONMENT
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Where the frontend is served from. Keep this narrow; "*" is fine for
# local development but should not survive to a deployed instance.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://127.0.0.1:5500,http://localhost:5500,"
        "http://127.0.0.1:3000,http://localhost:3000",
    ).split(",")
    if origin.strip()
]


# ============================================================
# SUMMARIES (optional)
#
# Off unless GROQ_API_KEY is set. Everything below fails silently:
# no key, a rate limit or a network error means the graph renders
# exactly as it does without summaries.
# ============================================================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_URL = os.getenv(
    "GROQ_URL",
    "https://api.groq.com/openai/v1/chat/completions",
)

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "openai/gpt-oss-20b")
REPO_MODEL = os.getenv("REPO_MODEL", "openai/gpt-oss-120b")

# The free tier allows 1,000 requests a day across the organisation.
# Stop short of it so the last stretch of a session degrades quietly
# instead of throwing a wall of 429s.
DAILY_CALL_BUDGET = int(os.getenv("DAILY_CALL_BUDGET", "900"))

# How much of a file's opening goes into the prompt. Structure carries
# most of the meaning, so this stays small: the whole prompt lands near
# 150 tokens, which keeps 30 requests a minute inside the 8K TPM cap.
HEAD_CHARACTERS = 600


# ============================================================
# TUNING
# ============================================================

TREE_CACHE_TTL_SECONDS = 15 * 60

FETCH_CONCURRENCY = 20

# Reverse ("imported by") scanning is the only place we fetch more than
# one file for a single request, so it is explicitly bounded.
REVERSE_SCAN_LIMIT_DIRECTORY = 80
REVERSE_SCAN_LIMIT_REPOSITORY = 600

MAX_FILE_BYTES = 1_000_000


# ============================================================
# APP LIFECYCLE
# ============================================================

_shared_client: httpx.AsyncClient | None = None


def get_shared_client() -> httpx.AsyncClient:
    """One connection-pooled client for the whole process. Opening a
    fresh connection per file is what made bulk scans take minutes."""

    global _shared_client

    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=40,
            ),
        )

    return _shared_client


@asynccontextmanager
async def lifespan(app: FastAPI):

    print()
    print("=" * 60)
    print("REPO AUTOPSY BACKEND")
    print("=" * 60)
    print(f"GitHub token:        {'present' if GITHUB_TOKEN else 'MISSING'}")
    print(
        f"Summaries:           "
        f"{'on (' + SUMMARY_MODEL + ')' if GROQ_API_KEY else 'off (no GROQ_API_KEY)'}"
    )
    print(f"Allowed origins:     {', '.join(ALLOWED_ORIGINS)}")
    print(f"Concurrent fetches:  {FETCH_CONCURRENCY}")
    print("Import resolution:   Python, JS/TS, C/C++, Java")
    print("Dependency scans:    shallow by default, deep on request")
    print(
        f"Per-client limits:   summary {RATE_BUCKETS['summary'][0]}/min, "
        f"github {RATE_BUCKETS['github'][0]}/min"
    )
    print("=" * 60)
    print()

    yield

    global _shared_client

    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()


app = FastAPI(title="Repo Autopsy API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class RepositoryRequest(BaseModel):
    url: str


# ============================================================
# RATE LIMITING
#
# The daily budget above protects the account in aggregate. This
# protects it from one visitor: without a per-client limit, a single
# person holding down refresh can spend the whole day's quota in a
# couple of minutes, and everyone else gets nothing.
#
# Two costs are worth defending. Model calls are the expensive one.
# GitHub reads are the slower one, and exhausting that quota breaks
# the app for every user for an hour.
# ============================================================

# Behind a proxy the peer address is the proxy, so the real client is
# in X-Forwarded-For. Only trust that header when you actually run
# behind something that sets it, or anyone can spoof an identity.
TRUST_PROXY = os.getenv("TRUST_PROXY", "").lower() in ("1", "true", "yes")

# bucket -> (per minute, per day)
RATE_BUCKETS = {
    "summary": (
        int(os.getenv("SUMMARY_RPM", "12")),
        int(os.getenv("SUMMARY_RPD", "120")),
    ),
    "github": (
        int(os.getenv("GITHUB_RPM", "45")),
        int(os.getenv("GITHUB_RPD", "800")),
    ),
    "cheap": (
        int(os.getenv("CHEAP_RPM", "120")),
        int(os.getenv("CHEAP_RPD", "4000")),
    ),
    # One run is a dozen model calls and up to ten file reads, so this
    # is deliberately the tightest bucket.
    "agent": (
        int(os.getenv("AGENT_RPM", "3")),
        int(os.getenv("AGENT_RPD", "25")),
    ),
}

# Keep some GitHub quota in reserve so an expensive scan can never
# leave the cheap structural views broken for the rest of the hour.
GITHUB_RESERVE = int(os.getenv("GITHUB_RESERVE", "250"))

# client -> bucket -> {"minute": [timestamps], "day": (date, count)}
_rate_state: dict[str, dict[str, dict]] = {}

_rate_last_swept = 0.0


def client_id(request: Request) -> str:

    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()

    return request.client.host if request.client else "unknown"


def sweep_rate_state(now: float):
    """Drop clients that have gone quiet, so memory does not grow with
    every visitor that ever hit the service."""

    global _rate_last_swept

    if now - _rate_last_swept < 300:
        return

    _rate_last_swept = now

    for key in list(_rate_state.keys()):

        buckets = _rate_state[key]

        active = any(
            bucket["minute"] and now - bucket["minute"][-1] < 3600
            for bucket in buckets.values()
        )

        if not active:
            del _rate_state[key]


def rate_limit(bucket: str):
    """FastAPI dependency. Raises 429 with Retry-After when a client is
    over either window."""

    async def check(request: Request):

        # Read the bucket per call rather than closing over it, so the
        # limits stay adjustable at runtime.
        per_minute, per_day = RATE_BUCKETS[bucket]

        now = time.time()
        today = time.strftime("%Y-%m-%d")

        sweep_rate_state(now)

        key = client_id(request)

        state = _rate_state.setdefault(key, {})
        entry = state.setdefault(bucket, {"minute": [], "day": (today, 0)})

        # Sliding minute.
        entry["minute"] = [
            stamp for stamp in entry["minute"] if now - stamp < 60
        ]

        if len(entry["minute"]) >= per_minute:
            oldest = entry["minute"][0]
            retry = max(1, int(60 - (now - oldest)))
            raise HTTPException(
                status_code=429,
                detail=(
                    "Slow down a moment — too many requests. "
                    f"Try again in {retry}s."
                ),
                headers={"Retry-After": str(retry)},
            )

        # Calendar day.
        day, count = entry["day"]

        if day != today:
            day, count = today, 0

        if count >= per_day:
            raise HTTPException(
                status_code=429,
                detail="Daily limit reached for this client. Resets at midnight UTC.",
                headers={"Retry-After": "3600"},
            )

        entry["minute"].append(now)
        entry["day"] = (day, count + 1)

    return check


async def guard_github_quota():
    """Refuse expensive work when the GitHub budget is nearly gone, so
    the cheap structural endpoints keep working."""

    remaining = RATE_LIMIT.get("remaining")

    if remaining is not None and remaining < GITHUB_RESERVE:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Only {remaining} GitHub requests left this hour, so "
                "source analysis is paused. Structure still works."
            ),
        )


# ============================================================
# GITHUB
# ============================================================

def github_headers():

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


# Updated from every response so the frontend can show remaining budget
# instead of discovering exhaustion as a mysterious 403.
RATE_LIMIT = {"limit": None, "remaining": None, "reset": None}


def record_rate_limit(response: httpx.Response):

    limit = response.headers.get("x-ratelimit-limit")
    remaining = response.headers.get("x-ratelimit-remaining")
    reset = response.headers.get("x-ratelimit-reset")

    if limit is not None:
        RATE_LIMIT["limit"] = int(limit)

    if remaining is not None:
        RATE_LIMIT["remaining"] = int(remaining)

    if reset is not None:
        RATE_LIMIT["reset"] = int(reset)


async def github_get(url: str, client: httpx.AsyncClient | None = None):
    """GET with retry. Raises HTTPException only after three attempts —
    callers doing bulk fetches must therefore tolerate exceptions
    (see gather(..., return_exceptions=True) below)."""

    active_client = client if client is not None else get_shared_client()

    last_error = None

    for attempt in range(3):

        try:
            response = await active_client.get(url, headers=github_headers())
            record_rate_limit(response)
            return response

        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
        ) as error:

            last_error = error
            print(f"GitHub request failed {attempt + 1}/3: {error}")

            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

    raise HTTPException(
        status_code=502,
        detail=f"Unable to reach GitHub ({last_error}).",
    )


GITHUB_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[^/\s]+)/"
    r"(?P<repo>[^/\s#?]+)"
    r"(?:/.*)?$",
    re.IGNORECASE,
)


def parse_github_url(url: str):
    """Accepts the forms people actually paste:

        https://github.com/owner/repo
        https://github.com/owner/repo.git
        https://github.com/owner/repo/tree/main/src
        github.com/owner/repo
    """

    match = GITHUB_URL_PATTERN.match(url.strip())

    if not match:
        raise HTTPException(
            status_code=400,
            detail="That does not look like a GitHub repository URL.",
        )

    owner = match.group("owner")
    repo = re.sub(r"\.git$", "", match.group("repo"))

    return owner, repo


# ============================================================
# LANGUAGES AND FILTERS
# ============================================================

LANGUAGE_MAP = {
    ".py": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".mts": "TypeScript", ".cts": "TypeScript",
    ".java": "Java",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".hpp": "C++", ".hh": "C++",
    ".go": "Go",
    ".rs": "Rust",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".dart": "Dart",
    ".lua": "Lua",
    ".r": "R",
    ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".sass": "Sass",
    ".sql": "SQL",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML",
    ".xml": "XML",
    ".toml": "TOML",
    ".md": "Markdown",
}

# Languages we can actually extract and resolve imports for. Dependency
# scans only fetch these — downloading Markdown and JSON to look for
# imports that cannot exist is pure rate-limit waste.
ANALYZABLE_LANGUAGES = {
    "Python", "JavaScript", "TypeScript", "Java", "C", "C++", "Go", "Rust",
}

IGNORED_DIRECTORIES = {
    ".git", ".github", ".idea", ".vscode",
    "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "venv", ".venv", "env",
    "dist", "build", "out", "target",
    ".next", ".nuxt",
    "coverage", "vendor", "Pods",
    "bin", "obj", "tmp", "temp",
}

IGNORED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".mp3", ".wav", ".mp4", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".7z",
    ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".woff", ".woff2", ".ttf", ".eot",
    ".lock",
}

IMPORTANT_FILES = {
    "README.md", "package.json", "requirements.txt", "pyproject.toml",
    "Dockerfile", "docker-compose.yml", "pom.xml", "go.mod", "Cargo.toml",
}

# Directories that commonly hold a project's import root, used to
# resolve absolute Python imports in src/-style layouts.
COMMON_SOURCE_ROOTS = ("", "src", "lib", "app", "backend", "server", "python")


def should_ignore(path: str) -> bool:

    for part in Path(path).parts:
        if part in IGNORED_DIRECTORIES:
            return True

    return Path(path).suffix.lower() in IGNORED_EXTENSIONS


def detect_language(path: str) -> str:

    filename = Path(path).name

    if filename == "Dockerfile":
        return "Dockerfile"

    if filename == "Makefile":
        return "Makefile"

    return LANGUAGE_MAP.get(Path(path).suffix.lower(), "Unknown")


def is_source_file(path: str) -> bool:
    """A file worth *displaying* as source."""

    if should_ignore(path):
        return False

    return Path(path).suffix.lower() in LANGUAGE_MAP


def is_analyzable_file(path: str) -> bool:
    """A file worth *fetching* during a dependency scan."""

    if should_ignore(path):
        return False

    return detect_language(path) in ANALYZABLE_LANGUAGES


def normalize_path(path: str) -> str:
    """Collapse '..' / '.' segments and force forward slashes so a
    resolved candidate can be compared against repository tree paths."""

    parts: list[str] = []

    for part in path.replace("\\", "/").split("/"):

        if part in ("", "."):
            continue

        if part == "..":
            if parts:
                parts.pop()
            continue

        parts.append(part)

    return "/".join(parts)


# ============================================================
# TREE CACHE
# ============================================================

TREE_CACHE: dict[str, dict] = {}


def cache_key(owner: str, repo: str, branch: str) -> str:
    return f"{owner}/{repo}@{branch}"


async def get_tree(owner: str, repo: str, branch: str):

    key = cache_key(owner, repo, branch)
    cached = TREE_CACHE.get(key)

    if cached and (time.time() - cached["fetched_at"]) < TREE_CACHE_TTL_SECONDS:
        return cached["tree"]

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/git/trees/{branch}?recursive=1"
    )

    response = await github_get(url)

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Repository or branch not found.",
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "GitHub rejected the request. The API rate limit is "
                f"{RATE_LIMIT.get('remaining')}/{RATE_LIMIT.get('limit')}."
            ),
        )

    if response.status_code != 200:
        print("GitHub tree error:", response.text[:400])
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve the repository tree.",
        )

    data = response.json()

    if data.get("truncated"):
        raise HTTPException(
            status_code=413,
            detail=(
                "This repository is too large for GitHub to return in one "
                "tree response."
            ),
        )

    tree = data.get("tree", [])

    TREE_CACHE[key] = {"tree": tree, "fetched_at": time.time()}

    print(f"Tree loaded for {key}: {len(tree)} entries")

    return tree


def tree_totals(tree) -> dict:

    files = 0
    directories = 0

    for item in tree:
        if item.get("type") == "blob":
            files += 1
        elif item.get("type") == "tree":
            directories += 1

    return {"totalFiles": files, "totalDirectories": directories}


# ============================================================
# SOURCE ANALYSIS
# ============================================================

def analyze_python(content: str) -> dict:

    result = {"imports": [], "functions": [], "classes": []}

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return result

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for item in node.names:
                result["imports"].append(item.name)

        elif isinstance(node, ast.ImportFrom):
            # Leading dots encode the relative level so the resolver can
            # tell "from . import x" from "from ..pkg import x".
            level = node.level or 0
            module = node.module or ""
            result["imports"].append(("." * level) + module)

    for node in tree.body:

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"].append(node.name)

        elif isinstance(node, ast.ClassDef):
            result["classes"].append(node.name)

    return result


JS_IMPORT_PATTERNS = (
    r'import\s+[^;]*?\s+from\s+["\'](.+?)["\']',
    r'import\s*["\'](.+?)["\']',
    r'require\s*\(\s*["\'](.+?)["\']\s*\)',
    r'export\s+[^;]*?\s+from\s+["\'](.+?)["\']',
    r'import\s*\(\s*["\'](.+?)["\']\s*\)',
)

GO_IMPORT_BLOCK = re.compile(r'import\s*\(([^)]*)\)', re.MULTILINE)
GO_IMPORT_SINGLE = re.compile(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.MULTILINE)
GO_QUOTED = re.compile(r'"([^"]+)"')


def analyze_imports(content: str, language: str) -> list[str]:

    imports: list[str] = []

    if language in ("JavaScript", "TypeScript"):
        for pattern in JS_IMPORT_PATTERNS:
            imports.extend(re.findall(pattern, content))

    elif language == "Java":
        imports.extend(
            re.findall(r'^\s*import\s+(?:static\s+)?([^;]+);', content, re.MULTILINE)
        )

    elif language in ("C", "C++"):
        # Keep the bracket type: only quoted includes can be local files.
        for quote, header in re.findall(
            r'^\s*#include\s*([<"])([^>"]+)[>"]', content, re.MULTILINE
        ):
            imports.append(header if quote == '"' else f"<{header}>")

    elif language == "Go":
        # Scoped to import declarations — a bare quoted-string regex
        # matches every string literal in the file.
        for block in GO_IMPORT_BLOCK.findall(content):
            imports.extend(GO_QUOTED.findall(block))
        imports.extend(GO_IMPORT_SINGLE.findall(content))

    elif language == "Rust":
        imports.extend(re.findall(r'^\s*use\s+([^;]+);', content, re.MULTILINE))

    return list(dict.fromkeys(item.strip() for item in imports if item.strip()))


def analyze_source(path: str, content: str) -> dict:

    language = detect_language(path)

    result = {
        "language": language,
        "imports": [],
        "functions": [],
        "classes": [],
    }

    if language == "Python":
        result.update(analyze_python(content))
        result["language"] = language
    else:
        result["imports"] = analyze_imports(content, language)
        result["functions"] = re.findall(
            r'\b(?:function|func|fn)\s+([A-Za-z_]\w*)', content
        )
        result["classes"] = re.findall(r'\bclass\s+([A-Za-z_]\w*)', content)

    return result


# ============================================================
# IMPORT RESOLUTION
# ============================================================

def resolve_python_import(imported: str, file_path: str, repository_files: set):

    level = len(imported) - len(imported.lstrip("."))
    remainder = imported[level:]
    module_parts = remainder.split(".") if remainder else []

    bases: list[Path] = []

    if level > 0:
        base_dir = Path(file_path).parent

        for _ in range(level - 1):
            base_dir = base_dir.parent

        bases.append(
            base_dir.joinpath(*module_parts) if module_parts else base_dir
        )

    else:
        if not module_parts:
            return None

        # Absolute import. Try each plausible source root — a src/ layout
        # means "myapp.utils" lives at "src/myapp/utils.py", and trying
        # only the repo root resolves nothing.
        for root in COMMON_SOURCE_ROOTS:
            bases.append(Path(root).joinpath(*module_parts) if root
                         else Path(*module_parts))

        # Also try dropping the leading segment, which covers the common
        # case of the package directory sharing the repo name.
        if len(module_parts) > 1:
            bases.append(Path(*module_parts[1:]))

    for base in bases:

        for candidate in (
            normalize_path(str(base) + ".py"),
            normalize_path(str(base / "__init__.py")),
        ):
            if candidate in repository_files:
                return candidate

    return None


JS_EXTENSION_CANDIDATES = ("", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
JS_INDEX_CANDIDATES = ("index.js", "index.ts", "index.jsx", "index.tsx")


def resolve_bare_js_import(imported: str, repository_files: set):
    """A bare specifier is not always a package.

    Monorepos and tsconfig/webpack aliases mean "shared/ReactSymbols"
    or "store" can be a file in this very repository. Resolving only
    relative paths made every one of those look like a third-party
    dependency, so a file's real internal imports never appeared in
    the graph."""

    name = imported.strip("/")

    if not name or name.startswith("@") and "/" not in name:
        return None

    suffixes = []

    for extension in JS_EXTENSION_CANDIDATES:
        if not extension:
            continue
        suffixes.append(f"/{name}{extension}")
        # Workspace packages usually point at an entry file rather than
        # sitting at the package root.
        for entry in ("index", "src/index", "lib/index"):
            suffixes.append(f"/{name}/{entry}{extension}")

    matches = [
        candidate for candidate in repository_files
        if any(candidate.endswith(suffix) for suffix in suffixes)
        and is_analyzable_file(candidate)
    ]

    # Also allow a match rooted at the repository itself.
    for extension in JS_EXTENSION_CANDIDATES:
        if not extension:
            continue
        for candidate in (
            f"{name}{extension}",
            f"{name}/index{extension}",
            f"{name}/src/index{extension}",
        ):
            if candidate in repository_files:
                matches.append(candidate)

    if not matches:
        return None

    # Shallowest path wins: "packages/shared/x.js" over
    # "packages/a/node_modules/shared/x.js".
    return min(matches, key=lambda path: (path.count("/"), len(path)))


def resolve_js_import(imported: str, file_path: str, repository_files: set):

    if not imported.startswith("."):
        return resolve_bare_js_import(imported, repository_files)

    base = Path(file_path).parent / imported

    candidates = [
        normalize_path(str(base) + extension)
        for extension in JS_EXTENSION_CANDIDATES
    ]

    candidates += [
        normalize_path(str(base / index_file))
        for index_file in JS_INDEX_CANDIDATES
    ]

    for candidate in candidates:
        if candidate in repository_files:
            return candidate

    return None


def resolve_c_include(imported: str, file_path: str, repository_files: set):

    if imported.startswith("<"):
        return None

    candidate = normalize_path(str(Path(file_path).parent / imported))

    if candidate in repository_files:
        return candidate

    root_candidate = normalize_path(imported)

    if root_candidate in repository_files:
        return root_candidate

    return None


def resolve_java_import(imported: str, file_path: str, repository_files: set):

    parts = [part for part in imported.split(".") if part]

    if len(parts) < 2 or parts[-1] == "*":
        return None

    suffix = "/".join(parts) + ".java"

    for candidate in repository_files:
        if candidate.endswith(suffix):
            return candidate

    return None


RUST_BUILTIN_ROOTS = {"std", "core", "alloc", "proc_macro", "test"}


def rust_crate_source_root(file_path: str) -> str | None:
    """The `src` directory of the crate a file belongs to."""

    parts = file_path.split("/")

    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "src":
            return "/".join(parts[: index + 1])

    return None


def resolve_rust_import(imported: str, file_path: str, repository_files: set):
    """Rust `use` paths were all being treated as third-party crates.

    In a workspace like react's compiler most of them are crates in the
    same repository, so a file that imports eighteen things from
    `react_compiler_ast` drew eighteen external nodes instead of one
    dependency on that crate's lib.rs."""

    # "foo::bar::{A, B}" and "foo::bar as baz" both describe one target.
    path = imported.split("{")[0].split(" as ")[0].strip().strip(":")

    segments = [part for part in path.split("::") if part]

    if not segments:
        return None

    first = segments[0]

    if first in RUST_BUILTIN_ROOTS:
        return None

    source_root = rust_crate_source_root(file_path)

    # Paths inside the current crate.
    if first in ("crate", "self", "super"):

        rest = segments[1:]

        # Rust's module tree is not the directory tree. A file
        # src/program.rs is the module `program`, so `super` from
        # inside it means src/, not the directory above src/.
        directory = "/".join(file_path.split("/")[:-1])
        stem = file_path.split("/")[-1].removesuffix(".rs")

        if stem in ("mod", "lib", "main"):
            self_base = directory
            super_base = "/".join(directory.split("/")[:-1])
        else:
            self_base = f"{directory}/{stem}"
            super_base = directory

        if first == "super":
            base = super_base
        elif first == "self":
            base = self_base
        else:
            if not source_root:
                return None
            base = source_root

        # The last segment is usually a type or function, so try the
        # module path both with and without it.
        for tail in (rest, rest[:-1]):

            if not tail:
                continue

            joined = "/".join(tail)

            for candidate in (
                normalize_path(f"{base}/{joined}.rs"),
                normalize_path(f"{base}/{joined}/mod.rs"),
            ):
                if candidate in repository_files:
                    return candidate

        return None

    # A crate in this workspace: find its library root.
    for suffix in (
        f"/{first}/src/lib.rs",
        f"/{first}/lib.rs",
        f"/{first}/src/main.rs",
    ):
        matches = [
            candidate for candidate in repository_files
            if candidate.endswith(suffix)
        ]
        if matches:
            return min(matches, key=lambda item: (item.count("/"), len(item)))

    for candidate in (f"{first}/src/lib.rs", f"{first}/lib.rs"):
        if candidate in repository_files:
            return candidate

    return None


def resolve_import(imported: str, file_path: str, language: str,
                   repository_files: set):

    if not imported:
        return None

    imported = str(imported).strip("\"'").strip()

    if not imported:
        return None

    if language == "Python":
        return resolve_python_import(imported, file_path, repository_files)

    if language in ("JavaScript", "TypeScript"):
        return resolve_js_import(imported, file_path, repository_files)

    if language in ("C", "C++"):
        return resolve_c_include(imported, file_path, repository_files)

    if language == "Java":
        return resolve_java_import(imported, file_path, repository_files)

    if language == "Rust":
        return resolve_rust_import(imported, file_path, repository_files)

    return None


def external_label(imported: str, language: str) -> str:
    """Package name as a person would recognise it."""

    name = str(imported).strip("\"'<>").strip()

    if language == "Python":
        return name.split(".")[0] or name

    if language in ("JavaScript", "TypeScript"):
        if name.startswith("@"):
            return "/".join(name.split("/")[:2])
        return name.split("/")[0]

    if language == "Java":
        return ".".join(name.split(".")[:3])

    if language == "Rust":
        # One node per crate, not one per imported symbol.
        head = name.split("{")[0].split(" as ")[0].strip().strip(":")
        return head.split("::")[0] or name

    return name


# ============================================================
# FILE FETCH + IMPORT INDEX
# ============================================================

# repo_key -> { path: {"language", "resolved": [...], "external": [...]} }
#
# Every file we analyse is remembered here, so "imported by" gets more
# complete the more of the repository the user explores. This is the
# progressive idea applied to analysis, not just to rendering.
IMPORT_INDEX: dict[str, dict[str, dict]] = {}


async def fetch_file_text(semaphore: asyncio.Semaphore, owner: str, repo: str,
                          branch: str, file_path: str):

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/contents/{file_path}?ref={branch}"
    )

    async with semaphore:
        response = await github_get(url, client=get_shared_client())

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "GitHub rate limit reached while reading source files "
                f"({RATE_LIMIT.get('remaining')}/{RATE_LIMIT.get('limit')} "
                "remaining)."
            ),
        )

    if response.status_code != 200:
        return None

    data = response.json()

    if data.get("size", 0) > MAX_FILE_BYTES:
        return None

    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (KeyError, UnicodeDecodeError, ValueError):
        return None


def index_file(repo_key: str, file_path: str, content: str,
               repository_files: set) -> dict:

    analysis = analyze_source(file_path, content)
    language = analysis["language"]

    resolved: list[str] = []
    external: list[str] = []

    # How many symbols came from each target, so a grouped node can say
    # "6 imports" instead of hiding what it stands for.
    counts: dict[str, int] = {}

    for imported in analysis["imports"]:

        target = resolve_import(imported, file_path, language, repository_files)

        if target and target != file_path:
            resolved.append(target)
            counts[target] = counts.get(target, 0) + 1
            continue

        # A relative import that did not resolve is a file we could not
        # find, not a third-party package. Labelling it external turned
        # "./missing" into a package called "." in the graph.
        if str(imported).strip("\"'").startswith("."):
            continue

        label = external_label(imported, language)

        if label and label not in (".", ".."):
            external.append(label)
            counts[label] = counts.get(label, 0) + 1

    entry = {
        "language": language,
        "resolved": list(dict.fromkeys(resolved)),
        "external": list(dict.fromkeys(external)),
        "functions": analysis["functions"],
        "classes": analysis["classes"],
        "counts": counts,
        # Kept so a summary costs no extra GitHub request for any file
        # the user has already opened.
        "head": content[:HEAD_CHARACTERS],
    }

    IMPORT_INDEX.setdefault(repo_key, {})[file_path] = entry

    return entry


async def index_files(repo_key: str, owner: str, repo: str, branch: str,
                      paths: list[str], repository_files: set) -> dict:
    """Fetch and index a batch of files. Failures are counted, never
    fatal — one unreachable file must not sink the whole graph."""

    known = IMPORT_INDEX.setdefault(repo_key, {})
    pending = [path for path in paths if path not in known]

    if not pending:
        return {"fetched": 0, "failed": 0, "skipped": len(paths)}

    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    results = await asyncio.gather(
        *[
            fetch_file_text(semaphore, owner, repo, branch, path)
            for path in pending
        ],
        return_exceptions=True,
    )

    fetched = 0
    failed = 0
    rate_limited = False

    for path, content in zip(pending, results):

        if isinstance(content, HTTPException) and content.status_code == 403:
            rate_limited = True
            failed += 1
            continue

        if isinstance(content, BaseException) or content is None:
            failed += 1
            continue

        index_file(repo_key, path, content, repository_files)
        fetched += 1

    return {
        "fetched": fetched,
        "failed": failed,
        "skipped": len(paths) - len(pending),
        "rateLimited": rate_limited,
    }


# ============================================================
# MODEL CALLS
# ============================================================

# sha or repo key -> summary text
SUMMARY_CACHE: dict[str, str] = {}

CALL_BUDGET = {"date": None, "used": 0}


def budget_remaining() -> int:

    today = time.strftime("%Y-%m-%d")

    if CALL_BUDGET["date"] != today:
        CALL_BUDGET["date"] = today
        CALL_BUDGET["used"] = 0

    return DAILY_CALL_BUDGET - CALL_BUDGET["used"]


async def call_model(model: str, system: str, user: str,
                     max_tokens: int = 400) -> str | None:
    """Returns the text, or None for any reason at all. A summary is a
    nicety; nothing upstream should have to handle its absence as an
    error."""

    if not GROQ_API_KEY:
        return None

    if budget_remaining() <= 0:
        print("Summary budget for today is spent.")
        return None

    CALL_BUDGET["used"] += 1

    try:
        response = await get_shared_client().post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # gpt-oss models reason before answering, and the
                # reasoning comes out of the same token budget. Too low
                # a ceiling and the whole allowance goes on thinking,
                # leaving content empty with no error at all.
                "max_completion_tokens": max_tokens,
                "reasoning_effort": "low",
                "temperature": 0.2,
            },
        )

    except Exception as error:
        print("Summary request failed:", error)
        return None

    if response.status_code == 429:
        print("Summary rate limit reached.")
        return None

    if response.status_code != 200:
        print("Summary error:", response.status_code, response.text[:200])
        return None

    try:
        choice = response.json()["choices"][0]
    except (KeyError, IndexError, ValueError):
        print("Summary response had no choices.")
        return None

    message = choice.get("message", {})
    text = (message.get("content") or "").strip()

    if not text:
        # Some reasoning models put everything in `reasoning` when the
        # answer gets truncated. Say so rather than failing silently.
        print(
            "Summary came back empty "
            f"(finish_reason={choice.get('finish_reason')}, "
            f"reasoning={len(message.get('reasoning') or '')} chars). "
            "Raise the token ceiling if this repeats."
        )
        return None

    return " ".join(text.split()).strip() or None


FILE_SYSTEM_PROMPT = (
    "You describe source files for a developer reading an unfamiliar "
    "codebase. Reply with ONE sentence, under 20 words, saying what the "
    "file does. No preamble, no file name, no markdown. If the evidence "
    "is thin, say what it appears to do."
)

REPO_SYSTEM_PROMPT = (
    "You explain what a software project is for, to a developer who has "
    "just opened it. Reply with two short sentences: what it does, and "
    "how it is organised. No preamble, no markdown, no bullet points."
)


def build_file_prompt(path: str, entry: dict | None, head: str) -> str:
    """Structure first, source second. A list of exports says more about
    a file's purpose than eighty lines of imports and licence header,
    and costs a tenth of the tokens."""

    lines = [f"File: {path}", f"Language: {detect_language(path)}"]

    if entry:

        if entry.get("resolved"):
            lines.append(
                "Imports from this repo: "
                + ", ".join(entry["resolved"][:8])
            )

        if entry.get("external"):
            lines.append("Packages: " + ", ".join(entry["external"][:8]))

        if entry.get("functions"):
            lines.append("Functions: " + ", ".join(entry["functions"][:12]))

        if entry.get("classes"):
            lines.append("Classes: " + ", ".join(entry["classes"][:8]))

    if head:
        lines.append("Opening lines:\n" + head)

    return "\n".join(lines)


# ============================================================
# ARCHITECTURE LEVELS
# ============================================================

def build_level_nodes(tree, prefix: str) -> list[dict]:
    """Direct children of `prefix`, with descendant counts. One pass over
    the tree rather than one pass per child."""

    children: dict[str, dict] = {}
    descendant_files: dict[str, int] = {}
    descendant_dirs: dict[str, int] = {}

    prefix_length = len(prefix)

    for item in tree:

        path = item.get("path", "")

        if not path or should_ignore(path):
            continue

        if prefix and not path.startswith(prefix):
            continue

        remaining = path[prefix_length:] if prefix else path

        if not remaining:
            continue

        parts = remaining.split("/")
        name = parts[0]
        child_path = f"{prefix}{name}" if prefix else name

        is_directory = len(parts) > 1 or item.get("type") == "tree"

        if name not in children:
            children[name] = {
                "id": child_path,
                "path": child_path,
                "label": name,
                "type": "directory" if is_directory else "file",
                "expandable": is_directory,
                "important": name in IMPORTANT_FILES,
                "language": None if is_directory else detect_language(child_path),
                "size": None if is_directory else item.get("size"),
            }

        # Attribute this entry to the child bucket it lives under.
        if len(parts) > 1:
            if item.get("type") == "blob":
                descendant_files[name] = descendant_files.get(name, 0) + 1
            elif item.get("type") == "tree":
                descendant_dirs[name] = descendant_dirs.get(name, 0) + 1

    for name, node in children.items():
        if node["type"] == "directory":
            node["descendantFileCount"] = descendant_files.get(name, 0)
            node["descendantDirectoryCount"] = descendant_dirs.get(name, 0)
            node["count"] = (
                descendant_files.get(name, 0) + descendant_dirs.get(name, 0)
            )

    return sorted(
        children.values(),
        key=lambda node: (
            0 if node["type"] == "directory" else 1,
            node["label"].lower(),
        ),
    )


# ============================================================
# ROUTES
# ============================================================

@app.get("/api/health")
async def health():
    """Quick check that the backend is alive and how much GitHub budget
    is left. Not on "/" — that path now serves the frontend."""

    return {
        "status": "ok",
        "service": "Repo Autopsy API",
        "githubToken": bool(GITHUB_TOKEN),
        "rateLimit": RATE_LIMIT,
    }


@app.post("/api/analyze", dependencies=[Depends(rate_limit("cheap"))])
async def analyze_repository(request: RepositoryRequest):

    owner, repo = parse_github_url(request.url)

    response = await github_get(f"https://api.github.com/repos/{owner}/{repo}")

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Repository not found, or it is private to this token.",
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "GitHub rejected the request "
                f"({RATE_LIMIT.get('remaining')}/{RATE_LIMIT.get('limit')} "
                "requests remaining)."
            ),
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Unable to read the repository from GitHub.",
        )

    data = response.json()

    return {
        "name": data["name"],
        "full_name": data["full_name"],
        "owner": data["owner"]["login"],
        "repo": data["name"],
        "default_branch": data["default_branch"],
        "private": data["private"],
        "html_url": data["html_url"],
        "description": data.get("description"),
        "language": data.get("language"),
        "rateLimit": RATE_LIMIT,
    }


@app.get("/api/repository/tree", dependencies=[Depends(rate_limit("cheap"))])
async def repository_tree(owner: str, repo: str, branch: str):

    tree = await get_tree(owner, repo, branch)

    files = [
        item["path"] for item in tree
        if item.get("type") == "blob" and not should_ignore(item["path"])
    ]

    directories = [
        item["path"] for item in tree
        if item.get("type") == "tree" and not should_ignore(item["path"])
    ]

    totals = tree_totals(tree)

    return {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "file_count": len(files),
        "directory_count": len(directories),
        "files": files,
        "directories": directories,
        "meta": {
            **totals,
            "visibleFiles": len(files),
            "visibleDirectories": len(directories),
        },
    }


@app.get(
    "/api/repository/architecture",
    dependencies=[Depends(rate_limit("cheap"))],
)
async def architecture(owner: str, repo: str, branch: str):

    tree = await get_tree(owner, repo, branch)

    children = [
        {**node, "level": 1} for node in build_level_nodes(tree, prefix="")
    ]

    repository_node = {
        "id": "repository",
        "path": "",
        "label": repo,
        "type": "repository",
        "expandable": True,
        "level": 0,
    }

    return {
        "path": "",
        "parent": None,
        "nodes": [repository_node, *children],
        "edges": [
            {"source": "repository", "target": node["id"], "type": "contains"}
            for node in children
        ],
        "meta": {**tree_totals(tree), "levelNodeCount": len(children)},
    }


@app.get(
    "/api/repository/architecture/expand",
    dependencies=[Depends(rate_limit("cheap"))],
)
async def architecture_expand(owner: str, repo: str, branch: str, path: str):

    path = path.strip("/")

    tree = await get_tree(owner, repo, branch)

    prefix = f"{path}/" if path else ""
    children = [
        {**node, "level": path.count("/") + 1}
        for node in build_level_nodes(tree, prefix=prefix)
    ]

    parent_id = path if path else "repository"

    # The parent is returned as a node, not just as an edge endpoint.
    # Without it the frontend drops every edge as unresolvable.
    parent_node = {
        "id": parent_id,
        "path": path,
        "label": Path(path).name if path else repo,
        "type": "directory" if path else "repository",
        "expandable": True,
        "level": path.count("/") if path else 0,
        "isParent": True,
    }

    return {
        "path": path,
        "parent": parent_node,
        "nodes": [parent_node, *children],
        "edges": [
            {"source": parent_id, "target": node["id"], "type": "contains"}
            for node in children
        ],
        "meta": {**tree_totals(tree), "levelNodeCount": len(children)},
    }


@app.get(
    "/api/repository/file",
    dependencies=[Depends(rate_limit("github")), Depends(guard_github_quota)],
)
async def repository_file(owner: str, repo: str, branch: str, path: str):

    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path.")

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/contents/{path}?ref={branch}"
    )

    response = await github_get(url)

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="File not found.")

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Unable to read the file.")

    data = response.json()

    if data.get("type") != "file":
        raise HTTPException(
            status_code=400,
            detail="That path is a directory, not a file.",
        )

    if data.get("size", 0) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="This file is too large to display.",
        )

    try:
        content = base64.b64decode(data["content"]).decode("utf-8")
    except (UnicodeDecodeError, ValueError, KeyError):
        raise HTTPException(
            status_code=400,
            detail="This file is not text and cannot be displayed.",
        )

    structure = analyze_source(path, content)

    return {
        "path": path,
        "size": data.get("size", 0),
        "language": structure["language"],
        "structure": structure,
        "content": content,
    }


# ============================================================
# FILE DEPENDENCIES  (one file out, bounded scan back)
# ============================================================

@app.get(
    "/api/repository/file/dependencies",
    dependencies=[Depends(rate_limit("github")), Depends(guard_github_quota)],
)
async def file_dependencies(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    scope: str = Query(
        "directory",
        pattern="^(none|directory|repository)$",
        description=(
            "How far to scan for files that import this one. "
            "'none' is one fetch; 'directory' scans sibling files; "
            "'repository' scans every analyzable file."
        ),
    ),
):
    """Outgoing dependencies come from parsing this one file.

    Incoming dependencies ('imported by') require reading other files, so
    the scan is bounded by `scope` and every file read is remembered in
    IMPORT_INDEX — revisit the same area and the answer gets better for
    free."""

    path = path.strip("/")

    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path.")

    tree = await get_tree(owner, repo, branch)

    repository_files = {
        item["path"] for item in tree if item.get("type") == "blob"
    }

    if path not in repository_files:
        raise HTTPException(
            status_code=404,
            detail="That file is not in this repository tree.",
        )

    repo_key = cache_key(owner, repo, branch)
    language = detect_language(path)

    if language not in ANALYZABLE_LANGUAGES:
        return {
            "path": path,
            "language": language,
            "nodes": [
                {
                    "id": path,
                    "label": Path(path).name,
                    "type": "file",
                    "role": "selected",
                    "language": language,
                    "path": path,
                }
            ],
            "edges": [],
            "meta": {
                "analyzable": False,
                "reason": (
                    f"{language} files are not parsed for imports yet."
                ),
                "scope": scope,
                "rateLimit": RATE_LIMIT,
            },
        }

    # --- outgoing: one fetch, one parse -----------------------------

    target_stats = await index_files(
        repo_key, owner, repo, branch, [path], repository_files
    )

    entry = IMPORT_INDEX.get(repo_key, {}).get(path)

    if entry is None:
        raise HTTPException(
            status_code=502,
            detail="Unable to read this file from GitHub.",
        )

    # --- incoming: bounded scan -------------------------------------

    candidates: list[str] = []

    if scope != "none":

        directory = str(Path(path).parent) if "/" in path else ""
        directory_prefix = f"{directory}/" if directory else ""

        if scope == "directory":
            candidates = [
                candidate for candidate in repository_files
                if candidate.startswith(directory_prefix)
                and "/" not in candidate[len(directory_prefix):]
                and is_analyzable_file(candidate)
            ][:REVERSE_SCAN_LIMIT_DIRECTORY]

        else:
            candidates = [
                candidate for candidate in sorted(repository_files)
                if is_analyzable_file(candidate)
            ][:REVERSE_SCAN_LIMIT_REPOSITORY]

    scan_stats = await index_files(
        repo_key, owner, repo, branch, candidates, repository_files
    )

    index = IMPORT_INDEX.get(repo_key, {})

    imported_by = sorted(
        candidate for candidate, data in index.items()
        if path in data["resolved"] and candidate != path
    )

    # --- assemble ---------------------------------------------------

    nodes = [
        {
            "id": path,
            "path": path,
            "label": Path(path).name,
            "type": "file",
            "role": "selected",
            "language": entry["language"],
        }
    ]

    edges = []

    counts = entry.get("counts", {})

    for target in entry["resolved"]:
        nodes.append({
            "id": target,
            "path": target,
            "label": Path(target).name,
            "type": "file",
            "role": "import",
            "language": detect_language(target),
            "count": counts.get(target, 1),
        })
        edges.append({"source": path, "target": target, "type": "dependency"})

    for package in entry["external"]:
        node_id = f"external:{package}"
        nodes.append({
            "id": node_id,
            "path": "",
            "label": package,
            "type": "external",
            "role": "import",
            "language": "package",
            "count": counts.get(package, 1),
        })
        edges.append({"source": path, "target": node_id, "type": "external"})

    for importer in imported_by:
        nodes.append({
            "id": importer,
            "path": importer,
            "label": Path(importer).name,
            "type": "file",
            "role": "importer",
            "language": detect_language(importer),
        })
        edges.append({"source": importer, "target": path, "type": "dependency"})

    # De-duplicate while preserving order (a file can be both).
    seen = set()
    unique_nodes = []

    for node in nodes:
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        unique_nodes.append(node)

    return {
        "path": path,
        "language": entry["language"],
        "nodes": unique_nodes,
        "edges": edges,
        "structure": {
            "functions": entry["functions"],
            "classes": entry["classes"],
        },
        "meta": {
            "analyzable": True,
            "scope": scope,
            "importCount": len(entry["resolved"]),
            "externalCount": len(entry["external"]),
            "importedByCount": len(imported_by),
            "candidatesScanned": len(candidates),
            "filesFetched": target_stats["fetched"] + scan_stats["fetched"],
            "filesFailed": target_stats["failed"] + scan_stats["failed"],
            "filesFromIndex": scan_stats["skipped"],
            "indexedFiles": len(index),
            "rateLimited": bool(
                target_stats.get("rateLimited") or scan_stats.get("rateLimited")
            ),
            "complete": scope == "repository",
            "rateLimit": RATE_LIMIT,
        },
    }


# ============================================================
# TASK IMPACT AGENT
#
# The one place in this project where a loop earns its complexity:
# you cannot know which files a change touches until you have looked
# at some and followed where they lead.
#
# The model gets the same operations the UI has, and decides what to
# open. Everything is bounded: iterations, file reads, and the size of
# each result, because an agent with a read_file tool will happily
# read forty files.
# ============================================================

# The limit that bites is input tokens per *minute*, counted across
# every call in that minute rather than per request. Six steps plus the
# final report lands near 4,500 input tokens, inside Qwen's 7,000.
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "6"))

# How long to wait out a rate limit mid-run, and how many times. A run
# that pauses ten seconds and finishes beats one that dies at step six.
AGENT_MAX_WAITS = int(os.getenv("AGENT_MAX_WAITS", "2"))
AGENT_MAX_WAIT_SECONDS = int(os.getenv("AGENT_MAX_WAIT_SECONDS", "25"))
AGENT_MAX_READS = int(os.getenv("AGENT_MAX_READS", "10"))
AGENT_MODEL = os.getenv("AGENT_MODEL", "openai/gpt-oss-120b")

AGENT_SYSTEM_PROMPT = (
    "You help a developer work out which files a change would touch in "
    "a repository you can explore with tools.\n\n"
    "Search once or twice at the start, then spend your remaining "
    "steps reading files and tracing dependencies. Never repeat a "
    "search you have already run: the results will be identical, and "
    "you only have "
    f"{AGENT_MAX_STEPS} steps in total.\n\n"
    "If a search returns nothing useful, do not search again with a "
    "similar word. Read the most plausible file you have seen so far, "
    "or report what you know.\n\n"
    "A file belongs in the report only if you have evidence from its "
    "contents or its dependencies. Say what you are unsure about in "
    "`unknowns` rather than padding the list. Four well-argued files "
    "beat twenty guesses."
)

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_repository",
            "description": (
                "Find files whose path matches any of the given words. "
                "Start here. Use several words; they are matched "
                "independently."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "words": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keywords, e.g. ['auth', 'login', 'session']",
                    }
                },
                "required": ["words"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "The opening of a file plus the functions and classes it "
                "defines. Use on files that search suggested."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dependencies",
            "description": (
                "What a file imports and what imports it. Use to find "
                "what else a change would break."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_impact",
            "description": "Deliver the answer. Call this exactly once, at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Two sentences on how the change fits this codebase.",
                    },
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "reason": {
                                    "type": "string",
                                    "description": "Why this file is affected, in one sentence.",
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                },
                            },
                            "required": ["path", "reason", "confidence"],
                        },
                    },
                    "unknowns": {
                        "type": "string",
                        "description": "What you could not determine. Empty if nothing.",
                    },
                },
                "required": ["summary", "files"],
            },
        },
    },
]


REPORT_JSON_INSTRUCTION = (
    "Stop exploring and give the answer now.\n\n"
    "Reply with a JSON object and nothing else. No markdown, no fences, "
    "no explanation around it. Shape:\n"
    '{"summary": "two sentences", "files": [{"path": "...", '
    '"reason": "one sentence", "confidence": "high|medium|low"}], '
    '"unknowns": "what you could not determine"}\n\n'
    "Use only paths you actually saw in tool results."
)


def parse_report_json(text: str) -> dict | None:
    """Pull the report out of a plain-text reply.

    Models wrap JSON in prose or code fences often enough that finding
    the object is more reliable than insisting they don't."""

    if not text:
        return None

    cleaned = text.strip()

    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                cleaned = candidate
                break

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end <= start:
        return None

    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None

    return data if isinstance(data, dict) else None


# An agent resends its whole conversation on every step, so a run that
# reads five files sends those five results again and again. That is
# what exhausts a per-minute token allowance near the end of a run,
# exactly when the answer is due.
AGENT_TOKEN_BUDGET = int(os.getenv("AGENT_TOKEN_BUDGET", "5000"))

# Tool results kept in full. Older ones are reduced to their first line,
# which is enough for the model to remember what it already looked at.
AGENT_FULL_RESULTS = 3


def estimate_tokens(messages: list) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages) // 4


def trim_conversation(messages: list) -> list:
    """Keep the task and the recent detail; compress the rest.

    Dropping old messages entirely would let the model repeat work it
    has already done, so old tool results are shortened rather than
    removed."""

    if len(messages) <= 4:
        return messages

    head = messages[:2]          # system prompt and the task
    tail = messages[2:]

    # Index of the oldest message that stays verbatim.
    full_from = len(tail)
    seen = 0

    for index in range(len(tail) - 1, -1, -1):
        if tail[index].get("role") == "tool":
            seen += 1
            if seen >= AGENT_FULL_RESULTS:
                full_from = index
                break
        full_from = index

    trimmed = []

    for index, message in enumerate(tail):

        if index >= full_from or message.get("role") != "tool":
            trimmed.append(message)
            continue

        content = str(message.get("content") or "")
        first_line = content.split("\n")[0][:120]

        trimmed.append({
            **message,
            "content": f"{first_line} … (earlier result, shortened)",
        })

    return head + trimmed


class ImpactRequest(BaseModel):
    owner: str
    repo: str
    branch: str
    task: str


async def call_model_with_tools(messages: list, tools: list | None,
                                max_tokens: int = 1200):
    """Like call_model, but returns the whole assistant message so tool
    calls survive."""

    if not GROQ_API_KEY:
        return None, "no_key"

    if budget_remaining() <= 0:
        return None, "daily_budget_spent"

    CALL_BUDGET["used"] += 1

    try:
        response = await get_shared_client().post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AGENT_MODEL,
                "messages": messages,
                **(
                    {
                        "tools": tools,
                        "tool_choice": "auto",
                    }
                    if tools
                    else {}
                ),
                "max_completion_tokens": max_tokens,
                "reasoning_effort": "low",
                "temperature": 0.2,
            },
            timeout=60,
        )
    except Exception as error:
        print("Agent request failed:", error)
        return None, "network"

    if response.status_code == 429:

        print("Agent hit the model rate limit:", response.text[:200])

        retry_after = response.headers.get("retry-after")

        try:
            wait = int(float(retry_after)) if retry_after else 0
        except ValueError:
            wait = 0

        return None, f"model_rate_limit:{wait}"

    if response.status_code == 400:
        # gpt-oss on Groq sometimes emits reasoning where a tool call
        # belongs, and the parser rejects the whole response. Known
        # behaviour, not something the prompt can fully prevent.
        body = response.text
        if "tool_use_failed" in body or "output_parse_failed" in body:
            print("Agent: model emitted prose instead of a tool call.")
            return None, "parse_failed"

    if response.status_code != 200:
        print("Agent error:", response.status_code, response.text[:300])
        return None, f"http_{response.status_code}"

    try:
        return response.json()["choices"][0]["message"], None
    except (KeyError, IndexError, ValueError):
        return None, "malformed_response"


def agent_search(tree, words: list[str]) -> list[str]:

    terms = [word.lower().strip() for word in words if word.strip()]

    if not terms:
        return []

    scored = []

    for item in tree:

        if item.get("type") != "blob":
            continue

        path = item["path"]

        if should_ignore(path) or not is_source_file(path):
            continue

        lower = path.lower()
        name = lower.split("/")[-1]

        hits = sum(1 for term in terms if term in lower)

        if not hits:
            continue

        # A match in the file's own name means more than a match in some
        # parent directory, which every file under it would share.
        name_hits = sum(1 for term in terms if term in name)

        scored.append((-name_hits, -hits, path.count("/"), path))

    scored.sort()

    return [row[-1] for row in scored[:20]]


# ============================================================
# SUMMARY ENDPOINTS
# ============================================================

def blob_sha(tree, path: str) -> str | None:

    for item in tree:
        if item.get("path") == path and item.get("type") == "blob":
            return item.get("sha")

    return None


@app.get(
    "/api/repository/file/summary",
    dependencies=[Depends(rate_limit("summary"))],
)
async def file_summary(owner: str, repo: str, branch: str, path: str):
    """One line on what a file does.

    Cached on the blob sha, so a given version of a file is summarised
    once ever and an edit invalidates itself."""

    path = path.strip("/")

    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path.")

    if not GROQ_API_KEY:
        return {"path": path, "summary": None, "reason": "no_key"}

    tree = await get_tree(owner, repo, branch)

    sha = blob_sha(tree, path)

    if sha is None:
        raise HTTPException(status_code=404, detail="File not in the tree.")

    cache_id = f"file:{sha}"

    if cache_id in SUMMARY_CACHE:
        return {"path": path, "summary": SUMMARY_CACHE[cache_id], "cached": True}

    repo_key = cache_key(owner, repo, branch)
    entry = IMPORT_INDEX.get(repo_key, {}).get(path)

    head = (entry or {}).get("head", "")

    # Only reach for the file if the dependency view has not already
    # read it, which it usually has by the time a summary is wanted.
    if not head:
        semaphore = asyncio.Semaphore(1)
        try:
            content = await fetch_file_text(
                semaphore, owner, repo, branch, path
            )
        except HTTPException:
            content = None

        head = (content or "")[:HEAD_CHARACTERS]

    if not head and not entry:
        return {"path": path, "summary": None, "reason": "unreadable"}

    summary = await call_model(
        SUMMARY_MODEL,
        FILE_SYSTEM_PROMPT,
        build_file_prompt(path, entry, head),
        max_tokens=400,
    )

    if summary:
        SUMMARY_CACHE[cache_id] = summary

    return {
        "path": path,
        "summary": summary,
        "cached": False,
        "budgetRemaining": budget_remaining(),
    }


README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md")

MANIFEST_NAMES = (
    "package.json", "pyproject.toml", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml",
)


@app.get(
    "/api/repository/summary",
    dependencies=[Depends(rate_limit("summary"))],
)
async def repository_summary(owner: str, repo: str, branch: str):
    """What this repository is for. One call per repository, cached."""

    if not GROQ_API_KEY:
        return {"summary": None, "reason": "no_key"}

    repo_key = cache_key(owner, repo, branch)
    cache_id = f"repo:{repo_key}"

    if cache_id in SUMMARY_CACHE:
        return {"summary": SUMMARY_CACHE[cache_id], "cached": True}

    tree = await get_tree(owner, repo, branch)

    paths = {item["path"] for item in tree if item.get("type") == "blob"}

    semaphore = asyncio.Semaphore(2)

    async def read(name: str, limit: int) -> str:
        if name not in paths:
            return ""
        try:
            content = await fetch_file_text(
                semaphore, owner, repo, branch, name
            )
        except HTTPException:
            return ""
        return (content or "")[:limit]

    readme = ""

    for name in README_NAMES:
        readme = await read(name, 1800)
        if readme:
            break

    manifest = ""

    for name in MANIFEST_NAMES:
        manifest = await read(name, 600)
        if manifest:
            manifest = f"{name}:\n{manifest}"
            break

    folders = [
        node["label"]
        for node in build_level_nodes(tree, prefix="")
        if node["type"] == "directory"
    ][:20]

    if not readme and not manifest and not folders:
        return {"summary": None, "reason": "nothing_to_read"}

    prompt = "\n\n".join(
        part for part in (
            f"Repository: {owner}/{repo}",
            f"Top-level folders: {', '.join(folders)}" if folders else "",
            manifest,
            f"README:\n{readme}" if readme else "",
        ) if part
    )

    summary = await call_model(
        REPO_MODEL, REPO_SYSTEM_PROMPT, prompt, max_tokens=700
    )

    if summary:
        SUMMARY_CACHE[cache_id] = summary

    return {
        "summary": summary,
        "cached": False,
        "budgetRemaining": budget_remaining(),
    }


@app.post(
    "/api/impact",
    dependencies=[Depends(rate_limit("agent")), Depends(guard_github_quota)],
)
async def task_impact(request: ImpactRequest):
    """Which files a described change would touch."""

    task = request.task.strip()

    if not task:
        raise HTTPException(status_code=400, detail="Describe the change first.")

    if len(task) > 500:
        raise HTTPException(status_code=400, detail="Keep the description short.")

    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Task impact needs a model key. Set GROQ_API_KEY.",
        )

    owner, repo, branch = request.owner, request.repo, request.branch

    tree = await get_tree(owner, repo, branch)

    repository_files = {
        item["path"] for item in tree if item.get("type") == "blob"
    }

    repo_key = cache_key(owner, repo, branch)

    folders = [
        node["label"]
        for node in build_level_nodes(tree, prefix="")
        if node["type"] == "directory"
    ][:20]

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Repository: {owner}/{repo}\n"
                f"Top-level folders: {', '.join(folders)}\n"
                f"Total files: {len(repository_files)}\n\n"
                f"Change to make: {task}"
            ),
        },
    ]

    steps: list[dict] = []
    reads = 0
    report = None

    searched: set[str] = set()
    seen_paths: list[str] = []
    stop_reason = "step_limit"
    nudged = False
    waits = 0

    async def run_tool(name: str, arguments: dict) -> str:
        """Returns a short string. Tool output goes back through the
        model on every later step, so verbosity here costs tokens for
        the rest of the run."""

        nonlocal reads

        if name == "search_repository":

            words = [
                str(word).lower().strip()
                for word in arguments.get("words", [])
                if str(word).strip()
            ]

            signature = ",".join(sorted(set(words)))

            # Repeating a search is the loop this agent fell into: the
            # same words return the same list, forever. Answer with a
            # push forward instead of the identical results.
            if signature and signature in searched:
                unread = [
                    path for path in seen_paths
                    if path not in IMPORT_INDEX.get(repo_key, {})
                ][:8]
                return (
                    "You already ran that search. Do not search again. "
                    + (
                        "Read one of these instead: " + ", ".join(unread)
                        if unread
                        else "Call report_impact with what you have."
                    )
                )

            searched.add(signature)

            matches = agent_search(tree, words)

            if not matches:
                return (
                    "Nothing matched those words. Do not try similar "
                    "words. Read a file you have already seen, or report "
                    "what you know."
                )

            for path in matches:
                if path not in seen_paths:
                    seen_paths.append(path)

            lines = []

            for path in matches:
                read_note = (
                    " [already read]"
                    if path in IMPORT_INDEX.get(repo_key, {})
                    else ""
                )
                lines.append(f"{path} ({detect_language(path)}){read_note}")

            return "\n".join(lines[:12])

        if name in ("read_file", "get_dependencies"):

            path = str(arguments.get("path", "")).strip()

            if path not in repository_files:
                return f"No such file: {path}"

            if reads >= AGENT_MAX_READS:
                return "Read limit reached. Report what you have."

            reads += 1

            await index_files(
                repo_key, owner, repo, branch, [path], repository_files
            )

            entry = IMPORT_INDEX.get(repo_key, {}).get(path)

            if entry is None:
                return f"Could not read {path}."

            if name == "read_file":
                parts = [f"{path} ({entry['language']})"]
                if entry["functions"]:
                    parts.append("Functions: " + ", ".join(entry["functions"][:20]))
                if entry["classes"]:
                    parts.append("Classes: " + ", ".join(entry["classes"][:12]))
                parts.append(entry.get("head", "")[:500])
                return "\n".join(parts)

            index = IMPORT_INDEX.get(repo_key, {})

            importers = [
                other for other, data in index.items()
                if path in data["resolved"] and other != path
            ]

            return (
                f"{path}\n"
                f"Imports: {', '.join(entry['resolved'][:8]) or 'none in repo'}\n"
                f"Packages: {', '.join(entry['external'][:6]) or 'none'}\n"
                f"Imported by (files read so far): "
                f"{', '.join(importers[:8]) or 'none found yet'}"
            )

        return f"Unknown tool: {name}"

    for step in range(AGENT_MAX_STEPS):

        message, error = await call_model_with_tools(
            trim_conversation(messages), AGENT_TOOLS, max_tokens=700
        )

        if error and error.startswith("model_rate_limit"):

            requested = error.split(":")[-1]
            wait = min(
                AGENT_MAX_WAIT_SECONDS,
                max(5, int(requested) if requested.isdigit() else 8),
            )

            if waits < AGENT_MAX_WAITS:

                waits += 1

                steps.append({
                    "tool": "note",
                    "detail": f"paused {wait}s for the model's rate limit",
                })

                await asyncio.sleep(wait)
                continue

            stop_reason = "model_rate_limit"
            break

        if error == "parse_failed":
            # The model wrote prose where a tool call belonged. Asking
            # again rarely helps, so go straight to the plain-JSON
            # request, which does not involve the tool parser at all.
            steps.append({
                "tool": "note",
                "detail": "tool call was unparseable; asked for the answer directly",
            })
            break

        if message is None:
            stop_reason = error or "model_unavailable"
            break

        tool_calls = message.get("tool_calls") or []

        if not tool_calls:

            # It answered in prose instead of using a tool. Ask once,
            # then give up rather than looping on the same behaviour.
            if nudged:
                stop_reason = "stopped_using_tools"
                break

            nudged = True

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
            })
            messages.append({
                "role": "user",
                "content": (
                    "Use a tool. Either read a file you have seen, or "
                    "call report_impact with what you know."
                ),
            })

            steps.append({
                "tool": "note",
                "detail": "answered without using a tool; asked again",
            })

            continue

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        })

        finished = False

        for call in tool_calls:

            name = call.get("function", {}).get("name", "")
            raw = call.get("function", {}).get("arguments") or "{}"

            try:
                arguments = json.loads(raw)
            except ValueError:
                arguments = {}

            if name == "report_impact":
                report = arguments
                steps.append({"tool": name, "detail": "delivered the report"})
                finished = True
                break

            result = await run_tool(name, arguments)

            steps.append({
                "tool": name,
                "detail": (
                    ", ".join(arguments.get("words", []))
                    if name == "search_repository"
                    else str(arguments.get("path", ""))
                ),
                "found": len(result.splitlines()),
            })

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result[:900],
            })

        if finished:
            break

    if report is None and stop_reason in ("step_limit", "parse_failed"):

        # No tools on this call, so Groq's tool parser is out of the
        # picture. Whatever the model has established still produces an
        # answer instead of an empty result.
        messages.append({"role": "user", "content": REPORT_JSON_INSTRUCTION})

        final, final_error = await call_model_with_tools(
            trim_conversation(messages), None, max_tokens=900
        )

        # The report is the one call worth waiting for.
        if final is None and str(final_error).startswith("model_rate_limit"):
            await asyncio.sleep(min(AGENT_MAX_WAIT_SECONDS, 10))
            final, final_error = await call_model_with_tools(
                trim_conversation(messages), None, max_tokens=900
            )

        if final is not None:
            report = parse_report_json(final.get("content") or "")

            if report:
                steps.append({
                    "tool": "report_impact",
                    "detail": "answered directly after exploring",
                })
            else:
                stop_reason = "unparseable_report"
        elif final_error:
            stop_reason = final_error.split(":")[0]

    if report is None:

        explanations = {
            "step_limit": (
                "It used all its steps without reaching a conclusion. "
                "A narrower description usually helps."
            ),
            "model_rate_limit": (
                "The model's per-minute input token limit was reached, "
                "and waiting did not clear it. Try again in a minute, or "
                "set AGENT_MODEL to groq/compound, which allows far more "
                "tokens per minute."
            ),
            "daily_budget_spent": (
                "The daily model budget is spent. It resets tomorrow."
            ),
            "network": "The model could not be reached.",
            "stopped_using_tools": (
                "The model stopped exploring before it had an answer. "
                "Try describing the change more concretely."
            ),
            "malformed_response": "The model returned something unusable.",
            "parse_failed": (
                "The model wrote prose where a tool call belonged, which "
                "this model does intermittently. Try again, or set "
                "AGENT_MODEL to qwen/qwen3.8-27b."
            ),
            "unparseable_report": (
                "It explored the repository but its final answer could "
                "not be read. Try again, or set AGENT_MODEL to "
                "qwen/qwen3.8-27b."
            ),
        }

        return {
            "task": task,
            "summary": None,
            "files": [],
            "steps": steps,
            "filesRead": reads,
            "budgetRemaining": budget_remaining(),
            "incomplete": True,
            "stopReason": stop_reason,
            "reason": explanations.get(
                stop_reason,
                f"The run stopped early ({stop_reason}).",
            ),
        }

    # Only report files that exist. A model naming a plausible path that
    # is not in the repository is the failure mode worth catching.
    verified = []

    for item in report.get("files", []):

        path = str(item.get("path", "")).strip()

        if path in repository_files:
            verified.append({
                "path": path,
                "reason": item.get("reason", ""),
                "confidence": item.get("confidence", "medium"),
            })

    return {
        "task": task,
        "summary": report.get("summary", ""),
        "files": verified,
        "unknowns": report.get("unknowns", ""),
        "dropped": len(report.get("files", [])) - len(verified),
        "steps": steps,
        "filesRead": reads,
        "budgetRemaining": budget_remaining(),
    }


# ============================================================
# FOLDER DEPENDENCIES
# ============================================================

@app.get(
    "/api/repository/architecture/dependencies",
    dependencies=[Depends(rate_limit("github")), Depends(guard_github_quota)],
)
async def architecture_dependencies(
    owner: str,
    repo: str,
    branch: str,
    path: str = "",
    depth: str = Query(
        "shallow",
        pattern="^(shallow|deep)$",
        description=(
            "'shallow' analyses only files directly inside the folder. "
            "'deep' recurses — one GitHub request per file, so it is "
            "opt-in."
        ),
    ),
):

    path = path.strip("/")

    tree = await get_tree(owner, repo, branch)

    prefix = f"{path}/" if path else ""

    repository_files = {
        item["path"] for item in tree if item.get("type") == "blob"
    }

    def in_scope(candidate: str) -> bool:

        if not candidate.startswith(prefix):
            return False

        if depth == "shallow" and "/" in candidate[len(prefix):]:
            return False

        return is_analyzable_file(candidate)

    source_files = sorted(
        candidate for candidate in repository_files if in_scope(candidate)
    )

    truncated = False

    if depth == "deep" and len(source_files) > REVERSE_SCAN_LIMIT_REPOSITORY:
        source_files = source_files[:REVERSE_SCAN_LIMIT_REPOSITORY]
        truncated = True

    repo_key = cache_key(owner, repo, branch)

    stats = await index_files(
        repo_key, owner, repo, branch, source_files, repository_files
    )

    index = IMPORT_INDEX.get(repo_key, {})

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node_id: str, label: str, node_type: str, language: str):
        if node_id not in nodes:
            nodes[node_id] = {
                "id": node_id,
                "path": node_id if node_type == "file" else "",
                "label": label,
                "type": node_type,
                "language": language,
            }

    for file_path in source_files:

        entry = index.get(file_path)

        if entry is None:
            continue

        add_node(file_path, Path(file_path).name, "file", entry["language"])

        for target in entry["resolved"]:
            add_node(target, Path(target).name, "file", detect_language(target))
            edges.append(
                {"source": file_path, "target": target, "type": "dependency"}
            )

        for package in entry["external"]:
            node_id = f"external:{package}"
            add_node(node_id, package, "external", "package")
            edges.append(
                {"source": file_path, "target": node_id, "type": "external"}
            )

    return {
        "path": path,
        "nodes": list(nodes.values()),
        "edges": edges,
        "meta": {
            "depth": depth,
            "filesScanned": len(source_files),
            "filesFetched": stats["fetched"],
            "filesFromIndex": stats["skipped"],
            "filesFailed": stats["failed"],
            "truncated": truncated,
            "rateLimit": RATE_LIMIT,
        },
    }


@app.get(
    "/api/repository/dependencies",
    dependencies=[Depends(rate_limit("github")), Depends(guard_github_quota)],
)
async def legacy_dependencies(owner: str, repo: str, branch: str):
    """Kept for older frontends. Routes to the shallow root scan."""

    return await architecture_dependencies(
        owner=owner, repo=repo, branch=branch, path="", depth="shallow"
    )


# ============================================================
# FRONTEND
#
# The API and the page are served from one origin, so there is no
# CORS preflight, no second server, and no port to keep free.
#
# This mount MUST stay last: it matches every path the API routes
# above did not claim.
# ============================================================

FRONTEND_DIR = BASE_DIR.parent / "frontend"

if FRONTEND_DIR.is_dir():

    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="frontend",
    )

else:

    print(f"WARNING: no frontend directory at {FRONTEND_DIR}")