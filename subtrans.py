#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai",
# ]
# ///
import re
import os
import json
import time
import locale
import logging
import argparse
from pathlib import Path
from dataclasses import dataclass
from subprocess import Popen, PIPE
from typing import IO, Any, Iterator, Optional, TextIO, TypedDict

from openai import OpenAI, RateLimitError, APIError, APIConnectionError, APITimeoutError

# Some gateways (e.g. AMD) rate-limit aggressively. The OpenAI client retries
# internally with exponential backoff; this is the extra number of
# request-level retries after the client gives up.
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_RETRY_DELAY = 3.0


PROMPT_DEV = """\
You are an expert subtitle translator.
Translate the given subtitle lines into {dest_lang}.

CRITICAL RULES:
1. Each line begins with a numeric ID tag like [123]. Output EXACTLY one translated line for each input line with the matching numeric [ID] (never replace numbers with words).
2. DO NOT merge, omit, reorder, or split lines. Every input [ID] must appear in the output.
3. Even if a sentence spans multiple lines, translate each line piece-by-piece so line boundaries match the audio timing.
   DO NOT complete the sentence early in the first line! Translate ONLY the words that belong to that line.
   Example:
     Input:
     [10] For example,
     [11] we are going to the park.
     Output:
     [10] Por ejemplo,
     [11] vamos al parque.
4. Preserve HTML formatting tags (e.g. <i>...</i>, <b>...</b>, <u>...</u>).
   Apply them to the corresponding translated words or phrases to preserve emphasis and formatting.
   Example:
     Input:
     [20] I'm talking about the <i>other</i> mindless circus freaks.
     [21] It seems like they have names for <i>everything</i> now.
     Output:
     [20] Hablo de los <i>otros</i> fenómenos de circo sin cerebro.
     [21] Parece que ahora le ponen nombre a <i>todo</i>.
5. NEVER insert annotations, brackets, or commentary like [seguirá], [continúa], or [notes] into the dialogue unless it was in the original line.
6. Output ONLY the translated [ID] lines in plain text. No code blocks, no markdown, no conversational filler.

{extra_prompt}\
"""
PROMPT_USER = "{srt_content}"

TAG_PATTERN = re.compile(r"<[^>]+>|\{[^}]*\}")


def sanitize_prompt_text(text: str) -> str:
    """Prepare dialogue for LLM prompt, preserving semantic tags (i, b, u)."""
    # Convert ASS style inline tags to HTML tags
    text = re.sub(r"\{\\i1\}", "<i>", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\\i0\}", "</i>", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\\b1\}", "<b>", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\\b0\}", "</b>", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\\u1\}", "<u>", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\\u0\}", "</u>", text, flags=re.IGNORECASE)
    # Strip remaining ASS tags like {\pos(...)}
    text = re.sub(r"\{[^}]*\}", "", text)
    # Strip font tags
    text = re.sub(r"<font[^>]*>|</font>", "", text, flags=re.IGNORECASE)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned if cleaned else "..."


