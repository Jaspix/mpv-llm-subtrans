# llm-subtrans

Translate video subtitles with OpenAI(-compatible) language models.
A [mpv player](https://mpv.io/) script.

## Features

- **Fast & Streaming.** Translation streams line by line and appears within seconds after some startup time.
- **Batch-Based & Progressive Chunking.** Translates continuously as you watch with zero dialogue gaps (`chunk_by_batch=yes`), or by time-based windows.
- **Auto-Healing, Error Handling & Sequence Resiliency.** Recovers seamlessly from model tag hallucinations, duplicate sequence tags, or dropped lines without desync.
- **Reasoning Effort Control.** Configurable reasoning effort (`reasoning_effort=none|low|medium|high`) .
- **Contextual.** Feeds conversation context and video metadata to preserve natural phrasing and idiomatic tone.
- **Multi-Provider Support.** Built-in support for OpenRouter, OpenAI, DeepSeek, and custom OpenAI-compatible endpoints.
- **Customizable OSD.** Compact, configurable on-screen display (`osd_font_size`) with live percentage and line progress.

Tested on Linux (Arch), might work with Windows.

Both internal subtitles in video files and external subtitle files are supported:
- **Internal** subtitles rely on FFmpeg and support **both SRT & ASS formats**. HTTP(S) video streaming is supported.
- **External** subtitles support local SRT files.

### Why you SHOULD NOT use it

This script is built for fast, frictionless viewing while watching. If you need manual timing adjustment, manual subtitle editing, or speech-to-text audio transcription, use dedicated tools.

Some complex ASS styles (animations, drawings) are simplified during extraction. (.ass subtitle compatibility in progress)

## Prerequisites

- [FFmpeg](https://www.ffmpeg.org/)
- [OpenRouter](https://openrouter.ai/), [OpenAI](https://platform.openai.com/api-keys), or [DeepSeek](https://platform.deepseek.com/api_keys) API key
- [Python](https://python.org) (3.10+) with `openai`:
  ```bash
  pip install openai
  ```
  *(or use `uv`)*

## Quick Start

### Linux

```bash
# 1. Clone this repository into your mpv scripts directory
git clone https://github.com/jaspix/mpv-llm-subtrans.git ~/.config/mpv/scripts/mpv-llm-subtrans

# 2. Copy the config template and add your API key
cp ~/.config/mpv/scripts/mpv-llm-subtrans/llm_subtrans.conf ~/.config/mpv/script-opts/
nano ~/.config/mpv/script-opts/llm_subtrans.conf

# 3. Play any video in mpv
mpv video.mkv
# Select the subtitle track you want to translate
# Press Alt+T to start progressive translation!
```


## Configuration

See [`llm_subtrans.conf`](llm_subtrans.conf) for all options.

Key settings include:
- `api_key`: Your API key (or set the `OPENAI_API_KEY` environment variable).
- `model`: Target model (e.g., `deepseek/deepseek-chat`, `gpt-4o-mini`, `nvidia/nemotron-3.5-lightning`).
- `dest_lang`: Target language (e.g., `es`, `es-la`, `ja`, `fr`, `zh`). Default is system language or English.
- `chunk_by_batch`: (Default `yes`) Chunks by batch count (e.g. 50 lines) instead of fixed seconds to prevent dialogue cutoff.
- `continuous_mode`: (Default `no`) Translates subsequent chunks immediately in the background without waiting for playback to approach the threshold.
- `reasoning_effort`: Set to `none`, `low`, `medium`, or `high` for reasoning-capable models.
- `osd_font_size`: Adjust the size of status and progress messages (default: `20`).

## Tested Models & Providers

- **OpenRouter**:
  - `openai/gpt-5.6-luna` (Recommended for cost/speed)
  - `google/gemini-3.8-flash`
  - `z-ai/glm-5.3-flash`
  - `deepseek/deepseek-v4.1-flash`
  - `openai/gpt-4o-mini`
  - `nvidia/nemotron-3.5-lightning`
  - `google/gemini-2.5-flash`
- **OpenAI**: `gpt-4o-mini`, `gpt-4o`
- **DeepSeek**: `deepseek-chat`

## Shortcuts & Controls

- `Alt+T`: Toggle **progressive translation** (translates ahead from current playback position). Press again to cancel.
- `Alt+Shift+T`: Toggle **full translation** (translates the entire subtitle track in one pass). Press again to cancel.

Subtitles are loaded automatically on the fly as each line streams in. Temporary chunk files are cleaned up automatically when mpv exits. Detailed error logs are written to `llm_subtrans_error.log` if an error occurs.
