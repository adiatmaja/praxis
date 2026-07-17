import ast
import logging
import os
import re
from pathlib import Path


logger = logging.getLogger(__name__)


class SkeletonTransformer(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = [node.body[0], ast.Pass()]
        else:
            node.body = [ast.Pass()]
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = [node.body[0], ast.Pass()]
        else:
            node.body = [ast.Pass()]
        return node


def get_python_skeleton(code: str) -> str:
    try:
        tree = ast.parse(code)
        transformed = SkeletonTransformer().visit(tree)
        return ast.unparse(transformed)
    except Exception:
        return ""


def get_non_python_skeleton(code: str) -> str:
    lines = []
    pattern = re.compile(r"^\s*(class|def|function|interface|type)\b")
    for line in code.splitlines():
        if pattern.match(line):
            lines.append(line)
    return "\n".join(lines)


def truncate_markdown(md: str, max_chars: int) -> str:
    if len(md) <= max_chars:
        return md

    truncated = md[:max_chars]

    # Fix broken markdown code blocks deterministically
    # Count the number of unescaped ```
    block_markers = truncated.count("```")
    if block_markers % 2 != 0:
        # We are inside a code block, close it
        # Try to find the last newline to avoid breaking a line in half
        last_nl = truncated.rfind("\n")
        if last_nl != -1:
            truncated = truncated[:last_nl]
        truncated += "\n```"
    else:
        # We are not in a code block, just truncate at last newline
        last_nl = truncated.rfind("\n")
        if last_nl != -1:
            truncated = truncated[:last_nl]

    return truncated


def get_one_hop_importers(repo_dir: str, declared_files: list[str]) -> list[str]:
    importers = set()
    stems = {Path(f).stem for f in declared_files if Path(f).suffix == ".py"}
    stems.discard("__init__")

    if not stems:
        return []

    import_re = re.compile(r"^(?:import|from)\s+([\w\.]+)", re.MULTILINE)

    try:
        for root, _dirs, files in os.walk(repo_dir):
            if ".git" in root.split(os.sep):
                continue
            for file in files:
                if not file.endswith(".py"):
                    continue
                full_path = os.path.join(root, file)
                try:
                    rel_path = os.path.relpath(full_path, repo_dir)
                    if rel_path in declared_files:
                        continue

                    with open(full_path, encoding="utf-8") as f:
                        content = f.read()

                    for match in import_re.finditer(content):
                        mod_path = match.group(1)
                        parts = mod_path.split(".")
                        if parts and parts[-1] in stems:
                            importers.add(rel_path)
                            break
                except Exception as exc:  # noqa: BLE001 - skip unreadable file
                    logger.debug("context_pack: skipping %s: %s", full_path, exc)
    except Exception as exc:  # noqa: BLE001 - never raise from importer scan
        logger.debug("context_pack: importer scan aborted: %s", exc)

    return sorted(importers)


def build_context_pack(
    repo_dir: str,
    files: list[str],
    *,
    max_chars: int = 6000,
) -> str:
    """Skeleton markdown for `files` + one-hop importers. Never raises;
    returns "" when nothing usable is found."""
    try:
        result_parts = []

        # 1. Process declared files
        for file in files:
            full_path = os.path.join(repo_dir, file)
            try:
                with open(full_path, encoding="utf-8") as f:
                    content = f.read()
            except Exception as exc:  # noqa: BLE001 - skip unreadable declared file
                logger.debug("context_pack: skipping %s: %s", full_path, exc)
                continue

            if file.endswith(".py"):
                skeleton = get_python_skeleton(content)
                if not skeleton:
                    skeleton = get_non_python_skeleton(content)
            else:
                skeleton = get_non_python_skeleton(content)

            if skeleton:
                ext = Path(file).suffix.lstrip(".")
                lang = ext if ext else "text"
                part = f"File: {file}\n```{lang}\n{skeleton}\n```\n"
                result_parts.append(part)

        # 2. Process one-hop importers
        importers = get_one_hop_importers(repo_dir, files)
        for file in importers:
            full_path = os.path.join(repo_dir, file)
            try:
                with open(full_path, encoding="utf-8") as f:
                    content = f.read()
            except Exception as exc:  # noqa: BLE001 - skip unreadable importer file
                logger.debug("context_pack: skipping %s: %s", full_path, exc)
                continue

            skeleton = get_python_skeleton(content)
            if not skeleton:
                continue

            part = f"File: {file}\n```python\n{skeleton}\n```\n"
            result_parts.append(part)

        full_md = "\n".join(result_parts)
        if not full_md.strip():
            return ""

        return truncate_markdown(full_md, max_chars)
    except Exception:
        return ""
