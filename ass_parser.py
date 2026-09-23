#!/usr/bin/env python3
"""
ass_parser.py - Native ASS (Advanced SubStation Alpha) parser and formatting engine.

Features:
- Preserves byte-accurate Script Info, V4+ Styles, and Aegisub metadata.
- Intelligent line classification: isolates spoken dialogues & readable signs while
  passing vector drawings (\\p1..\\p9), karaoke timing (\\k), and generated effects (Effect=fx)
  through untouched.
- In-house prefix tag separation (e.g. {\\pos\\an8...}) with zero loss of coordinates or colors.
- Inline HTML-tag mapping (<i>, <b>) for LLM friendliness without proportional word-splitting bugs.
- Multi-layer sign deduplication to keep glowing/shadowed signs synchronized.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator, Optional, Sequence


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ASSStyle:
    """Represents a style definition in [V4+ Styles]."""
    name: str
    raw_line: str


@dataclass
class ASSEvent:
    """Represents a Dialogue or Comment event in [Events]."""
    is_comment: bool
    layer: int
    start: str
    end: str
    style: str
    name: str
    margin_l: str
    margin_r: str
    margin_v: str
    effect: str
    text: str
    raw_line: str
    index: int  # 0-based position in doc.events

    @property
    def start_millis(self) -> int:
        return parse_ass_timestamp(self.start)

    @property
    def end_millis(self) -> int:
        return parse_ass_timestamp(self.end)


@dataclass
class ASSDocument:
    """Represents an entire parsed ASS subtitle file."""
    sections_order: list[str] = field(default_factory=list)
    script_info: list[str] = field(default_factory=list)
    styles_header: str = "[V4+ Styles]"
    styles_format: str = "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    styles: list[ASSStyle] = field(default_factory=list)
    events_header: str = "[Events]"
    events_format: str = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    events: list[ASSEvent] = field(default_factory=list)
    other_sections: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regex Patterns
# ---------------------------------------------------------------------------

# Leading tag block: one or more contiguous {...} blocks at the start of text
PREFIX_TAG_PATTERN = re.compile(r"^(?:\{[^}]*\})+")

# Vector drawing command (\p1..\p9)
VECTOR_DRAWING_PATTERN = re.compile(r"\\p[1-9]")

# Karaoke timing tags (\k, \kf, \ko, \K)
KARAOKE_TAG_PATTERN = re.compile(r"\\k[fo]?\d+", re.IGNORECASE)

# Spatial/geometric transform tags that cannot be preserved if word length changes (rotation, shear, positioning, clipping, animation)
SPATIAL_TRANSFORM_TAGS = r"\\(?:fr[xyz]|fa[xy]|pos|move|[i]?clip|org|t\b|fsc[xy])"
INTRA_WORD_SPATIAL_PATTERN = re.compile(rf"\w\{{[^}}]*{SPATIAL_TRANSFORM_TAGS}[^}}]*\}}\w", re.IGNORECASE)

# Programming code syntax and source filename patterns to preserve without translation
CODE_PATTERNS = [
    re.compile(r"#include\s*[<\"][\w./]+", re.IGNORECASE),
    re.compile(r"\busing\s+namespace\s+\w+", re.IGNORECASE),
    re.compile(r"\b(int|void|float|double|bool)\s+main\s*\(", re.IGNORECASE),
    re.compile(r"\b(cout|cin)\s*(?:<<|>>)", re.IGNORECASE),
    re.compile(r"\b(printf|scanf|println|console\.log)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:mysql|sqlite3|pthread)_[a-z0-9_]+\s*\(", re.IGNORECASE),
    re.compile(r"\b(public|private|protected)\s+(?:static\s+)?(?:class|void|int|string|boolean)\b", re.IGNORECASE),
    re.compile(r"\bdef\s+[a-zA-Z_]\w*\s*\([^)]*\)\s*:", re.IGNORECASE),
    re.compile(r"\bfunction\s*[a-zA-Z_]?\w*\s*\([^)]*\)\s*\{", re.IGNORECASE),
    re.compile(r"\b(import|from)\s+[a-zA-Z_]\w*\s+(?:import\b|as\b)", re.IGNORECASE),
    re.compile(r"^[\w.-]+\.(?:cpp|c|h|hpp|py|js|ts|jsx|tsx|java|rs|go|rb|php|html|css|json|xml|sh|asm)$", re.IGNORECASE),
]


# Styles typically containing Japanese/Romaji song lyrics
UNTRANSLATABLE_STYLE_PATTERNS = [
    re.compile(r"_RO$", re.IGNORECASE),
    re.compile(r"_JP$", re.IGNORECASE),
    re.compile(r"Romaji", re.IGNORECASE),
    re.compile(r"Japanese", re.IGNORECASE),
    re.compile(r"Kanji", re.IGNORECASE),
    re.compile(r"Karaoke", re.IGNORECASE),
]

# Inline style conversions between ASS and HTML
INLINE_ASS_TO_HTML = [
    (re.compile(r"\{\\i1\}", re.IGNORECASE), "<i>"),
    (re.compile(r"\{\\i0\}", re.IGNORECASE), "</i>"),
    (re.compile(r"\{\\b1\}", re.IGNORECASE), "<b>"),
    (re.compile(r"\{\\b0\}", re.IGNORECASE), "</b>"),
    (re.compile(r"\{\\u1\}", re.IGNORECASE), "<u>"),
    (re.compile(r"\{\\u0\}", re.IGNORECASE), "</u>"),
]

INLINE_HTML_TO_ASS = [
    (re.compile(r"<\s*i\s*>", re.IGNORECASE), r"{\\i1}"),
    (re.compile(r"<\s*/\s*i\s*>", re.IGNORECASE), r"{\\i0}"),
    (re.compile(r"<\s*b\s*>", re.IGNORECASE), r"{\\b1}"),
    (re.compile(r"<\s*/\s*b\s*>", re.IGNORECASE), r"{\\b0}"),
    (re.compile(r"<\s*u\s*>", re.IGNORECASE), r"{\\u1}"),
    (re.compile(r"<\s*/\s*u\s*>", re.IGNORECASE), r"{\\u0}"),
]


# ---------------------------------------------------------------------------
# Timestamp Utilities
# ---------------------------------------------------------------------------

def parse_ass_timestamp(timestamp_str: str) -> int:
    """Parse an ASS timestamp 'H:MM:SS.cc' into milliseconds."""
    try:
        parts = timestamp_str.strip().split(":")
        if len(parts) != 3:
            return 0
        h = int(parts[0])
        m = int(parts[1])
        s_parts = parts[2].split(".")
        s = int(s_parts[0])
        cs = int(s_parts[1]) if len(s_parts) > 1 else 0
        return ((h * 3600 + m * 60 + s) * 1000) + (cs * 10)
    except Exception:
        return 0


def format_ass_timestamp(millis: int) -> str:
    """Format milliseconds into an ASS timestamp 'H:MM:SS.cc'."""
    cs = int((millis % 1000) / 10)
    total_seconds = int(millis / 1000)
    s = total_seconds % 60
    total_minutes = int(total_seconds / 60)
    m = total_minutes % 60
    h = int(total_minutes / 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ---------------------------------------------------------------------------
# Parsing Engine
# ---------------------------------------------------------------------------

def parse_dialogue_line(line: str, index: int) -> Optional[ASSEvent]:
    """Parse a single 'Dialogue:' or 'Comment:' line into ASSEvent."""
    prefix = "Dialogue:" if line.startswith("Dialogue:") else "Comment:"
    is_comment = prefix == "Comment:"
    content = line[len(prefix):].strip()

    # Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    parts = content.split(",", 9)
    if len(parts) < 10:
        logging.warning("Malformed ASS event at index %d: %s", index, line[:60])
        return None

    try:
        layer = int(parts[0].strip()) if parts[0].strip() else 0
    except ValueError:
        layer = 0

    return ASSEvent(
        is_comment=is_comment,
        layer=layer,
        start=parts[1].strip(),
        end=parts[2].strip(),
        style=parts[3].strip(),
        name=parts[4].strip(),
        margin_l=parts[5].strip(),
        margin_r=parts[6].strip(),
        margin_v=parts[7].strip(),
        effect=parts[8].strip(),
        text=parts[9],
        raw_line=line,
        index=index,
    )


def parse_ass_stream(stream: IO[str]) -> ASSDocument:
    """Parse an ASS stream into structured ASSDocument."""
    doc = ASSDocument()
    current_section = None
    event_index = 0

    for raw_line in stream:
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()

        # Section Headers
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            if current_section not in doc.sections_order:
                doc.sections_order.append(current_section)
            if current_section.lower() == "script info":
                doc.script_info.append(line)
            elif "styles" in current_section.lower():
                doc.styles_header = line
            elif current_section.lower() == "events":
                doc.events_header = line
            else:
                if current_section not in doc.other_sections:
                    doc.other_sections[current_section] = []
                doc.other_sections[current_section].append(line)
            continue

        if current_section is None:
            continue

        sec_lower = current_section.lower()

        if sec_lower == "script info":
            doc.script_info.append(line)

        elif "styles" in sec_lower:
            if line.startswith("Format:"):
                doc.styles_format = line
            elif line.startswith("Style:"):
                # Style: Name, Fontname, ...
                content = line[6:].strip()
                name = content.split(",", 1)[0].strip() if "," in content else content
                doc.styles.append(ASSStyle(name=name, raw_line=line))

        elif sec_lower == "events":
            if line.startswith("Format:"):
                doc.events_format = line
            elif line.startswith("Dialogue:") or line.startswith("Comment:"):
                event = parse_dialogue_line(line, event_index)
                if event:
                    doc.events.append(event)
                    event_index += 1
            else:
                # Blank lines or comments in Events
                pass

        else:
            if current_section not in doc.other_sections:
                doc.other_sections[current_section] = []
            doc.other_sections[current_section].append(line)

    return doc


def parse_ass_file(file_path: str | Path) -> ASSDocument:
    """Parse an ASS file from path with automatic encoding fallback."""
    path = Path(file_path)
    with path.open("rb") as f:
        raw_bytes = f.read()

    decoded = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1", "gb18030", "shift_jis"):
        try:
            decoded = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        decoded = raw_bytes.decode("utf-8", errors="replace")

    import io
    return parse_ass_stream(io.StringIO(decoded))


# ---------------------------------------------------------------------------
# Line Classification
# ---------------------------------------------------------------------------

def is_vector_drawing(text: str) -> bool:
    """Check if the text contains ASS vector drawings."""
    if VECTOR_DRAWING_PATTERN.search(text):
        return True
    # Common Aegisub raw drawing command: starts with move 'm 123 456'
    stripped = PREFIX_TAG_PATTERN.sub("", text).strip()
    if re.match(r"^m\s+-?\d+\s+-?\d+", stripped):
        return True
    return False


def is_karaoke_or_fx(event: ASSEvent) -> bool:
    """Check if the event is a generated karaoke singing aid or decomposed particle effect."""
    # Syllable timing tags (\k, \kf, \ko, \K)
    if KARAOKE_TAG_PATTERN.search(event.text):
        return True
    # Aegisub Kara-templater generated line
    if event.effect and event.effect.lower().startswith("fx"):
        # Decomposed letter animation or flying particles have very short duration
        duration = event.end_millis - event.start_millis
        if duration < 200:
            return True
        # Decomposed letter animation: single letter / non-word fragment
        plain = extract_plain_text(event.text).strip()
        if len(plain) <= 1:
            return True
        # Multi-word readable song lyric lines generated with fx (e.g. ED English, OP TL) are translatable
        return False
    return False


def is_untranslatable_style(style_name: str) -> bool:
    """Check if the style indicates non-translatable song lyrics (Romaji/Kanji)."""
    for pattern in UNTRANSLATABLE_STYLE_PATTERNS:
        if pattern.search(style_name):
            return True
    return False


def is_intra_word_transform(text: str) -> bool:
    """Check if line is frame-by-frame letter animation with spatial transforms between characters."""
    # Replace linebreaks (\N, \n, \h) with spaces so tags following newlines are not considered intra-word
    cleaned = re.sub(r"\\[Nnh]", " ", text)
    return bool(INTRA_WORD_SPATIAL_PATTERN.search(cleaned))


def is_programming_code(text: str) -> bool:
    """Check if text is programming source code or a source code filename."""
    plain = extract_plain_text(text)
    if not plain:
        return False
    return any(p.search(plain) for p in CODE_PATTERNS)


def is_translatable_event(event: ASSEvent) -> bool:
    """
    Determine if an event should be translated.
    
    Skips:
    - Comment lines
    - Pure vector drawings
    - Syllable-timed singing aids and generated song fx (Effect=fx)
    - Untranslatable styles (Romaji, Japanese lyrics)
    - Intra-word frame-by-frame letter animations
    - Programming source code and code filenames
    - Purely blank or tag-only lines
    """
    if event.is_comment:
        return False

    if is_vector_drawing(event.text):
        return False

    if is_karaoke_or_fx(event):
        return False

    if is_untranslatable_style(event.style):
        return False

    if is_intra_word_transform(event.text):
        return False

    if is_programming_code(event.text):
        return False

    # Check if there is actual human-readable text after removing tags
    plain = extract_plain_text(event.text)
    if not plain or not re.search(r"\w", plain):
        return False

    return True


# ---------------------------------------------------------------------------
# Tag & Formatting Extraction / Restoration
# ---------------------------------------------------------------------------

def is_layout_tag_block(block_content: str) -> bool:
    """Check if a {...} block contains layout/override tags rather than pure inline formatting."""
    cleaned = re.sub(r"\\[ibus][01]\b", "", block_content, flags=re.IGNORECASE).strip()
    return bool(cleaned)


def extract_prefix_tags(text: str) -> tuple[str, str]:
    """
    Extract leading layout/styling override tag blocks from the rest of the text.
    
    Leaves pure inline formatting tags (e.g. {\\i1}) attached to the payload.
    
    Examples:
      '{\\an8\\pos(971,395)}Your account has been suspended'
      -> ('{\\an8\\pos(971,395)}', 'Your account has been suspended')
      
      '{\\fscx16\\fscy15\\an8\\fnpuffmod1\\b0\\c&HC2AAA0&\\pos(1576.5,233.5)\\blur0.5}Fire'
      -> ('{\\fscx16\\fscy15\\an8\\fnpuffmod1\\b0\\c&HC2AAA0&\\pos(1576.5,233.5)\\blur0.5}', 'Fire')
      
      '{\\an8}{\\i1}Top italic text{\\i0}'
      -> ('{\\an8}', '{\\i1}Top italic text{\\i0}')
    """
    pos = 0
    prefix_parts = []
    while pos < len(text) and text[pos] == "{":
        end = text.find("}", pos)
        if end == -1:
            break
        block = text[pos + 1 : end]
        if is_layout_tag_block(block):
            prefix_parts.append(text[pos : end + 1])
            pos = end + 1
        else:
            break
    prefix = "".join(prefix_parts)
    payload = text[pos:]
    return prefix, payload


def encode_inline_tags(text: str) -> str:
    """Convert standalone ASS inline formatting tags to HTML tags for the LLM."""
    for pat, tag in INLINE_ASS_TO_HTML:
        text = pat.sub(tag, text)
    return text


def decode_inline_tags(text: str) -> str:
    """
    Convert HTML tags back into ASS formatting tags.
    
    Examples:
      '<i>mejores</i>' -> '{\\i1}mejores{\\i0}'
      '<b>precaución</b>' -> '{\\b1}precaución{\\b0}'
    """
    result = text
    for html_pat, ass_tag in INLINE_HTML_TO_ASS:
        result = html_pat.sub(ass_tag, result)
    return result


def extract_plain_text(text: str) -> str:
    """Strip drawing commands and all ASS tags {...} to get pure readable text."""
    # Strip vector drawing commands between \p1..\p9 and \p0 (or to end of string)
    no_draw = re.sub(r"\\p[1-9].*?(?:\\p0|$)", "", text)
    clean = re.sub(r"\{[^}]*\}", "", no_draw)
    clean = re.sub(r"^m\s+-?\d+\s+-?\d+.*", "", clean.strip())
    clean = clean.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    return re.sub(r"\s+", " ", clean).strip()


def prepare_translatable_payload(event: ASSEvent) -> tuple[str, str]:
    """
    Splits an event into (prefix_tags, translatable_text).
    
    1. Extracts leading layout/styling override tags ({...}) as prefix.
    2. Encodes standalone inline tags ({\\i1} -> <i>) in the remaining text.
    3. Cleans intermediate non-inline ASS tags (e.g. animated {\\ybord11})
       and hard spaces (\\h) for LLM friendliness and multi-frame deduplication.
    """
    prefix, raw_payload = extract_prefix_tags(event.text)
    encoded_payload = encode_inline_tags(raw_payload)
    # Strip any remaining non-inline ASS tags in the middle of words/sentences
    clean_payload = re.sub(r"\{[^}]*\}", "", encoded_payload)
    # Replace hard spaces \h with regular space
    clean_payload = clean_payload.replace(r"\h", " ")
    # Normalize multiple whitespace characters (while keeping \N intact)
    clean_payload = re.sub(r"[ \t\f\v]+", " ", clean_payload).strip()
    return prefix, clean_payload


def extract_color_gradient(raw_payload: str, prefix_tags: str = "") -> list[str]:
    """
    Extract active inline color override tags across each plain text character position.
    
    Returns a list of color tag strings (e.g. '{\\1c&H...&\\3c&H...&}'),
    or an empty list if there is no multi-color gradient across characters.
    """
    prefix_colors = re.findall(r"\\[1234]?c&H[0-9a-fA-F]+&", prefix_tags)
    curr_color = "{" + "".join(prefix_colors) + "}" if prefix_colors else ""

    tokens = re.split(r"(\{[^}]*\})", raw_payload)
    color_sequence = []
    for t in tokens:
        if t.startswith("{") and t.endswith("}"):
            colors = re.findall(r"\\[1234]?c&H[0-9a-fA-F]+&", t)
            if colors:
                curr_color = "{" + "".join(colors) + "}"
        else:
            for c in t:
                color_sequence.append(curr_color)

    distinct_colors = {c for c in color_sequence if c}
    if len(distinct_colors) >= 2:
        return color_sequence
    return []


def apply_color_gradient(translated_payload: str, color_sequence: list[str], prefix_tags: str = "") -> str:
    """
    Proportionally distribute an original color gradient sequence across
    the characters of translated_payload, inserting color override tags
    only at color-change boundaries and preserving HTML tags and \\N.
    """
    if not color_sequence:
        return translated_payload

    token_pattern = re.compile(r"(<[^>]+>|\\[Nnh])")
    parts = token_pattern.split(translated_payload)

    num_visible = sum(len(p) for p in parts if not token_pattern.fullmatch(p))
    if num_visible == 0:
        return translated_payload

    prefix_colors = re.findall(r"\\[1234]?c&H[0-9a-fA-F]+&", prefix_tags)
    curr_color = "{" + "".join(prefix_colors) + "}" if prefix_colors else ""

    result = []
    char_idx = 0
    L_orig = len(color_sequence)

    for p in parts:
        if token_pattern.fullmatch(p):
            result.append(p)
        else:
            for ch in p:
                orig_idx = int(round(char_idx * (L_orig - 1) / max(1, num_visible - 1)))
                col = color_sequence[orig_idx]
                if col and col != curr_color:
                    result.append(col)
                    curr_color = col
                result.append(ch)
                char_idx += 1

    return "".join(result)


def scale_sign_fscx(
    prefix_tags: str,
    orig_plain_len: int,
    trans_plain_len: int,
    style: str,
    name: str = "",
) -> str:
    """
    Horizontally scale (\\fscx) positioned sign events if the translated text
    is significantly longer than the original text, preventing button/tab collisions.
    """
    if orig_plain_len <= 0 or trans_plain_len <= 0:
        return prefix_tags

    # Only scale non-dialogue signs with explicit positioning
    if "\\pos(" not in prefix_tags:
        return prefix_tags

    style_lower = style.lower()
    name_lower = name.lower()
    is_sign = (
        "sign" in style_lower
        or "title" in style_lower
        or "text" in style_lower
        or name_lower in ("sign", "ide", "title", "screen", "ui")
        or (style_lower not in ("default", "dialogue", "main") and "default" not in style_lower)
    )
    if not is_sign:
        return prefix_tags

    ratio = trans_plain_len / orig_plain_len
    if ratio <= 1.35:
        return prefix_tags

    fscx_match = re.search(r"\\fscx(\d+)", prefix_tags)
    orig_fscx = int(fscx_match.group(1)) if fscx_match else 100

    new_fscx = max(55, int(orig_fscx / ratio))
    if new_fscx == orig_fscx:
        return prefix_tags

    if fscx_match:
        return re.sub(r"\\fscx\d+", f"\\\\fscx{new_fscx}", prefix_tags)
    else:
        # Insert \fscx into the first {...} block
        return re.sub(r"^(\{[^}]*)", rf"\1\\fscx{new_fscx}", prefix_tags)


def reconstruct_event_text(
    prefix_tags: str,
    translated_payload: str,
    color_gradient: Optional[list[str]] = None,
    orig_plain_len: int = 0,
    style: str = "",
    name: str = "",
) -> str:
    """Recombine prefix tags with the translated payload, restoring gradients and scaling signs."""
    scaled_prefix = scale_sign_fscx(
        prefix_tags,
        orig_plain_len=orig_plain_len,
        trans_plain_len=len(extract_plain_text(translated_payload)),
        style=style,
        name=name,
    )
    if color_gradient:
        translated_payload = apply_color_gradient(translated_payload, color_gradient, scaled_prefix)
    decoded_payload = decode_inline_tags(translated_payload)
    return f"{scaled_prefix}{decoded_payload}"


# ---------------------------------------------------------------------------
# Translation Unit Batching & Deduplication
# ---------------------------------------------------------------------------

@dataclass
class ASSTranslationUnit:
    """A distinct text payload to be translated, mapping to one or more ASS events."""
    id: int  # 1-based sequential ID for LLM batching
    payload: str  # Cleaned translatable text (HTML inline tags, \N intact)
    speaker: str  # Speaker name if available
    start_millis: int  # Earliest start timestamp for chronological sorting
    event_targets: list[tuple[int, str, list[str], int] | tuple[int, str]] = field(default_factory=list)


def prepare_translation_units(doc: ASSDocument) -> list[ASSTranslationUnit]:
    """
    Extract translatable events from ASSDocument and group identical payloads.
    
    Identical payloads (like multi-layer signs and frame-by-frame motion tracking)
    are deduplicated to prevent token waste and translation flickering across frames.
    
    Units are sorted chronologically by start timestamp to give the LLM natural
    conversational context.
    """
    payload_to_targets: dict[str, list[tuple[int, str, list[str], int]]] = {}
    payload_metadata: dict[str, tuple[str, int]] = {}

    for idx, event in enumerate(doc.events):
        if not is_translatable_event(event):
            continue

        prefix, payload = prepare_translatable_payload(event)
        key = payload.strip()
        if not key:
            continue

        _, raw_payload = extract_prefix_tags(event.text)
        color_gradient = extract_color_gradient(raw_payload, prefix)
        orig_plain_len = len(extract_plain_text(event.text))

        if key not in payload_to_targets:
            payload_to_targets[key] = []
            payload_metadata[key] = (event.name, event.start_millis)
        else:
            # Update earliest start timestamp
            existing_name, existing_start = payload_metadata[key]
            if event.start_millis < existing_start:
                payload_metadata[key] = (existing_name or event.name, event.start_millis)

        payload_to_targets[key].append((idx, prefix, color_gradient, orig_plain_len))

    # Build chronological list of units
    raw_units: list[tuple[str, str, int, list[tuple[int, str, list[str], int]]]] = []
    for key, targets in payload_to_targets.items():
        name, start_millis = payload_metadata[key]
        raw_units.append((key, name, start_millis, targets))

    # Sort chronologically by start time
    raw_units.sort(key=lambda u: u[2])

    units: list[ASSTranslationUnit] = []
    for seq_id, (key, name, start_millis, targets) in enumerate(raw_units, start=1):
        units.append(
            ASSTranslationUnit(
                id=seq_id,
                payload=key,
                speaker=name,
                start_millis=start_millis,
                event_targets=targets,
            )
        )

    return units


def apply_translation_units(
    doc: ASSDocument,
    units: Sequence[ASSTranslationUnit] | dict[int, str],
    translated_map: Optional[dict[int, str]] = None,
) -> ASSDocument:
    """
    Apply translated texts from translated_map ({unit_id: translated_payload})
    back to the corresponding events in ASSDocument.
    
    Accepts either (doc, units, translated_map) or (doc, translated_map).
    When units is provided, it uses the static precomputed unit-to-event mappings,
    preventing unit desynchronization and drift during streaming.
    """
    if translated_map is None and isinstance(units, dict):
        translated_map = units
        unit_list = prepare_translation_units(doc)
    else:
        unit_list = units  # type: ignore

    for unit in unit_list:
        if unit.id not in translated_map:
            continue
        translated_payload = translated_map[unit.id]
        for target in unit.event_targets:
            event_idx = target[0]
            prefix = target[1]
            color_gradient = target[2] if len(target) > 2 else []
            orig_plain_len = target[3] if len(target) > 3 else 0
            event = doc.events[event_idx]
            new_text = reconstruct_event_text(
                prefix,
                translated_payload,
                color_gradient=color_gradient,
                orig_plain_len=orig_plain_len,
                style=event.style,
                name=event.name,
            )
            event.text = new_text
    return doc


# ---------------------------------------------------------------------------
# Formatting and Serialization
# ---------------------------------------------------------------------------

def format_event_line(event: ASSEvent) -> str:
    """Format an ASSEvent back into a valid ASS Dialogue: or Comment: line."""
    prefix = "Comment:" if event.is_comment else "Dialogue:"
    return (
        f"{prefix} {event.layer},{event.start},{event.end},"
        f"{event.style},{event.name},{event.margin_l},{event.margin_r},"
        f"{event.margin_v},{event.effect},{event.text}"
    )


def write_ass_stream(doc: ASSDocument, out: IO[str]):
    """Write an ASSDocument to an open file stream."""
    # 1. Script Info
    if doc.script_info:
        for line in doc.script_info:
            out.write(line + "\n")
        out.write("\n")

    # 2. Other sections preceding Styles (e.g. Aegisub Project Garbage)
    for sec_name, sec_lines in doc.other_sections.items():
        if "events" not in sec_name.lower():
            for line in sec_lines:
                out.write(line + "\n")
            out.write("\n")

    # 3. V4+ Styles
    if doc.styles_header:
        out.write(doc.styles_header + "\n")
    if doc.styles_format:
        out.write(doc.styles_format + "\n")
    for style in doc.styles:
        out.write(style.raw_line + "\n")
    out.write("\n")

    # 4. Events
    if doc.events_header:
        out.write(doc.events_header + "\n")
    if doc.events_format:
        out.write(doc.events_format + "\n")

    for event in doc.events:
        out.write(format_event_line(event) + "\n")


def write_ass_file(doc: ASSDocument, file_path: str | Path):
    """Write an ASSDocument to a file path using UTF-8 encoding."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        write_ass_stream(doc, f)
