from pygments.lexers import guess_lexer_for_filename
from pygments.util import ClassNotFound


def detect_language(filename, content):

    try:
        lexer = guess_lexer_for_filename(
            filename,
            content
        )

        return lexer.name

    except ClassNotFound:
        return "Unknown"


tests = [
    ("app.py", "def hello():\n    print('hello')"),
    ("App.tsx", "export default function App() { return <div>Hello</div>; }"),
    ("Main.java", "public class Main { }"),
    ("main.cpp", "#include <iostream>\nint main() {}"),
    ("server.go", "package main\nfunc main() {}"),
    ("main.rs", "fn main() {}"),
    ("SECURITY.md", "# Security Issues"),
    ("package.json", '{"name": "example"}'),
]

for filename, content in tests:

    language = detect_language(
        filename,
        content
    )

    print(
        f"{filename:15} → {language}"
    )