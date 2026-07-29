#!/usr/bin/env python3
"""Build Markdown, standalone HTML, and Chinese PDF ISA references."""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from validate_isa import (
    DEFAULT_ISA,
    DEFAULT_SCHEMA,
    DEFAULT_VECTORS,
    enumerate_vgpr_tag_effects,
    form_key,
    format_report,
    get_families,
    get_forms,
    get_word_bits,
    load_isa,
    load_json,
    parse_integer,
    validate_all,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
DIST_DIR = REPO_ROOT / "dist"
OUTPUT_BASENAME = "VTX-ISA-Reference-1.0-Draft"
MERGED_MARKDOWN = DIST_DIR / f"{OUTPUT_BASENAME}.md"
HTML_REFERENCE = DIST_DIR / f"{OUTPUT_BASENAME}.html"
PDF_REFERENCE = DIST_DIR / f"{OUTPUT_BASENAME}.pdf"
STALE_ARTIFACT_NAMES = (
    "VTX-1-ISA-Reference-1.0-Draft.md",
    "VTX-1-ISA-Reference-1.0-Draft.html",
    "VTX-1-ISA-Reference-1.0-Draft.pdf",
    "VTX-1-ISA-Reference-2.0.md",
    "VTX-1-ISA-Reference-2.0.html",
    "VTX-1-ISA-Reference-2.0.pdf",
    "generated-instruction-reference.md",
)
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
)
PDF_TEXT_REPLACEMENTS = {
    "⊆": " 子集或等于 ",
    "⊂": " 真子集于 ",
    "⋃": " 并集 ",
    "↔": " <-> ",
    "∩": " 交集 ",
    "∈": " 属于 ",
    "∉": " 不属于 ",
}


