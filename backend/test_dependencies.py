import ast
from pathlib import Path


# ============================================================
# PYTHON DEPENDENCY ANALYZER
# ============================================================

class PythonDependencyAnalyzer:

    def __init__(self, content, file_path, repository_files):
        self.content = content
        self.file_path = file_path
        self.repository_files = repository_files

    # --------------------------------------------------------
    # Extract imports
    # --------------------------------------------------------

    def extract_imports(self):

        tree = ast.parse(self.content)

        imports = []

        for node in ast.walk(tree):

            # import requests
            # import requests.cookies

            if isinstance(node, ast.Import):

                for alias in node.names:

                    imports.append({
                        "type": "import",
                        "module": alias.name,
                        "line": node.lineno
                    })

            # from requests.cookies import X

            elif isinstance(
                node,
                ast.ImportFrom
            ):

                imports.append({
                    "type": "from_import",
                    "module": node.module or "",
                    "line": node.lineno
                })

        return imports

    # --------------------------------------------------------
    # Convert Python module to possible file paths
    # --------------------------------------------------------

    def possible_paths(self, module):

        parts = module.split(".")

        paths = []

        # requests.cookies
        #
        # → requests/cookies.py
        #
        # → requests/cookies/__init__.py

        module_path = "/".join(parts)

        paths.append(
            f"{module_path}.py"
        )

        paths.append(
            f"{module_path}/__init__.py"
        )

        # Also check src layout
        #
        # src/requests/cookies.py

        paths.append(
            f"src/{module_path}.py"
        )

        paths.append(
            f"src/{module_path}/__init__.py"
        )

        return paths

    # --------------------------------------------------------
    # Resolve module
    # --------------------------------------------------------

    def resolve_import(self, module):

        possible_paths =self.possible_paths(module)

        for repository_file in self.repository_files:

            normalized =repository_file.replace(
                    "\\",
                    "/"
                )

            for possible in possible_paths:

                if normalized.endswith(
                    possible
                ):

                    return {
                        "resolved": True,
                        "path": normalized,
                        "type": "internal"
                    }

        return {
            "resolved": False,
            "path": None,
            "type": "external"
        }

    # --------------------------------------------------------
    # Analyze dependencies
    # --------------------------------------------------------

    def analyze(self):

        imports =self.extract_imports()

        dependencies = []

        for item in imports:

            resolution =self.resolve_import(
                    item["module"]
                )

            dependencies.append({

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

                "path":
                    resolution["path"]

            })

        return dependencies


# ============================================================
# TEST REPOSITORY FILES
# ============================================================

repository_files = [

    "src/requests/__init__.py",

    "src/requests/api.py",

    "src/requests/cookies.py",

    "src/requests/models.py",

    "src/requests/sessions.py",

    "src/requests/status_codes.py",

    "src/requests/utils.py",

]


# ============================================================
# TEST SOURCE FILE
# ============================================================

test_content = """

import os

import requests

from requests.cookies import RequestsCookieJar

from requests.models import Response

from pathlib import Path

"""


# ============================================================
# RUN TEST
# ============================================================

if __name__ == "__main__":

    analyzer = PythonDependencyAnalyzer(

        content=test_content,

        file_path="src/requests/api.py",

        repository_files=repository_files

    )

    dependencies =analyzer.analyze()


    print()

    print(
        "Repository Dependency Analysis"
    )

    print(
        "==============================="
    )

    print()


    for dependency in dependencies:

        print(
            f"Line {dependency['line']}: "
            f"{dependency['module']}"
        )

        print(
            f"  Type: "
            f"{dependency['dependency_type']}"
        )

        print(
            f"  Resolved: "
            f"{dependency['resolved']}"
        )

        print(
            f"  Path: "
            f"{dependency['path']}"
        )

        print()