def clean_hallucinated_tags(text: str) -> str:
    """Strip hallucinated bracket annotations like [seguirá] or [continúa]."""
    cleaned = re.sub(
        r"\[(?:seguirá|seguira|continúa|continua|continuará|continuara|sigue|to be continued|cont(?:inued|\.)?|notes?|sic)\]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def apply_translation_to_text(original_text: str, translated: str) -> str:
    """Clean and apply translated text, preserving formatting tags.

    Tags (like <i>...</i>, <b>...</b>) are passed directly to the LLM
    which accurately places them around the translated words.
    If the LLM returned tags, we preserve its semantic placement.
    If the entire original line was wrapped in tags (e.g. <i>...</i>)
    and the LLM returned plain text, we preserve the line-level wrapping.
    """
    translated = clean_hallucinated_tags(translated)
    if not translated:
        return ""

    # If the LLM already included formatting tags, keep them
    if TAG_PATTERN.search(translated):
        return translated

    # If the entire original line was italicized/bolded/underlined, keep that style
    orig_clean = original_text.strip()
    for tag in ("i", "b", "u"):
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        if orig_clean.lower().startswith(open_tag) and orig_clean.lower().endswith(close_tag):
            return f"<{tag}>{translated}</{tag}>"

    return translated


def wrap_subtitle_text(text: str, max_chars: int = 45) -> list[str]:
    """Wrap translated dialogue into 1 or 2 lines for clean subtitle rendering."""
    if len(text) <= max_chars or " " not in text:
        return [text]
    if "\n" in text:
        return [l.strip() for l in text.splitlines() if l.strip()]
    words = text.split()
    if len(words) <= 3:
        return [text]
    mid = len(text) // 2
    best_split = len(words) // 2
    best_diff = float("inf")
    current_len = 0
    for i, w in enumerate(words[:-1]):
        current_len += len(w) + 1
        diff = abs(current_len - mid)
        if diff < best_diff:
            best_diff = diff
            best_split = i + 1
    line1 = " ".join(words[:best_split])
    line2 = " ".join(words[best_split:])

    # Fix any tags that were split across lines (e.g. <i> opened in line1 but closed in line2)
    for tag in ("i", "b", "u"):
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        open_count1 = line1.lower().count(open_tag)
        close_count1 = line1.lower().count(close_tag)
        if open_count1 > close_count1:
            line1 = line1 + close_tag
            line2 = open_tag + line2

    return [line1, line2]


@dataclass
class PlatformOpts:
    key_regex: str
    model: str
    base_url: Optional[str] = None


PLATFORM_DEFAULTS = {
    "OpenRouter": PlatformOpts(
        key_regex=r"sk-or-v1-[a-zA-Z0-9]+",
        model="deepseek/deepseek-chat",
        base_url="https://openrouter.ai/api/v1",
    ),
    "OpenAI": PlatformOpts(r"sk-\w+T3BlbkFJ\w+", "gpt-4o-mini"),
    "Gemini": PlatformOpts(
        key_regex=r"AIzaSyD[\w\-_]+",
        model="gemini-2.5-pro-exp-03-25",  # not working well
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    "DeepSeek": PlatformOpts(
        key_regex=r"sk-[a-z0-9]{32}",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
    ),
}
PLATFORM_DEFAUTLS = PLATFORM_DEFAULTS


@dataclass(frozen=True)
class SubtitleLine:
    seq: int
    time_line: str
    text_lines: list[str]

    def format_without_time(self) -> str:
        """SRT dialogus without timestamp line"""
        body = "\n".join(self.text_lines)
        return f"{self.seq}\n{body}"

    def format_bracket_line(self) -> str:
        """Format as single line with bracketed ID: [seq] text_with_tags"""
        clean = sanitize_prompt_text(" ".join(self.text_lines))
        return f"[{self.seq}] {clean}"

    def format_full(self) -> str:
        """SRT dialogus with timestamp"""
        body = "\n".join(self.text_lines)
        return f"{self.seq}\n{self.time_line}\n{body}"

    def strip_font_tags(self) -> "SubtitleLine":
        return SubtitleLine(
            seq=self.seq,
            time_line=self.time_line,
            text_lines=[re.sub(r"<font[^>]+>|</font>", "", l) for l in self.text_lines],
        )

    @property
    def timestamp_millis(self) -> tuple[int, int]:
        matches = re.findall(r"((\d\d:){2}\d\d(,\d{1,3}))", self.time_line)
        ts = []
        for match, *_ in matches:
            h, m, s = match.split(":", 3)
            if "," in s:
                s, ms = s.split(",", 2)
            else:
                ms = "0"
            ts.append((((int(h) * 60) + int(m) * 60) + int(s)) * 1000 + int(ms))
        if len(ts) != 2:
            raise ValueError("malformated timestamp (%s)" % self.time_line)
        return ts[0], ts[1]


def parse_subtitle(input: IO[str]) -> Iterator[SubtitleLine]:
    seq = None
    time_line = None
    text_lines = []
    for line in input:
        line = line.strip()
        # parse seq
        if seq is None:
            try:
                seq = int(line)
            except ValueError as err:
                logging.warning("expect seq num, found `%s` (%s)", line, err)
            continue
        # parse time line
        if time_line is None:
            if "-->" in line:
                time_line = line
            else:
                logging.warning("expect time, found `%s`", line)
            continue
        # parse text lines
        if not line:
            # dialogue end
            yield SubtitleLine(seq, time_line, text_lines)
            seq = None
            time_line = None
            text_lines = []
        else:
            text_lines.append(line)


def extract_subtitle_from_video(
    ffmpeg_bin: str, video_url: str, sub_track_id: int
) -> Iterator[SubtitleLine]:
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_url,
        "-map",
        f"0:s:{sub_track_id}",
        "-f",
        "srt",
        "-",
    ]
    logging.info("Execute %s", " ".join(args))
    with Popen(args, stdout=PIPE, encoding="utf-8", errors="replace") as proc:
        assert proc.stdout is not None
        yield from parse_subtitle(proc.stdout)
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg exit with {ret}")


def read_subtitle_from_srt(path: str) -> Iterator[SubtitleLine]:
    import io
    with open(path, "rb") as f:
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
    yield from parse_subtitle(io.StringIO(decoded))


def iter_take[T](iter: Iterator[T], num: int) -> Iterator[T]:
    count = 0
    for item in iter:
        yield item
        count += 1
        if count >= num:
            return


def translate_subtitle(
    openai: OpenAI,
    model: str,
    batch_size: int,
    prompt_vars: dict[str, Any],
    lines: Iterator[SubtitleLine],
    ipc: Optional[TextIO] = None,
    args: Optional["Args"] = None,
) -> Iterator[SubtitleLine]:
    prompt_dev = PROMPT_DEV.format(**prompt_vars)
    batch_count = 0
    batch = list(iter_take(lines, batch_size))
    total_translated = 0
    seq_miss_count: dict[int, int] = {}
    network_retry_count = 0
    MAX_NETWORK_RETRIES = 3
    NETWORK_RETRY_DELAY = 2.0

    while batch:
        translated_count = 0
        handled_in_batch: set[int] = set()
        history_holder = {}
        had_stream_error = False

        items = translate_subtitle_batch(
            openai,
            model,
            prompt_dev,
            prompt_vars,
            batch,
            ipc=ipc,
            reasoning_effort=args.reasoning_effort if args else "none",
            history_holder=history_holder,
        )
        try:
            for item in items:
                handled_in_batch.add(item.seq)
                translated_count += 1
                total_translated += 1
                yield item
        except Exception as err:
            had_stream_error = True
            logging.warning(
                "translate error at batch#%s line#%s: %s",
                batch_count,
                translated_count,
                err,
            )
            raw_history = getattr(err, "_raw_response", None) or (
                history_holder.get("raw")() if history_holder.get("raw") else None
            )
            if args is not None:
                dump_error_log(args, err, batch, raw_history)

            if translated_count == 0:
                network_retry_count += 1
                if network_retry_count <= MAX_NETWORK_RETRIES:
                    delay = NETWORK_RETRY_DELAY * network_retry_count
                    logging.warning(
                        "Batch attempt failed before producing output; retrying in %.1fs (attempt %d/%d)...",
                        delay,
                        network_retry_count,
                        MAX_NETWORK_RETRIES,
                    )
                    if ipc is not None:
                        try:
                            ipc.seek(0)
                            json.dump(
                                dict(
                                    status="network_retry",
                                    retry_in=int(delay),
                                    attempt=network_retry_count,
                                    max_retries=MAX_NETWORK_RETRIES,
                                ),
                                ipc,
                            )
                            ipc.truncate()
                            ipc.flush()
                        except Exception:
                            pass
                    time.sleep(delay)
                    continue
                else:
                    logging.error("translate failure after %d retries", MAX_NETWORK_RETRIES)
                    raise err
            else:
                # Stream made partial progress before dropping; short pause before retrying remaining unhandled lines
                time.sleep(NETWORK_RETRY_DELAY)

        logging.info(
            "translated %s dialogues in batch#%s", translated_count, batch_count
        )
        if translated_count == 0 and not had_stream_error:
            if args is not None and args.reasoning_effort != "none":
                logging.warning(
                    "Model produced 0 content with reasoning_effort='%s'. Retrying batch with reasoning disabled ('none')...",
                    args.reasoning_effort,
                )
                args.reasoning_effort = "none"
                continue
            err = RuntimeError("empty response from model")
            raw_history = history_holder.get("raw")() if history_holder.get("raw") else None
            if args is not None:
                dump_error_log(args, err, batch, raw_history)
            raise err

        # Reset consecutive network retry counter if we successfully translated lines
        if translated_count > 0:
            network_retry_count = 0

        # Process handled and unhandled lines
        unhandled = [l for l in batch if l.seq not in handled_in_batch]
        new_batch = []
        for l in unhandled:
            if had_stream_error:
                # Mid-stream crash (e.g. SSE drop); this was NOT an intentional omission by the model.
                # Don't increment seq_miss_count so it doesn't prematurely drop lines to English.
                new_batch.append(l)
            else:
                misses = seq_miss_count.get(l.seq, 0) + 1
                seq_miss_count[l.seq] = misses
                if misses >= 2:
                    # Retried already and still omitted by LLM; yield original text to avoid stalling
                    logging.warning(
                        "Line seq %s omitted by model twice; passing through original text",
                        l.seq,
                    )
                    yield l
                    total_translated += 1
                else:
                    new_batch.append(l)

        batch = new_batch
        batch.extend(iter_take(lines, batch_size - len(batch)))
        batch_count += 1


OPENROUTER_MODEL_ALIASES = {
    "z-ai/glm-flash-latest": "z-ai/glm-4.7-flash",
    "glm-flash": "z-ai/glm-4.7-flash",
    "glm-flash-latest": "z-ai/glm-4.7-flash",
    "glm-4-flash": "z-ai/glm-4.7-flash",
    "glm-4.7-flash": "z-ai/glm-4.7-flash",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "nemotron": "nvidia/nemotron-3.5-lightning",
    "nemotron-3.5": "nvidia/nemotron-3.5-lightning",
    "nemotron-3.5-lightning": "nvidia/nemotron-3.5-lightning",
    "deepseek-flash": "deepseek/deepseek-chat",
    "deepseek-v4.1-flash": "deepseek/deepseek-chat",
    "deepseek-v4-flash": "deepseek/deepseek-chat",
}


class RespBuf:
    def __init__(self, known_seqs: Optional[set[int]] = None):
        self.known_seqs: set[int] = known_seqs or set()
        self._raw_stream: str = ""
        self._raw_history: list[str] = []
        self._completed: list[tuple[int, list[str]]] = []
        self._current_seq: Optional[int] = None
        self._current_lines: list[str] = []
        self._in_think: bool = False

    def _clean_seq_prefix(self, seq: int, text: str) -> str:
        text = text.strip()
        # Strip redundant repeated sequence number, e.g. "182 ", "[182] ", "[182 ", "182] ", "182. " at start of text
        cleaned = re.sub(rf"^(?:\[?{seq}\]?[\.\:\-\)]?\s*)", "", text)
        # Also strip any leading bracketed or numbered prefix that may have leaked, e.g. "[46 " or "46. "
        cleaned = re.sub(r"^\[?\d+\]?[\.\:\-\)]\s*", "", cleaned)
        cleaned = re.sub(r"^\[\d+\s+", "", cleaned)
        cleaned = clean_hallucinated_tags(cleaned)
        return cleaned.strip()

    def _commit_current(self):
        if self._current_lines:
            text = " ".join(self._current_lines).strip()
            if self._current_seq is not None:
                text = self._clean_seq_prefix(self._current_seq, text)
            self._completed.append((self._current_seq or 0, [text]))
            self._current_seq = None
            self._current_lines = []

    def put(self, raw: str):
        self._raw_history.append(raw)
        self._raw_stream += raw

        # Process think tags with persistent in_think state
        cleaned = []
        i = 0
        s = self._raw_stream
        while i < len(s):
            if self._in_think:
                end_think = s.find("</think>", i)
                if end_think == -1:
                    end_thought = s.find("</thought>", i)
                    if end_thought == -1:
                        # In-flight think tag; wait for more stream
                        i = len(s)
                        break
                    else:
                        i = end_thought + len("</thought>")
                        self._in_think = False
                else:
                    i = end_think + len("</think>")
                    self._in_think = False
            else:
                start_think = s.find("<think>", i)
                start_thought = s.find("<thought>", i)
                starts = [pos for pos in (start_think, start_thought) if pos != -1]
                if not starts:
                    cleaned.append(s[i:])
                    i = len(s)
                    break
                first_start = min(starts)
                cleaned.append(s[i:first_start])
                tag_len = len("<think>") if first_start == start_think else len("<thought>")
                i = first_start + tag_len
                self._in_think = True

        self._raw_stream = "".join(cleaned)

        if "\n" in self._raw_stream:
            lines = self._raw_stream.replace("\r", "").splitlines(keepends=True)
            if not self._raw_stream.endswith("\n"):
                self._raw_stream = lines.pop()
            else:
                self._raw_stream = ""

            for l in lines:
                self._process_line(l)

    def _process_line(self, l: str):
        s = l.strip()
        # Skip markdown code fences
        if re.match(r"^```\w*$", s):
            return
        if not s:
            self._commit_current()
            return

        # Pattern 1: Closed Bracketed ID: [123] text or **[123]** text or [123]: text
        m_bracket = re.match(r"^(?:\*\*)?\[(\d+)\](?:\*\*)?[\:\.\-]?\s*(.*)$", s)
        if m_bracket:
            self._commit_current()
            self._current_seq = int(m_bracket.group(1))
            line_text = m_bracket.group(2).strip()
            line_text = self._clean_seq_prefix(self._current_seq, line_text)
            self._current_lines = [line_text] if line_text else []
            return

        # Pattern 1b: Unclosed Bracket ID: e.g. "[46 tus estrellas por no tener hijos." or "[46: text"
        m_unclosed = re.match(r"^(?:\*\*)?\[(\d+)[\:\.\-\)]?\s+(.*)$", s)
        if m_unclosed:
            seq_val = int(m_unclosed.group(1))
            if not self.known_seqs or seq_val in self.known_seqs:
                self._commit_current()
                self._current_seq = seq_val
                line_text = m_unclosed.group(2).strip()
                line_text = self._clean_seq_prefix(self._current_seq, line_text)
                self._current_lines = [line_text] if line_text else []
                return

        # Pattern 1c: Unopened Bracket ID: e.g. "46] tus estrellas por no tener hijos."
        m_unopened = re.match(r"^(?:\*\*)?(\d+)\](?:\*\*)?[\:\.\-]?\s*(.*)$", s)
        if m_unopened:
            seq_val = int(m_unopened.group(1))
            if not self.known_seqs or seq_val in self.known_seqs:
                self._commit_current()
                self._current_seq = seq_val
                line_text = m_unopened.group(2).strip()
                line_text = self._clean_seq_prefix(self._current_seq, line_text)
                self._current_lines = [line_text] if line_text else []
                return

        # Pattern 2: Standalone sequence number line: e.g. "1" or "42" or "[42]" or "[42" or "42]"
        m_standalone = re.match(r"^(?:\*\*)?\[?(\d+)\]?(?:\*\*)?[\:\.\-]?$", s)
        if m_standalone:
            val = int(m_standalone.group(1))
            if val in self.known_seqs:
                self._commit_current()
                self._current_seq = val
                self._current_lines = []
                return
            elif self._current_seq is not None and not self._current_lines:
                # First line of dialogue happens to be a number (not a known seq ID)
                self._current_lines.append(s)
                return
            elif not self.known_seqs:
                self._commit_current()
                self._current_seq = val
                self._current_lines = []
                return

        # Pattern 3: Numbered list format with punctuation: e.g. "1. Hello", "2: World", "3) Test"
        m_punct = re.match(r"^(\d+)[\.\:\-\)]\s+(.*)$", s)
        if m_punct:
            seq_val = int(m_punct.group(1))
            if not self.known_seqs or seq_val in self.known_seqs:
                self._commit_current()
                self._current_seq = seq_val
                line_text = m_punct.group(2).strip()
                line_text = self._clean_seq_prefix(self._current_seq, line_text)
                self._current_lines = [line_text] if line_text else []
                return

        # Pattern 4: Bare number followed by space and text, e.g. "48 ¿Recomendaciones estelares?"
        # ONLY if the number is in self.known_seqs!
        m_bare = re.match(r"^(\d+)\s+(.*)$", s)
        if m_bare:
            seq_val = int(m_bare.group(1))
            if seq_val in self.known_seqs:
                self._commit_current()
                self._current_seq = seq_val
                line_text = m_bare.group(2).strip()
                line_text = self._clean_seq_prefix(self._current_seq, line_text)
                self._current_lines = [line_text] if line_text else []
                return

        # Pattern 5: Continuation of current dialogue
        self._current_lines.append(s)

    def flush(self):
        if self._raw_stream.strip():
            self._process_line(self._raw_stream)
            self._raw_stream = ""
        self._commit_current()

    def has_entry(self) -> bool:
        return bool(self._completed)

    def peek_entry(self, index: int = 0) -> Optional[tuple[int, list[str]]]:
        if 0 <= index < len(self._completed):
            return self._completed[index]
        return None

    def entry_count(self) -> int:
        return len(self._completed)

    def pop_entry(self) -> tuple[int, list[str]]:
        return self._completed.pop(0)

    def get_raw_history(self) -> str:
        return "".join(self._raw_history)

    def __bool__(self):
        return bool(self._completed) or bool(self._current_seq is not None) or bool(self._raw_stream)


def drain_buf(
    buf: RespBuf,
    orig_by_seq: dict[int, SubtitleLine],
    remaining_seqs: list[int],
    handled_seqs: set[int],
    is_flush: bool = False,
) -> list[SubtitleLine]:
    results = []
    while buf.has_entry():
        expected_seq = remaining_seqs[0] if remaining_seqs else None
        next_entry = buf.peek_entry(1)

        peek_curr = buf.peek_entry(0)
        curr_seq, _ = peek_curr

        # If sequence number is mismatched and no lookahead is ready yet,
        # wait for the next stream chunk before committing unless flushing.
        if (
            not is_flush
            and expected_seq is not None
            and curr_seq != expected_seq
            and next_entry is None
        ):
            break

        seq, lines = buf.pop_entry()

        # Sequence healing
        if expected_seq is not None:
            if next_entry is not None:
                next_seq = next_entry[0]
                # Preamble / chatty LLM check: if lookahead is the expected sequence
                # and this entry is unknown or already handled, discard as preamble chatter
                if next_seq == expected_seq and (seq not in orig_by_seq or seq in handled_seqs):
                    logging.info(
                        "Skipping preamble/chatter entry [%s] because lookahead [%s] matches expected",
                        seq,
                        next_seq,
                    )
                    continue

                # Healing check: sequence went backwards, is duplicate, or unknown,
                # but lookahead points to a valid remaining sequence
                if (seq > next_seq or seq in handled_seqs or seq not in orig_by_seq) and next_seq in remaining_seqs:
                    idx = remaining_seqs.index(next_seq)
                    if idx > 0:
                        target_seq = remaining_seqs[idx - 1]
                        if target_seq not in handled_seqs:
                            logging.info(
                                "Healed sequence number [%s] -> [%s] (lookahead next is [%s])",
                                seq,
                                target_seq,
                                next_seq,
                            )
                            seq = target_seq
            elif is_flush:
                if len(remaining_seqs) == 1 and not buf.has_entry():
                    target_seq = remaining_seqs[0]
                    if seq != target_seq:
                        logging.info(
                            "Healed final sequence number [%s] -> [%s] on stream flush",
                            seq,
                            target_seq,
                        )
                        seq = target_seq
                elif seq in handled_seqs and expected_seq not in handled_seqs:
                    logging.info(
                        "Healed duplicate sequence number [%s] -> [%s] on stream flush",
                        seq,
                        expected_seq,
                    )
                    seq = expected_seq

        src_line = None
        if seq in orig_by_seq:
            if seq not in handled_seqs:
                src_line = orig_by_seq[seq]
                handled_seqs.add(seq)
                if seq in remaining_seqs:
                    remaining_seqs.remove(seq)
            else:
                # Duplicate response for already handled seq; skip
                logging.warning("Skipping duplicate response for already handled seq %s", seq)
                continue
        else:
            # Seq unknown (e.g. 0 or model hallucinated seq number)
            if remaining_seqs:
                fallback_seq = remaining_seqs.pop(0)
                src_line = orig_by_seq[fallback_seq]
                handled_seqs.add(fallback_seq)
                logging.info(
                    "Mapped unknown sequence number [%s] to fallback [%s]",
                    seq,
                    fallback_seq,
                )
            else:
                continue

        if src_line is not None:
            raw_text = " ".join(lines).strip()
            orig_text = "\n".join(src_line.text_lines)
            if not raw_text:
                restored_text = orig_text
            else:
                restored_text = apply_translation_to_text(orig_text, raw_text)
            wrapped_lines = wrap_subtitle_text(restored_text)
            results.append(SubtitleLine(src_line.seq, src_line.time_line, wrapped_lines))
    return results


def translate_subtitle_batch(
    openai: OpenAI,
    model: str,
    prompt_dev: str,
    prompt_vars: dict[str, Any],
    batch_lines: list[SubtitleLine],
    ipc: Optional[TextIO] = None,
    reasoning_effort: str = "none",
    history_holder: Optional[dict] = None,
) -> Iterator[SubtitleLine]:
    # Resolve known model aliases and auto-prefix provider if missing on OpenRouter
    resolved_model = model
    if "openrouter.ai" in str(openai.base_url):
        resolved_model = OPENROUTER_MODEL_ALIASES.get(model, model)
        if "/" not in resolved_model:
            if resolved_model.startswith("gpt-") or resolved_model.startswith("o1") or resolved_model.startswith("o3"):
                resolved_model = f"openai/{resolved_model}"
            elif resolved_model.startswith("claude-"):
                resolved_model = f"anthropic/{resolved_model}"
            elif resolved_model.startswith("deepseek-"):
                resolved_model = f"deepseek/{resolved_model}"
            elif resolved_model.startswith("gemini-"):
                resolved_model = f"google/{resolved_model}"
            elif resolved_model.startswith("glm-"):
                resolved_model = f"z-ai/{resolved_model}"
            elif "nemotron" in resolved_model:
                resolved_model = f"nvidia/{resolved_model}"

    # send request
    user_prompt = PROMPT_USER.format(
        srt_content="\n".join(l.format_bracket_line() for l in batch_lines),
        **prompt_vars,
    )

    extra_body = None
    max_tokens = max(4096, min(8192, len(batch_lines) * 50))
    is_openrouter = "openrouter.ai" in str(openai.base_url)

    if is_openrouter:
        if reasoning_effort == "none":
            extra_body = {
                "reasoning": {
                    "effort": "none",
                    "exclude": True,
                },
                "provider": {"ignore": ["Wafer"]},
            }
        else:
            extra_body = {
                "reasoning": {
                    "effort": reasoning_effort,
                    "exclude": True,
                },
                "provider": {"ignore": ["Wafer"]},
            }
    elif "deepseek-v4" in resolved_model.lower() or "deepseek-r1" in resolved_model.lower():
        if reasoning_effort == "none":
            extra_body = {"thinking": {"type": "disabled"}}
        else:
            extra_body = {"thinking": {"type": "enabled"}}
            max_tokens = 4096
    elif reasoning_effort != "none":
        extra_body = {"reasoning_effort": reasoning_effort}
        max_tokens = 4096

    stream = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            stream = openai.chat.completions.create(
                model=resolved_model,
                stream=True,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": prompt_dev},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body=extra_body,
            )
            break
        except (RateLimitError, APIConnectionError, APITimeoutError) as err:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            delay = RATE_LIMIT_RETRY_DELAY * (attempt + 1)
            err_type = "rate limited (429)" if isinstance(err, RateLimitError) else "network/timeout error"
            logging.warning(
                "%s, retry request in %.0fs (%d/%d): %s",
                err_type,
                delay,
                attempt + 1,
                MAX_RATE_LIMIT_RETRIES,
                err,
            )
            if ipc is not None:
                try:
                    ipc.seek(0)
                    json.dump(
                        dict(
                            status="rate_limited" if isinstance(err, RateLimitError) else "network_retry",
                            retry_in=int(delay),
                            attempt=attempt + 1,
                            max_retries=MAX_RATE_LIMIT_RETRIES,
                        ),
                        ipc,
                    )
                    ipc.truncate()
                    ipc.flush()
                except Exception:
                    pass
            time.sleep(delay)
        except APIError as err:
            status_code = getattr(err, "status_code", None)
            if attempt < MAX_RATE_LIMIT_RETRIES and status_code in (500, 502, 503, 504):
                delay = RATE_LIMIT_RETRY_DELAY * (attempt + 1)
                logging.warning(
                    "server error (%s), retry request in %.0fs (%d/%d): %s",
                    status_code,
                    delay,
                    attempt + 1,
                    MAX_RATE_LIMIT_RETRIES,
                    err,
                )
                time.sleep(delay)
                continue
            err_str = str(err).lower()
            if extra_body and "reasoning is mandatory" in err_str:
                logging.info("Endpoint mandates reasoning, falling back to reasoning effort 'low'")
                extra_body = {
                    "reasoning": {"effort": "low", "exclude": True},
                    "provider": {"ignore": ["Wafer"]},
                }
                max_tokens = 2048
                continue
            if extra_body is not None:
                logging.warning("Request failed with extra_body, retrying without it: %s", err)
                extra_body = None
                continue
            raise
    assert stream is not None

    # parse response
    known_seqs = {l.seq for l in batch_lines}
    orig_by_seq = {l.seq: l for l in batch_lines}
    remaining_seqs = [l.seq for l in batch_lines]
    handled_seqs: set[int] = set()
    buf = RespBuf(known_seqs=known_seqs)
    if history_holder is not None:
        history_holder["raw"] = buf.get_raw_history
    try:
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            content = choice.delta.content
            if content:
                buf.put(content)

            for line in drain_buf(buf, orig_by_seq, remaining_seqs, handled_seqs, is_flush=False):
                yield line

            if choice.finish_reason is not None:
                if choice.finish_reason not in ("stop", "length"):
                    logging.warning("unknown finish reason %s", choice.finish_reason)
                buf.flush()
                for line in drain_buf(buf, orig_by_seq, remaining_seqs, handled_seqs, is_flush=True):
                    yield line
                return

        buf.flush()
        for line in drain_buf(buf, orig_by_seq, remaining_seqs, handled_seqs, is_flush=True):
            yield line
    except Exception as err:
        setattr(err, "_raw_response", buf.get_raw_history())
        raise err


def dump_error_log(
    args: "Args",
    error: Exception,
    batch: Optional[list[SubtitleLine]] = None,
    raw_response: Optional[str] = None,
):
    try:
        out_dir = Path(args.output_path).parent
        if out_dir.name == ".subtrans_chunks":
            out_dir = out_dir.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        log_file = out_dir / "llm_subtrans_error.log"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=== LLM SubTrans Error Log ===\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Model: {args.model or '(auto)'}\n")
            f.write(f"Base URL: {args.base_url or '(auto)'}\n")
            f.write(f"Target Language: {args.dest_lang_with_default}\n")
            f.write(f"Video URL: {args.video_url}\n")
            f.write(f"Subtitle URL: {args.subtitle_url or '(internal)'}\n")
            f.write(f"Output Path: {args.output_path}\n")
            f.write(
                f"Window: start_offset={args.start_offset}s, max_duration={args.max_duration}s, start_seq={args.start_seq}\n"
            )
            f.write("\n--- Exception Traceback ---\n")
            import traceback
            import sys
            exc_tb = traceback.format_exc()
            if sys.exc_info()[0] is not None and exc_tb.strip() != "NoneType: None":
                f.write(exc_tb)
            else:
                f.write(f"{type(error).__name__}: {error}\n")
            if batch:
                f.write(f"\n--- Current Batch Input ({len(batch)} items) ---\n")
                for item in batch[:15]:
                    f.write(f"[{item.seq}] {item.time_line}: {' '.join(item.text_lines)}\n")
                if len(batch) > 15:
                    f.write(f"... and {len(batch) - 15} more lines\n")
            if raw_response:
                f.write("\n--- Raw Response Buffer (LLM Output) ---\n")
                f.write(raw_response)
                f.write("\n")
        logging.info("Detailed error dump written to %s", log_file)
    except Exception as e:
        logging.error("Failed to write error dump: %s", e)


@dataclass
class Args:
    api_key: str
    model: str
    base_url: str
    ffmpeg_bin: str
    video_url: str
    sub_track_id: int
    subtitle_url: str
    dest_lang: str
    batch_size: int
    output_path: str
    ipc_path: str
    extra_prompt: str
    start_offset: float
    max_duration: float
    start_seq: int
    reasoning_effort: str = "none"
    max_lines: int = 0

    def build_openai_client(self) -> tuple[OpenAI, str]:
        key = self.api_key
        if not key:
            # fall back to the environment for manual invocation
            key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise KeyError("No OPENAI_API_KEY set")
        base_url = None
        model = None
        for platform in PLATFORM_DEFAULTS.values():
            if re.fullmatch(platform.key_regex, key):
                base_url = platform.base_url
                model = platform.model
                break
        if self.model:
            model = self.model
        if self.base_url:
            base_url = self.base_url
        if model is None:
            if (base_url and "openrouter.ai" in base_url) or key.startswith("sk-or-v1-"):
                model = "deepseek/deepseek-chat"
            else:
                raise ValueError("No model specified")

        default_headers = None
        is_openrouter = (base_url and "openrouter.ai" in base_url) or key.startswith("sk-or-v1-")
        if is_openrouter:
            default_headers = {
                "HTTP-Referer": "https://github.com/escapezn/mpv-llm-subtrans",
                "X-Title": "mpv-llm-subtrans",
            }

        return (
            OpenAI(
                api_key=key,
                base_url=base_url,
                max_retries=5,
                default_headers=default_headers,
            ),
            model,
        )

    @property
    def dest_lang_with_default(self) -> str:
        if not self.dest_lang:
            loc, _ = locale.getlocale()
            if loc is None or loc == "C":
                return "English"
            else:
                return loc
        else:
            return self.dest_lang

    @property
    def prompt_vars(self) -> dict[str, Any]:
        return dict(
            dest_lang=self.dest_lang_with_default,
            video_name=Path(self.video_url).stem,
            subtitle_name=Path(self.subtitle_url).stem,
            extra_prompt=self.extra_prompt,
        )


def get_cli_args() -> Args:
    parser = argparse.ArgumentParser(
        prog="mpv-llm-subtrans",
        description="MPV plugin for translating subtitles with LLM",
    )
    parser.add_argument("--api-key", default="", help="API key")
    parser.add_argument("--model", default="", help="Model name")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--ffmpeg-bin", default="", help="ffmpeg execute path")
    parser.add_argument("--video-url", default="", help="video file path")
    parser.add_argument(
        "--sub-track-id",
        default=0,
        type=int,
        help="track id of subtitle, start from 0",
    )
    parser.add_argument(
        "--subtitle-url", default="", help="standalone subtitle file path"
    )
    parser.add_argument("--dest-lang", default="", help="Destination language")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--ipc-path", required=True)
    parser.add_argument("--extra-prompt", default="")
    parser.add_argument(
        "--start-offset",
        type=float,
        default=0,
        help="Skip subtitles ending before this time (seconds)",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=0,
        help="Stop after translating this many seconds of content from start-offset (0=unlimited)",
    )
    parser.add_argument(
        "--start-seq",
        type=int,
        default=0,
        help="Skip subtitles with sequence number <= this value",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=0,
        help="Maximum number of subtitle lines to process (0=unlimited)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="none",
        help="Reasoning effort level: none, low, medium, high",
    )
    return Args(**vars(parser.parse_args()))


class Progress(TypedDict):
    last_seq: int
    last_timestamp_millis: tuple[int, int]


def filter_by_offset(
    lines: Iterator[SubtitleLine],
    start_offset_ms: float,
    end_offset_ms: float,
    start_seq: int,
    max_lines: int = 0,
) -> Iterator[SubtitleLine]:
    """Filter subtitle lines to a time window or maximum line count.

    Skips lines ending before start_offset_ms, stops when lines start
    at or after end_offset_ms or when max_lines is reached. Also skips lines
    with seq <= start_seq for precise chunk boundary control.
    """
    count = 0
    for line in lines:
        if start_seq > 0 and line.seq <= start_seq:
            continue  # skip already-translated lines by sequence number
        ts = line.timestamp_millis
        # If start_seq is specified, don't skip slightly overlapping lines at the chunk boundary
        if ts[1] < start_offset_ms:
            if start_seq > 0 and (start_offset_ms - ts[1]) < 10000:
                pass
            else:
                continue  # skip lines ending before the window
        if end_offset_ms > 0 and ts[0] >= end_offset_ms:
            return  # stop when past the window
        yield line
        count += 1
        if max_lines > 0 and count >= max_lines:
            return


def process(args: Args, ipc: TextIO):
    openai, model = args.build_openai_client()
    logging.info("Target language: %s", args.dest_lang_with_default)
    logging.info("Model: %s", model)

    if args.subtitle_url:
        # Use standalone srt file
        subtitle_lines = read_subtitle_from_srt(args.subtitle_url)
    else:
        # Extract subtitle with ffmpeg (async)
        subtitle_lines = extract_subtitle_from_video(
            args.ffmpeg_bin, args.video_url, args.sub_track_id
        )

    # Apply time-window and line-count filtering
    start_offset_ms = args.start_offset * 1000
    end_offset_ms = (
        (args.start_offset + args.max_duration) * 1000
        if args.max_duration > 0
        else 0
    )
    if start_offset_ms > 0 or end_offset_ms > 0 or args.start_seq > 0 or args.max_lines > 0:
        logging.info(
            "Filtering: start_offset=%.1fs, max_duration=%.1fs, start_seq=%d, max_lines=%d",
            args.start_offset,
            args.max_duration,
            args.start_seq,
            args.max_lines,
        )
        subtitle_lines = filter_by_offset(
            subtitle_lines, start_offset_ms, end_offset_ms, args.start_seq, args.max_lines
        )

    # Translate (async)
    translated = translate_subtitle(
        openai=openai,
        model=model,
        batch_size=args.batch_size,
        prompt_vars=args.prompt_vars,
        lines=subtitle_lines,
        ipc=ipc,
        args=args,
    )

    # Write out
    srt_path = Path(args.output_path)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Write to %s", srt_path.resolve())

    translated_subs: dict[int, SubtitleLine] = {}
    with srt_path.open("w", encoding="utf-8") as srt:
        for line in translated:
            translated_subs[line.seq] = line
            srt.seek(0)
            srt.truncate()
            for s in sorted(translated_subs.keys()):
                srt.write(translated_subs[s].format_full())
                srt.write("\n\n")
            srt.flush()

            ipc.seek(0)
            json.dump(
                dict(
                    status="translating",
                    last_seq=max(translated_subs.keys()),
                    last_timestamp_millis=line.timestamp_millis,
                    lines_done=len(translated_subs),
                    is_eof=False,
                ),
                ipc,
            )
            ipc.truncate()
            ipc.flush()

    # Final completion status in IPC
    is_eof = False
    if args.max_lines > 0 and len(translated_subs) < args.max_lines:
        is_eof = True
    elif len(translated_subs) == 0:
        is_eof = True

    if translated_subs:
        last_line = translated_subs[max(translated_subs.keys())]
        ipc.seek(0)
        json.dump(
            dict(
                status="completed",
                last_seq=max(translated_subs.keys()),
                last_timestamp_millis=last_line.timestamp_millis,
                lines_done=len(translated_subs),
                is_eof=is_eof,
            ),
            ipc,
        )
        ipc.truncate()
        ipc.flush()
    else:
        ipc.seek(0)
        json.dump(
            dict(
                status="completed",
                lines_done=0,
                is_eof=True,
            ),
            ipc,
        )
        ipc.truncate()
        ipc.flush()


def main():
    logging.basicConfig(level=logging.INFO)
    args = get_cli_args()

    ipc_path = Path(args.ipc_path)
    ipc_path.parent.mkdir(parents=True, exist_ok=True)
    with ipc_path.open("w", encoding="utf-8") as ipc:
        try:
            process(args, ipc)
        except Exception as err:
            dump_error_log(args, err)
            ipc.seek(0)
            json.dump(dict(panic=f"{err}"), ipc)
            ipc.truncate()
            ipc.flush()
            raise err


if __name__ == "__main__":
    main()
