import ast
import httpx
import os
import re
import asyncio
from dotenv import load_dotenv


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


# ============================================================
# GITHUB HEADERS
# ============================================================

def github_headers():

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = (
            f"Bearer {GITHUB_TOKEN}"
        )

    return headers


# ============================================================
# FETCH GITHUB DATA
# ============================================================

async def github_get(url):

    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True
    ) as client:

        response = await client.get(
            url,
            headers=github_headers()
        )

        return response


# ============================================================
# GET REPOSITORY TREE
# ============================================================

async def get_repository_tree(
    owner,
    repo,
    branch
):

    url = (
        f"https://api.github.com/repos/"
        f"{owner}/{repo}/git/trees/"
        f"{branch}?recursive=1"
    )

    response = await github_get(url)

    print(
        "Tree status:",
        response.status_code
    )

    if response.status_code != 200:

        print(
            response.text
        )

        raise Exception(
            "Unable to retrieve repository tree."
        )

    data = response.json()

    if data.get("truncated"):

        raise Exception(
            "Repository tree was truncated."
        )

    files = [
        item["path"]
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]

    return files


# ============================================================
# GET FILE CONTENT
# ============================================================

async def get_file_content(
    owner,
    repo,
    branch,
    path
):

    url = (
        f"https://api.github.com/repos/"
        f"{owner}/{repo}/contents/"
        f"{path}?ref={branch}"
    )

    response = await github_get(url)

    if response.status_code != 200:

        print(
            "File failed:",
            path,
            response.status_code
        )

        return None

    data = response.json()

    if data.get("type") != "file":

        return None

    import base64

    try:

        return base64.b64decode(
            data["content"]
        ).decode("utf-8")

    except (
        UnicodeDecodeError,
        ValueError,
        KeyError
    ):

        return None


# ============================================================
# PYTHON IMPORT EXTRACTION
# ============================================================

def extract_imports(content):

    try:

        tree = ast.parse(content)

    except SyntaxError:

        return []

    imports = []

    for node in ast.walk(tree):

        # ----------------------------------------------------
        # import requests
        # import requests.cookies
        # ----------------------------------------------------

        if isinstance(
            node,
            ast.Import
        ):

            for alias in node.names:

                imports.append({
                    "module": alias.name,
                    "type": "import",
                    "line": node.lineno
                })


        # ----------------------------------------------------
        # from requests.cookies import X
        # ----------------------------------------------------

        elif isinstance(
            node,
            ast.ImportFrom
        ):

            if node.module:

                imports.append({
                    "module": node.module,
                    "type": "from_import",
                    "line": node.lineno
                })

    return imports


# ============================================================
# POSSIBLE MODULE PATHS
# ============================================================

def possible_paths(module):

    parts = module.split(".")

    module_path = "/".join(parts)

    return [

        f"{module_path}.py",

        f"{module_path}/__init__.py",

        f"src/{module_path}.py",

        f"src/{module_path}/__init__.py"

    ]


# ============================================================
# RESOLVE IMPORT
# ============================================================

def resolve_import(
    module,
    repository_files
):

    candidates =possible_paths(module)

    for file_path in repository_files:

        normalized =file_path.replace(
                "\\",
                "/"
            )

        for candidate in candidates:

            if normalized.endswith(
                candidate
            ):

                return {
                    "resolved": True,
                    "type": "internal",
                    "path": normalized
                }

    return {
        "resolved": False,
        "type": "external",
        "path": None
    }


# ============================================================
# ANALYZE ONE FILE
# ============================================================

def analyze_file(
    file_path,
    content,
    repository_files
):

    imports =extract_imports(content)

    dependencies = []

    for item in imports:

        resolution =resolve_import(
                item["module"],
                repository_files
            )

        dependencies.append({

            "source": file_path,

            "module":
                item["module"],

            "line":
                item["line"],

            "import_type":
                item["type"],

            "dependency_type":
                resolution["type"],

            "resolved":
                resolution["resolved"],

            "target":
                resolution["path"]

        })

    return dependencies


# ============================================================
# MAIN REPOSITORY ANALYSIS
# ============================================================

async def analyze_repository(
    owner,
    repo,
    branch
):

    print()
    print(
        "======================================"
    )
    print(
        "Repository Dependency Analysis"
    )
    print(
        "======================================"
    )
    print()


    # --------------------------------------------------------
    # STEP 1
    # --------------------------------------------------------

    print(
        "[1/3] Loading repository tree..."
    )


    repository_files =await get_repository_tree(
            owner,
            repo,
            branch
        )


    python_files = [

        path

        for path in repository_files

        if path.endswith(".py")

    ]


    print(
        f"Total files: "
        f"{len(repository_files)}"
    )

    print(
        f"Python files: "
        f"{len(python_files)}"
    )

    print()


    # --------------------------------------------------------
    # STEP 2
    # --------------------------------------------------------

    print(
        "[2/3] Fetching Python files..."
    )


    all_dependencies = []

    successful_files = 0

    failed_files = 0


    for index, file_path in enumerate(
        python_files
    ):

        print(
            f"[{index + 1}/"
            f"{len(python_files)}] "
            f"{file_path}"
        )


        content =await get_file_content(
                owner,
                repo,
                branch,
                file_path
            )


        if content is None:

            failed_files += 1

            continue


        successful_files += 1


        dependencies =analyze_file(
                file_path,
                content,
                repository_files
            )


        all_dependencies.extend(
            dependencies
        )


        # ----------------------------------------------------
        # Small delay to avoid hammering
        # GitHub
        # ----------------------------------------------------

        await asyncio.sleep(
            0.05
        )


    print()


    # --------------------------------------------------------
    # STEP 3
    # --------------------------------------------------------

    print(
        "[3/3] Building dependency graph..."
    )

    print()


    internal_dependencies = [

        dependency

        for dependency in all_dependencies

        if dependency[
            "dependency_type"
        ] == "internal"

    ]


    external_dependencies = [

        dependency

        for dependency in all_dependencies

        if dependency[
            "dependency_type"
        ] == "external"

    ]


    # ========================================================
    # RESULTS
    # ========================================================

    print(
        "======================================"
    )

    print(
        "RESULTS"
    )

    print(
        "======================================"
    )

    print()


    print(
        "Python files analyzed:",
        successful_files
    )

    print(
        "Python files failed:",
        failed_files
    )

    print(
        "Total imports:",
        len(all_dependencies)
    )

    print(
        "Internal dependencies:",
        len(internal_dependencies)
    )

    print(
        "External dependencies:",
        len(external_dependencies)
    )

    print()


    # ========================================================
    # INTERNAL DEPENDENCIES
    # ========================================================

    print(
        "--------------------------------------"
    )

    print(
        "INTERNAL DEPENDENCIES"
    )

    print(
        "--------------------------------------"
    )

    print()


    for dependency in internal_dependencies:

        print(
            f"{dependency['source']}"
        )

        print(
            f"  └── imports "
            f"{dependency['module']}"
        )

        print(
            f"      → "
            f"{dependency['target']}"
        )

        print()


    # ========================================================
    # EXTERNAL DEPENDENCIES
    # ========================================================

    print(
        "--------------------------------------"
    )

    print(
        "EXTERNAL DEPENDENCIES"
    )

    print(
        "--------------------------------------"
    )

    print()


    unique_external = sorted({

        dependency["module"]

        for dependency
        in external_dependencies

    })


    for module in unique_external:

        print(
            f"  • {module}"
        )


    print()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(

        analyze_repository(

            owner="psf",

            repo="requests",

            branch="main"

        )

    )