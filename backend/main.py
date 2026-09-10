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
import ast
import time
import base64
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query
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
    print(f"Allowed origins:     {', '.join(ALLOWED_ORIGINS)}")
    print(f"Concurrent fetches:  {FETCH_CONCURRENCY}")
    print("Import resolution:   Python, JS/TS, C/C++, Java")
    print("Dependency scans:    shallow by default, deep on request")
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

    for imported in analysis["imports"]:

        target = resolve_import(imported, file_path, language, repository_files)

        if target and target != file_path:
            resolved.append(target)
            continue

        # A relative import that did not resolve is a file we could not
        # find, not a third-party package. Labelling it external turned
        # "./missing" into a package called "." in the graph.
        if str(imported).strip("\"'").startswith("."):
            continue

        label = external_label(imported, language)

        if label and label not in (".", ".."):
            external.append(label)

    entry = {
        "language": language,
        "resolved": list(dict.fromkeys(resolved)),
        "external": list(dict.fromkeys(external)),
        "functions": analysis["functions"],
        "classes": analysis["classes"],
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


@app.post("/api/analyze")
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


@app.get("/api/repository/tree")
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


@app.get("/api/repository/architecture")
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


@app.get("/api/repository/architecture/expand")
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


@app.get("/api/repository/file")
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

@app.get("/api/repository/file/dependencies")
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

    for target in entry["resolved"]:
        nodes.append({
            "id": target,
            "path": target,
            "label": Path(target).name,
            "type": "file",
            "role": "import",
            "language": detect_language(target),
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
# FOLDER DEPENDENCIES
# ============================================================

@app.get("/api/repository/architecture/dependencies")
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


@app.get("/api/repository/dependencies")
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