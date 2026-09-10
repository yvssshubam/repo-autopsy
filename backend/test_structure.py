import ast


def analyze_python_code(content):

    tree = ast.parse(content)

    imports = []
    functions = []
    classes = []

    for node in tree.body:

        # -----------------------------------------
        # Imports
        # -----------------------------------------

        if isinstance(node, ast.Import):

            for name in node.names:
                imports.append(name.name)

        elif isinstance(node, ast.ImportFrom):

            imports.append(
                node.module or ""
            )

        # -----------------------------------------
        # Top-level functions
        # -----------------------------------------

        elif isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef)
        ):

            functions.append({
                "name": node.name,
                "line": node.lineno
            })

        # -----------------------------------------
        # Classes
        # -----------------------------------------

        elif isinstance(node, ast.ClassDef):

            methods = []

            for child in node.body:

                if isinstance(
                    child,
                    (
                        ast.FunctionDef,
                        ast.AsyncFunctionDef
                    )
                ):

                    methods.append({
                        "name": child.name,
                        "line": child.lineno
                    })

            classes.append({
                "name": node.name,
                "line": node.lineno,
                "methods": methods
            })

    return {
        "imports": imports,
        "functions": functions,
        "classes": classes
    }


# ============================================================
# TEST
# ============================================================

sample_code = """
import os
import httpx
from fastapi import FastAPI
from pathlib import Path


class RepositoryAnalyzer:

    def analyze(self):
        pass

    async def fetch(self):
        pass


def load_repository():
    pass


async def fetch_file():
    pass
"""


result = analyze_python_code(
    sample_code
)


print("Imports:")

for item in result["imports"]:
    print("  -", item)


print("\nFunctions:")

for item in result["functions"]:
    print(
        f"  - {item['name']} "
        f"(line {item['line']})"
    )


print("\nClasses:")

for cls in result["classes"]:

    print(
        f"  - {cls['name']} "
        f"(line {cls['line']})"
    )

    for method in cls["methods"]:

        print(
            f"      └── {method['name']} "
            f"(line {method['line']})"
        )