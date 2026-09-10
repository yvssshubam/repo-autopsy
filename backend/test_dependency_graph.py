import asyncio
from test_repo_dependencies import (
    get_repository_tree,
    get_file_content,
    analyze_file
)


async def build_dependency_graph(
    owner,
    repo,
    branch
):

    print()
    print("Building dependency graph...")
    print()

    # --------------------------------------------------------
    # Get real repository tree
    # --------------------------------------------------------

    repository_files = await get_repository_tree(
        owner,
        repo,
        branch
    )

    python_files = [
        path
        for path in repository_files
        if path.endswith(".py")
    ]

    nodes = set()
    edges = set()

    # --------------------------------------------------------
    # Analyze every Python file
    # --------------------------------------------------------

    for index, file_path in enumerate(
        python_files
    ):

        print(
            f"[{index + 1}/{len(python_files)}] "
            f"{file_path}"
        )

        nodes.add(file_path)

        content = await get_file_content(
            owner,
            repo,
            branch,
            file_path
        )

        if content is None:
            continue

        dependencies = analyze_file(
            file_path,
            content,
            repository_files
        )

        # ----------------------------------------------------
        # Keep only internal dependencies
        # ----------------------------------------------------

        for dependency in dependencies:

            if (
                dependency["dependency_type"]
                != "internal"
            ):
                continue

            target = dependency["target"]

            if not target:
                continue

            # A graph edge is unique based on:
            #
            # source → target

            edges.add(
                (
                    file_path,
                    target
                )
            )

    # --------------------------------------------------------
    # Convert graph into JSON-friendly structure
    # --------------------------------------------------------

    graph = {

        "nodes": [
            {
                "id": node
            }
            for node in sorted(nodes)
        ],

        "edges": [
            {
                "source": source,
                "target": target,
                "type": "import"
            }

            for source, target
            in sorted(edges)
        ]

    }

    # --------------------------------------------------------
    # Results
    # --------------------------------------------------------

    print()
    print("======================================")
    print("DEPENDENCY GRAPH")
    print("======================================")
    print()

    print(
        "Nodes:",
        len(graph["nodes"])
    )

    print(
        "Edges:",
        len(graph["edges"])
    )

    print()

    print("Sample edges:")
    print()

    for edge in graph["edges"][:30]:

        print(
            f"{edge['source']}"
            f"  →  "
            f"{edge['target']}"
        )

    print()

    return graph


if __name__ == "__main__":

    asyncio.run(
        build_dependency_graph(
            owner="psf",
            repo="requests",
            branch="main"
        )
    )