def _first(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return f"`{yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip()}`"
    return str(value)


def _field_rows(form: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    fields = form.get("fields", {})
    rows: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(fields, Mapping):
        for name, data in fields.items():
            rows.append((str(name), data if isinstance(data, Mapping) else {"bits": data}))
    elif isinstance(fields, list):
        for index, data in enumerate(fields):
            if not isinstance(data, Mapping):
                rows.append((f"field-{index + 1}", {"bits": data}))
                continue
            name = _first(data, ("name", "id", "field"))
            rows.append((str(name or f"field-{index + 1}"), data))
    return rows


def _bits_text(field_data: Mapping[str, Any]) -> str:
    if "bits" in field_data:
        value = field_data["bits"]
        if isinstance(value, (list, tuple)):
            return ":".join(str(item) for item in value)
        return str(value)
    if "bit" in field_data:
        return str(field_data["bit"])
    lsb = _first(field_data, ("lsb", "lo", "low"))
    msb = _first(field_data, ("msb", "hi", "high"))
    width = parse_integer(field_data.get("width"))
    if lsb is not None and msb is not None:
        return f"{msb}:{lsb}"
    if lsb is not None and width is not None:
        return f"{parse_integer(lsb) + width - 1}:{lsb}"
    return "—"


def _table_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _vector_words(vectors: Any) -> dict[str, str]:
    if not isinstance(vectors, Mapping) or not isinstance(vectors.get("forms"), list):
        return {}
    return {
        str(item["key"]): str(item["machine_word"])
        for item in vectors["forms"]
        if isinstance(item, Mapping) and "key" in item and "machine_word" in item
    }


def render_cover(document: Mapping[str, Any]) -> str:
    families = get_families(document) or []
    forms = [
        form
        for family in families
        if isinstance(family, Mapping)
        for form in (get_forms(family) or [])
        if isinstance(form, Mapping)
    ]
    return "\n".join(
        [
            f"# {document.get('title', 'VTX-1 ISA 1.0 Draft')}",
            "",
            f"**状态：{document.get('status', 'Draft')}**",
            "",
            "## SGPR + VGPR 架构",
            "",
            "本规范同时定义每 warp 归属的标量寄存器（SGPR）与逐 lane 切片的"
            "向量寄存器（VGPR）。",
            "",
            f"- 指令家族：{len(families)}",
            f"- 指令形式：{len(forms)}",
            f"- 指令字宽：{get_word_bits(document)} 位",
            "",
        ]
    )


def render_instruction_reference(
    document: Mapping[str, Any],
    vectors: Any | None = None,
) -> str:
    families = get_families(document) or []
    form_count = sum(
        len(get_forms(family) or [])
        for family in families
        if isinstance(family, Mapping) and isinstance(get_forms(family), list)
    )
    lines = [
        "# 附录A 指令形式参考（自动生成）",
        "",
        "> 本章由 `isa/vtx1/isa.yaml` 自动生成，请勿手工编辑。",
        "",
        f"- ISA：{document.get('title', 'VTX-1 ISA')}",
        f"- 版本：{document.get('version', '1.0-draft')}",
        f"- 指令字宽：{get_word_bits(document)} 位",
        f"- Family 数：{len(families)}",
        f"- Form 数：{form_count}",
        f"- Descriptor contract：{_display(document.get('descriptor_contract'))}",
        f"- Barrier contract：{_display(document.get('barrier_contract'))}",
        "",
    ]
    vector_words = _vector_words(vectors)
    tag_effects = enumerate_vgpr_tag_effects(document)
    for family_index, family in enumerate(families):
        if not isinstance(family, Mapping):
            continue
        family_name = str(family.get("mnemonic") or f"Family {family_index + 1}")
        family_id = str(family.get("id", "—"))
        semantic_group = str(family.get("semantic_group", "—"))
        lines.extend(
            [
                f"## {family_name}",
                "",
                f"- Family ID：`{family_id}`",
                f"- 语义组：`{semantic_group}`",
            ]
        )
        description = family.get("summary")
        if description:
            lines.extend(["", str(description).strip()])
        lines.append("")

        forms = get_forms(family) or []
        for form_index, form in enumerate(forms):
            if not isinstance(form, Mapping):
                continue
            form_name = str(form.get("mnemonic") or f"Form {form_index + 1}")
            form_id = str(form.get("id", "—"))
            triple = f"({form.get('class')}, {form.get('format')}, {form.get('opcode')})"
            lines.extend(
                [
                    f"### {form_name} — `{form_id}`",
                    "",
                    f"- 执行域：`{form.get('execution_domain', '—')}`",
                    f"- 编码格式：`{form.get('encoding_format', '—')}`",
                    f"- 语义组：`{semantic_group}`",
                    f"- `(class, format, opcode)`：`{triple}`",
                    f"- Guard policy：`{form.get('guard_policy', '—')}`",
                    f"- Required state：`{form.get('required_state', '—')}`",
                    f"- VGPR tag effect：`{tag_effects.get(form_key(family, form), 'none')}`",
                    "",
                ]
            )
            syntax = form.get("syntax")
            if syntax:
                lines.extend([f"**语法：** `{syntax}`", ""])

            legal_orders = form.get("legal_orders")
            legal_scopes = form.get("legal_scopes")
            if legal_orders is not None or legal_scopes is not None:
                lines.extend(
                    [
                        "#### Atomic modifiers",
                        "",
                        f"- Legal orders：{_display(legal_orders)}",
                        f"- Legal scopes：{_display(legal_scopes)}",
                        "",
                    ]
                )

            address_template = form.get("address_template")
            if isinstance(address_template, Mapping):
                lines.extend(
                    [
                        "#### Address template",
                        "",
                        f"- 地址空间：`{address_template.get('space', '—')}`",
                        f"- 地址模式：`{address_template.get('mode', '—')}`",
                        f"- 表达式：`{address_template.get('expression', '—')}`",
                        f"- 地址操作数：{_display(address_template.get('address_operands'))}",
                        f"- 偏移单位：`{address_template.get('offset_unit', '—')}`",
                        f"- 缩放：`{address_template.get('scale', '—')}`",
                        "",
                    ]
                )

            matrix_contract = form.get("matrix_contract")
            if isinstance(matrix_contract, Mapping):
                lines.extend(
                    [
                        "#### Matrix contract",
                        "",
                        "| 合同项 | 值 |",
                        "|---|---|",
                    ]
                )
                for name, value in matrix_contract.items():
                    lines.append(
                        "| "
                        + " | ".join(
                            (
                                _table_cell(name),
                                _table_cell(_display(value)),
                            )
                        )
                        + " |"
                    )
                lines.append("")

            operands = form.get("operands", [])
            lines.extend(
                [
                    "#### Operands",
                    "",
                    "| 名称 | 类型 | 访问 | 字段 | 说明 |",
                    "|---|---|---|---|---|",
                ]
            )
            if operands:
                for operand in operands:
                    if not isinstance(operand, Mapping):
                        continue
                    cells = (
                        operand.get("name", "—"),
                        operand.get("type", "—"),
                        operand.get("access", "—"),
                        operand.get("field", "—"),
                        operand.get("description", "—"),
                    )
                    lines.append("| " + " | ".join(_table_cell(cell) for cell in cells) + " |")
            lines.append("")

            example = form.get("example")
            example_word = str(example.get("machine_word")) if isinstance(example, Mapping) else "—"
            machine_word = vector_words.get(form_key(family, form), example_word)
            assembly = str(example.get("assembly")) if isinstance(example, Mapping) else "—"
            example_field_values = (
                example.get("field_values") if isinstance(example, Mapping) else None
            )
            lines.extend(
                [
                    "**Semantics：**",
                    "",
                    str(form.get("semantics", "")).strip(),
                    "",
                    "**Constraints：**",
                    "",
                ]
            )
            lines.extend(f"- {item}" for item in form.get("constraints", []))
            lines.extend(["", "**Faults：**", ""])
            lines.extend(f"- {item}" for item in form.get("faults", []))
            lines.extend(
                [
                    "",
                    f"**示例：** `{assembly}`",
                    "",
                    f"**示例字段值：** {_display(example_field_values)}",
                    "",
                    f"**64 位机器字：** `{machine_word}`",
                    "",
                    "#### 编码字段",
                    "",
                    "| 字段 | 位段 | 固定值 | 必须为零 | 保留值 | 说明 |",
                    "|---|---:|---:|:---:|---|---|",
                ]
            )
            for field_name, field_data in _field_rows(form):
                fixed = field_data.get("fixed")
                cells = (
                    field_name,
                    _bits_text(field_data),
                    _display(fixed),
                    "是" if field_data.get("must_zero") else "否",
                    _display(field_data.get("reserved_values")),
                    _display(field_data.get("description")),
                )
                lines.append("| " + " | ".join(_table_cell(cell) for cell in cells) + " |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def merge_markdown(chapters: Sequence[Path], generated: str, cover: str = "") -> str:
    sections: list[str] = [cover.strip()] if cover.strip() else []
    for chapter in chapters:
        text = chapter.read_text(encoding="utf-8-sig").strip()
        if text:
            sections.append(text)
    sections.append(generated.strip())
    return "\n\n<div class=\"page-break\"></div>\n\n".join(sections) + "\n"


def clean_stale_artifacts() -> list[Path]:
    """Delete only obsolete, exactly named distribution files."""
    removed: list[Path] = []
    for name in STALE_ARTIFACT_NAMES:
        artifact = DIST_DIR / name
        if artifact.is_file() or artifact.is_symlink():
            artifact.unlink()
            removed.append(artifact)
    return removed


def _slugify(text: str, used: set[str]) -> str:
    base = re.sub(r"[^\w\u3400-\u9fff-]+", "-", text.lower()).strip("-") or "section"
    slug = base
    suffix = 2
    while slug in used:
        slug = f"{base}-{suffix}"
        suffix += 1
    used.add(slug)
    return slug


def _inline_markup(text: str) -> str:
    escaped = html.escape(text, quote=False)
    placeholders: list[str] = []

    def save_code(match: re.Match[str]) -> str:
        placeholders.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(placeholders) - 1}\x00"

    escaped = re.sub(r"`([^`\n]+)`", save_code, escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )
    for index, replacement in enumerate(placeholders):
        escaped = escaped.replace(f"\x00{index}\x00", replacement)
    return escaped


def split_markdown_table_row(line: str) -> list[str]:
    """Split a Markdown table row without splitting escaped or code-span pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    escaped = False
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "`":
            in_code = not in_code
            current.append(character)
        elif character == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    cells = split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _fence_marker(line: str) -> tuple[str, int, str] | None:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
    if not match:
        return None
    marker = match.group(1)
    return marker[0], len(marker), match.group(2)


def _markdown_headings(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    """Return source index, level, and text for headings outside fenced code."""
    headings: list[tuple[int, int, str]] = []
    fence_character: str | None = None
    fence_length = 0
    for index, line in enumerate(lines):
        marker = _fence_marker(line)
        if marker:
            character, length, suffix = marker
            if fence_character is None:
                fence_character, fence_length = character, length
            elif (
                character == fence_character
                and length >= fence_length
                and not suffix.strip()
            ):
                fence_character = None
                fence_length = 0
            continue
        if fence_character is None:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match:
                headings.append((index, len(match.group(1)), match.group(2)))
    return headings


def markdown_to_html(markdown: str, title: str) -> str:
    source_lines = markdown.splitlines()
    used_slugs: set[str] = set()
    heading_slugs: dict[int, str] = {}
    toc: list[tuple[int, str, str]] = []
    for index, level, text in _markdown_headings(source_lines):
        heading_text = re.sub(r"[`*_]", "", text).strip()
        slug = _slugify(heading_text, used_slugs)
        heading_slugs[index] = slug
        toc.append((level, heading_text, slug))

    body: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None
    code_fence_character: str | None = None
    code_fence_length = 0
    code_lines: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{_inline_markup(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            body.append(f"</{list_tag}>")
            list_tag = None

    while index < len(source_lines):
        line = source_lines[index]
        marker = _fence_marker(line)
        if marker and code_fence_character is None:
            flush_paragraph()
            close_list()
            code_fence_character, code_fence_length, _ = marker
            index += 1
            continue
        if code_fence_character is not None:
            if (
                marker
                and marker[0] == code_fence_character
                and marker[1] >= code_fence_length
                and not marker[2].strip()
            ):
                body.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines.clear()
                code_fence_character = None
                code_fence_length = 0
            else:
                code_lines.append(line)
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            body.append(
                f'<h{level} id="{heading_slugs[index]}">{_inline_markup(heading.group(2))}</h{level}>'
            )
            index += 1
            continue
        if line.strip() == '<div class="page-break"></div>':
            flush_paragraph()
            close_list()
            body.append(line.strip())
            index += 1
            continue
        if line.startswith("> "):
            flush_paragraph()
            close_list()
            body.append(f"<blockquote>{_inline_markup(line[2:])}</blockquote>")
            index += 1
            continue
        if re.match(r"^\s*[-*]\s+", line):
            flush_paragraph()
            if list_tag != "ul":
                close_list()
                body.append("<ul>")
                list_tag = "ul"
            item_text = re.sub(r"^\s*[-*]\s+", "", line)
            body.append(f"<li>{_inline_markup(item_text)}</li>")
            index += 1
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            flush_paragraph()
            if list_tag != "ol":
                close_list()
                body.append("<ol>")
                list_tag = "ol"
            item_text = re.sub(r"^\s*\d+\.\s+", "", line)
            body.append(f"<li>{_inline_markup(item_text)}</li>")
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(source_lines):
            separator = source_lines[index + 1]
            if _is_table_separator(separator):
                flush_paragraph()
                close_list()
                headers = split_markdown_table_row(line)
                body.append("<div class=\"table-wrap\"><table><thead><tr>")
                body.extend(f"<th>{_inline_markup(cell)}</th>" for cell in headers)
                body.append("</tr></thead><tbody>")
                index += 2
                while index < len(source_lines) and source_lines[index].startswith("|"):
                    cells = split_markdown_table_row(source_lines[index])
                    body.append("<tr>")
                    body.extend(f"<td>{_inline_markup(cell)}</td>" for cell in cells)
                    body.append("</tr>")
                    index += 1
                body.append("</tbody></table></div>")
                continue
        if not line.strip():
            flush_paragraph()
            close_list()
        elif re.fullmatch(r"\s*(?:---+|\*\*\*+)\s*", line):
            flush_paragraph()
            close_list()
            body.append("<hr>")
        else:
            close_list()
            paragraph.append(line.strip())
        index += 1

    if code_fence_character is not None:
        body.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    flush_paragraph()
    close_list()
    toc_html = "\n".join(
        f'<li class="toc-level-{level}"><a href="#{slug}">{html.escape(text)}</a></li>'
        for level, text, slug in toc
        if level <= 3
    )
    css = """
    :root { color-scheme: light; font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }
    body { margin: 0; color: #20242a; background: #f5f7fa; line-height: 1.65; }
    .layout { display: grid; grid-template-columns: minmax(220px, 300px) minmax(0, 900px);
              gap: 32px; max-width: 1260px; margin: 0 auto; padding: 28px; }
    nav { position: sticky; top: 20px; align-self: start; max-height: calc(100vh - 40px);
          overflow: auto; padding: 20px; background: white; border: 1px solid #dfe4ea;
          border-radius: 8px; }
    nav h2 { margin-top: 0; } nav ul { list-style: none; padding: 0; }
    nav li { margin: 5px 0; } nav .toc-level-2 { padding-left: 12px; }
    nav .toc-level-3 { padding-left: 24px; font-size: .92em; }
    nav a { color: #245c9c; text-decoration: none; }
    main { padding: 36px 48px; background: white; border: 1px solid #dfe4ea; border-radius: 8px; }
    h1, h2, h3 { line-height: 1.3; scroll-margin-top: 20px; }
    h2 { margin-top: 2em; border-bottom: 1px solid #dfe4ea; padding-bottom: .3em; }
    code { font-family: Consolas, "Microsoft YaHei", monospace; background: #eef2f6; padding: .1em .35em;
           border-radius: 3px; }
    pre { overflow: auto; padding: 16px; background: #17202a; color: #f2f4f7; border-radius: 6px; }
    pre code { padding: 0; color: inherit; background: transparent; }
    blockquote { margin-left: 0; padding: 10px 16px; border-left: 4px solid #4b7bec; background: #f4f7ff; }
    .table-wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; margin: 1em 0; }
    th, td { border: 1px solid #ccd3dc; padding: 7px 10px; text-align: left; }
    th { background: #eef2f6; } .page-break { margin: 3em 0; border-top: 2px solid #ccd3dc; }
    @media (max-width: 800px) { .layout { display: block; padding: 10px; } nav { position: static; margin-bottom: 12px; }
      main { padding: 24px 18px; } }
    @media print { body { background: white; } .layout { display: block; max-width: none; padding: 0; }
      nav { display: none; } main { border: 0; padding: 0; } .page-break { break-before: page; border: 0; } }
    """
    return (
        "<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>"
        '<div class="layout"><nav aria-label="目录"><h2>目录</h2><ul>'
        f"{toc_html}</ul></nav><main>{''.join(body)}</main></div></body></html>\n"
    )


def _register_chinese_font() -> tuple[str, Path]:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError as exc:
        raise RuntimeError("缺少 ReportLab；请运行 pip install -r requirements.txt") from exc

    failures: list[str] = []
    for candidate in FONT_CANDIDATES:
        if not candidate.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont("VTXChinese", str(candidate), subfontIndex=0))
            return "VTXChinese", candidate
        except Exception as exc:  # ReportLab exposes several font parser exception types.
            failures.append(f"{candidate}: {exc}")
    searched = ", ".join(str(path) for path in FONT_CANDIDATES)
    detail = f"；加载失败：{' | '.join(failures)}" if failures else ""
    raise RuntimeError(f"找不到可用的中文字体。已检查：{searched}{detail}")


def _normalize_pdf_text(text: str) -> str:
    for source, replacement in PDF_TEXT_REPLACEMENTS.items():
        text = text.replace(source, replacement)
    return text


def _check_font_glyphs(text: str, font_name: str, font_path: Path) -> None:
    from reportlab.pdfbase import pdfmetrics

    font = pdfmetrics.getFont(font_name)
    widths = getattr(getattr(font, "face", None), "charWidths", {})
    missing = sorted(
        {
            character
            for character in text
            if not character.isspace()
            and ord(character) >= 128
            and ord(character) not in widths
        },
        key=ord,
    )
    if missing:
        preview = " ".join(f"{character}(U+{ord(character):04X})" for character in missing[:20])
        raise RuntimeError(f"中文字体 {font_path} 缺少所需字形：{preview}")


def markdown_to_pdf(
    markdown: str,
    destination: Path,
    title: str,
    cover_summary: str = "",
) -> Path:
    font_name, font_path = _register_chinese_font()
    markdown = _normalize_pdf_text(markdown)
    _check_font_glyphs(markdown, font_name, font_path)
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase.pdfdoc import PDFOutlines, count as count_outlines
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
            XPreformatted,
        )
        from reportlab.platypus.tableofcontents import TableOfContents
    except ImportError as exc:
        raise RuntimeError("缺少 ReportLab；请运行 pip install -r requirements.txt") from exc

    def style(name: str, **kwargs: Any) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=font_name, wordWrap="CJK", **kwargs)

    normal = style("ChineseNormal", fontSize=9.5, leading=15, spaceAfter=6)
    quote = style(
        "ChineseQuote",
        parent=normal,
        leftIndent=12,
        borderColor=colors.HexColor("#4b7bec"),
        borderWidth=1,
        borderPadding=7,
        backColor=colors.HexColor("#f4f7ff"),
    )
    code = style(
        "ChineseCode",
        parent=normal,
        fontSize=7.5,
        leading=10,
        leftIndent=8,
        backColor=colors.HexColor("#eef2f6"),
        borderPadding=6,
    )
    heading_styles = {
        1: style("ChineseH1", fontSize=22, leading=29, spaceBefore=10, spaceAfter=14, alignment=TA_CENTER),
        2: style("ChineseH2", fontSize=16, leading=22, spaceBefore=16, spaceAfter=9, textColor=colors.HexColor("#163a63")),
        3: style("ChineseH3", fontSize=13, leading=18, spaceBefore=12, spaceAfter=7),
        4: style("ChineseH4", fontSize=11, leading=16, spaceBefore=9, spaceAfter=5),
        5: style("ChineseH5", fontSize=10, leading=15, spaceBefore=7, spaceAfter=4),
        6: style("ChineseH6", fontSize=9.5, leading=14, spaceBefore=6, spaceAfter=3),
    }
    bullet = style("ChineseBullet", parent=normal, leftIndent=14, firstLineIndent=-8)
    toc_title = style(
        "ChineseTocTitle",
        fontSize=20,
        leading=28,
        spaceAfter=14,
        alignment=TA_CENTER,
    )
    cover_title = style(
        "ChineseCoverTitle",
        fontSize=30,
        leading=40,
        spaceAfter=18,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#163a63"),
    )
    cover_subtitle = style(
        "ChineseCoverSubtitle",
        fontSize=17,
        leading=25,
        spaceAfter=16,
        alignment=TA_CENTER,
    )
    cover_summary_style = style(
        "ChineseCoverSummary",
        fontSize=11,
        leading=18,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#66717f"),
    )

    def paragraph_text(text: str) -> str:
        escaped = html.escape(text)
        code_spans: list[str] = []

        def save_code_span(match: re.Match[str]) -> str:
            code_spans.append(match.group(1))
            return f"\x00CODE{len(code_spans) - 1}\x00"

        escaped = re.sub(r"`([^`]+)`", save_code_span, escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)
        for span_index, span in enumerate(code_spans):
            escaped = escaped.replace(
                f"\x00CODE{span_index}\x00",
                f'<font name="{font_name}">{span}</font>',
            )
        return escaped

    story: list[Any] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            story.append(XPreformatted(html.escape("\n".join(code_lines)), code))
        elif line.strip() == '<div class="page-break"></div>':
            story.append(PageBreak())
        elif (heading := re.match(r"^(#{1,6})\s+(.+)$", line)):
            level = len(heading.group(1))
            story.append(Paragraph(paragraph_text(heading.group(2)), heading_styles[level]))
        elif line.startswith("> "):
            story.append(Paragraph(paragraph_text(line[2:]), quote))
        elif re.match(r"^\s*[-*]\s+", line):
            text = re.sub(r"^\s*[-*]\s+", "", line)
            story.append(Paragraph("• " + paragraph_text(text), bullet))
        elif re.match(r"^\s*\d+\.\s+", line):
            story.append(Paragraph(paragraph_text(line.strip()), bullet))
        elif line.startswith("|") and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            header = [
                Paragraph(paragraph_text(cell), normal)
                for cell in split_markdown_table_row(line)
            ]
            rows: list[list[Any]] = [header]
            index += 2
            while index < len(lines) and lines[index].startswith("|"):
                cells = [
                    Paragraph(paragraph_text(cell), normal)
                    for cell in split_markdown_table_row(lines[index])
                ]
                rows.append(cells)
                index += 1
            column_count = max(len(row) for row in rows)
            for row in rows:
                row.extend([""] * (column_count - len(row)))
            table = Table(rows, repeatRows=1, colWidths=[(170 * mm) / column_count] * column_count)
            table.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
                        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#aab4c0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.extend([table, Spacer(1, 6)])
            continue
        elif line.strip() and not re.fullmatch(r"\s*(?:---+|\*\*\*+)\s*", line):
            paragraph_lines = [line.strip()]
            while index + 1 < len(lines) and lines[index + 1].strip():
                next_line = lines[index + 1]
                if (
                    next_line.startswith(("#", "```", "|", "> "))
                    or re.match(r"^\s*(?:[-*]|\d+\.)\s+", next_line)
                    or next_line.strip() == '<div class="page-break"></div>'
                ):
                    break
                index += 1
                paragraph_lines.append(next_line.strip())
            story.append(Paragraph(paragraph_text(" ".join(paragraph_lines)), normal))
        index += 1

    def decorate_page(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#66717f"))
        canvas.drawString(20 * mm, 12 * mm, title)
        canvas.drawRightString(190 * mm, 12 * mm, str(document.page))
        canvas.restoreState()

    class OutlineDocTemplate(SimpleDocTemplate):
        def beforeDocument(self) -> None:
            self._heading_index = 0
            self._last_outline_level = -1

        def afterFlowable(self, flowable: Any) -> None:
            if not isinstance(flowable, Paragraph):
                return
            match = re.fullmatch(r"ChineseH([1-6])", flowable.style.name)
            if not match:
                return
            requested_level = int(match.group(1)) - 1
            if requested_level > 1:
                return
            outline_level = min(requested_level, self._last_outline_level + 1)
            text = flowable.getPlainText()
            key = f"heading-{self._heading_index}"
            self._heading_index += 1
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=outline_level, closed=False)
            self._last_outline_level = outline_level
            self.notify("TOCEntry", (requested_level, text, self.page, key))

    class AccuratePDFOutlines(PDFOutlines):
        def prepare(self, document: Any, canvas: Any) -> None:
            super().prepare(document, canvas)
            if self.mydestinations is not None:
                # ReportLab reuses ``count`` as an object-numbering counter in
                # maketree(), inflating the root /Count once per nested list.
                # Recompute it from the finished destination tree instead.
                self.count = count_outlines(self.mydestinations, self.closedict)

    class OutlineCanvas(Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            outlines = AccuratePDFOutlines()
            self._doc.outline = self._doc.Outlines = outlines
            self._doc.Catalog.Outlines = outlines

    toc = TableOfContents()
    toc.levelStyles = [
        style(
            f"ChineseTocLevel{level}",
            fontSize=max(7.5, 11 - level * 0.7),
            leading=15,
            leftIndent=level * 12,
            firstLineIndent=0,
            spaceBefore=2,
        )
        for level in range(2)
    ]
    cover = [
        Spacer(1, 55 * mm),
        Paragraph(paragraph_text(title), cover_title),
        Paragraph("参考手册", cover_subtitle),
    ]
    if cover_summary:
        cover.append(Paragraph(paragraph_text(cover_summary), cover_summary_style))
    cover.extend([PageBreak(), Paragraph("目录", toc_title), toc, PageBreak()])
    story = cover + story

    pdf = OutlineDocTemplate(
        str(destination),
        pagesize=A4,
        title=title,
        author="vtx-isa",
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
    )
    pdf.multiBuild(
        story,
        onFirstPage=decorate_page,
        onLaterPages=decorate_page,
        canvasmaker=OutlineCanvas,
    )
    return font_path


def build(isa_path: Path = DEFAULT_ISA) -> list[Path]:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    clean_stale_artifacts()
    isa_path = isa_path.expanduser().resolve()
    if not isa_path.is_file():
        raise RuntimeError(f"ISA YAML 不存在：{isa_path}")
    try:
        document = load_isa(isa_path)
        schema = load_json(DEFAULT_SCHEMA)
        vectors = load_json(DEFAULT_VECTORS)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError(f"无法加载构建输入：{exc}") from exc
    report = validate_all(document, schema, vectors)
    print(format_report(report, isa_path))
    if not report.ok:
        raise RuntimeError("ISA YAML 验证失败，未生成文档")
    if not isinstance(document, Mapping):
        raise RuntimeError("ISA YAML 根节点必须是 mapping")

    chapters = sorted(DOCS_DIR.glob("*.md"), key=lambda path: path.name.casefold())
    if not chapters:
        raise RuntimeError(f"未找到 Markdown 章节：{DOCS_DIR / '*.md'}")

    generated = render_instruction_reference(document, vectors)
    merged = merge_markdown(chapters, generated, render_cover(document))
    base_title = str(_first(document, ("title", "name", "isa")) or "VTX-1 ISA")
    version = str(_first(document, ("version", "revision")) or "1.0-draft")
    title = base_title if version in base_title else f"{base_title} {version}"
    standalone_html = markdown_to_html(merged, title)

    MERGED_MARKDOWN.write_text(merged, encoding="utf-8", newline="\n")
    HTML_REFERENCE.write_text(standalone_html, encoding="utf-8", newline="\n")
    font_path = markdown_to_pdf(
        merged,
        PDF_REFERENCE,
        title,
        f"SGPR + VGPR · 统一 64 位编码 · {report.family_count} 指令家族 · "
        f"{report.form_count} 指令形式",
    )
    print(f"PDF 中文字体：{font_path}")
    return [MERGED_MARKDOWN, HTML_REFERENCE, PDF_REFERENCE]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--isa",
        type=Path,
        default=DEFAULT_ISA,
        help=f"ISA YAML path (default: {DEFAULT_ISA})",
    )
    args = parser.parse_args(argv)
    try:
        outputs = build(args.isa)
    except RuntimeError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    print("BUILD OK")
    for output in outputs:
        print(f"  {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
