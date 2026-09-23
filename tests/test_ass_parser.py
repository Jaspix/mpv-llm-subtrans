#!/usr/bin/env python3
"""
Unit tests for ass_parser.py using real-world fansub files.
"""

import unittest
from pathlib import Path
import tempfile
import sys
import os

# Add parent directory to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ass_parser


EXAMPLE_DIR = REPO_ROOT.parent / "example subs"


class TestASSParser(unittest.TestCase):

    def test_prefix_tag_extraction(self):
        """Test separating leading ASS override blocks from translatable text."""
        raw = r"{\an8\pos(971,395)\c&HD8D8D8&}Your account has been \Ntemporarily suspended"
        prefix, payload = ass_parser.extract_prefix_tags(raw)
        self.assertEqual(prefix, r"{\an8\pos(971,395)\c&HD8D8D8&}")
        self.assertEqual(payload, r"Your account has been \Ntemporarily suspended")

        # Test line without tags
        prefix2, payload2 = ass_parser.extract_prefix_tags("Good morning, everyone!")
        self.assertEqual(prefix2, "")
        self.assertEqual(payload2, "Good morning, everyone!")

        # Test reconstruction
        reconstructed = ass_parser.reconstruct_event_text(prefix, r"Su cuenta ha sido \Nsuspendida temporalmente")
        self.assertEqual(
            reconstructed,
            r"{\an8\pos(971,395)\c&HD8D8D8&}Su cuenta ha sido \Nsuspendida temporalmente",
        )

    def test_inline_tag_mapping(self):
        r"""Test converting {\i1}..{\i0} to <i>..</i> and back."""
        original = r'"The {\i1}best{\i0} maids"?'
        encoded = ass_parser.encode_inline_tags(original)
        self.assertEqual(encoded, '"The <i>best</i> maids"?')

        # Translate in Spanish keeping HTML tag
        translated = '¿Las <i>mejores</i> sirvientas?'
        decoded = ass_parser.decode_inline_tags(translated)
        self.assertEqual(decoded, r'¿Las {\i1}mejores{\i0} sirvientas?')

    def test_classification_koba(self):
        """Test filtering on the 11,000-line Kobayashi dragon maid fansub."""
        koba_path = EXAMPLE_DIR / "koba.ass"
        if not koba_path.exists():
            self.skipTest(f"{koba_path} not found")

        doc = ass_parser.parse_ass_file(koba_path)
        self.assertEqual(len(doc.events), 11382)  # 11,167 dialogues + 215 comments

        translatable = [e for e in doc.events if ass_parser.is_translatable_event(e)]
        
        # Verify that drawings and generated fx lines are filtered out, keeping English song lyrics
        self.assertLess(len(translatable), 1700)  # Dialogues + motion tracked pSigns + OP/ED English TL
        self.assertGreater(len(translatable), 1500)

        # Verify translation units: all 1,000+ pSigns collapse into ~480 unique units!
        units = ass_parser.prepare_translation_units(doc)
        self.assertLess(len(units), 500)
        self.assertGreater(len(units), 450)

        # Verify that spoken dialogues (e.g. Tohru & Kobayashi) are included
        dialogue_texts = [e.text for e in translatable]
        self.assertTrue(any("Miss Kobayashi!" in t for t in dialogue_texts))
        self.assertTrue(any("demons' den" in t for t in dialogue_texts))

        # Verify that pure vector drawings are NOT in translatable
        self.assertFalse(any(r"\p1" in t for t in dialogue_texts))

    def test_classification_mitsu(self):
        """Test filtering on Mitsudomoe fansub with Romaji + TL song tracks."""
        mitsu_path = EXAMPLE_DIR / "mitsu.ass"
        if not mitsu_path.exists():
            self.skipTest(f"{mitsu_path} not found")

        doc = ass_parser.parse_ass_file(mitsu_path)
        translatable = [e for e in doc.events if ass_parser.is_translatable_event(e)]

        # Verify RomajiOP lines with \k are filtered out
        trans_styles = {e.style for e in translatable}
        self.assertNotIn("RomajiOP", trans_styles)
        self.assertNotIn("RomajiED", trans_styles)

        # Verify TLOP (English song lyrics) and Default dialogues are included
        self.assertIn("TLOP", trans_styles)
        self.assertIn("Default", trans_styles)

        # Verify dialogue with speaker names is preserved
        actors = {e.name for e in translatable if e.name}
        self.assertIn("Futaba", actors)
        self.assertIn("Yabe", actors)

    def test_classification_hana(self):
        """Test filtering on Hanamaru Kindergarten fansub."""
        hana_path = EXAMPLE_DIR / "hana.ass"
        if not hana_path.exists():
            self.skipTest(f"{hana_path} not found")

        doc = ass_parser.parse_ass_file(hana_path)
        units = ass_parser.prepare_translation_units(doc)
        self.assertGreater(len(units), 350)
        self.assertLess(len(units), 420)

    def test_classification_kini(self):
        """Test filtering on Kiniro Mosaic fansub."""
        kini_path = EXAMPLE_DIR / "kini.ass"
        if not kini_path.exists():
            self.skipTest(f"{kini_path} not found")

        doc = ass_parser.parse_ass_file(kini_path)
        units = ass_parser.prepare_translation_units(doc)
        self.assertGreater(len(units), 300)
        self.assertLess(len(units), 400)

    def test_roundtrip_fidelity(self):
        """Test that parsing and re-writing an ASS file maintains exact structure."""
        mitsu_path = EXAMPLE_DIR / "mitsu.ass"
        if not mitsu_path.exists():
            self.skipTest(f"{mitsu_path} not found")

        doc1 = ass_parser.parse_ass_file(mitsu_path)
        with tempfile.NamedTemporaryFile("w+", suffix=".ass", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            ass_parser.write_ass_file(doc1, tmp_path)
            doc2 = ass_parser.parse_ass_file(tmp_path)

            self.assertEqual(len(doc1.styles), len(doc2.styles))
            self.assertEqual(len(doc1.events), len(doc2.events))
            self.assertEqual(doc1.events[0].text, doc2.events[0].text)
            self.assertEqual(doc1.events[-1].text, doc2.events[-1].text)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


    def test_apply_translation_units(self):
        """Test translating units and verifying prefix preservation and event text update."""
        raw_ass = """[Script Info]
Title: Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1
Style: Signs,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:01:00.00,0:01:03.00,Default,Tohru,0,0,0,,{\\i1}Hello{\\i0}, world!
Dialogue: 1,0:01:05.00,0:01:08.00,Signs,,0,0,0,,{\\an8\\pos(960,100)\\c&H0000FF&}Tokyo Station
Dialogue: 2,0:01:05.00,0:01:08.00,Signs,,0,0,0,,{\\an8\\pos(960,100)\\c&HFFFFFF&\\blur2}Tokyo Station
Dialogue: 0,0:01:09.00,0:01:10.00,Default,,0,0,0,,{\\p1}m 0 0 l 10 10{\\p0}
"""
        import io
        doc = ass_parser.parse_ass_stream(io.StringIO(raw_ass))
        units = ass_parser.prepare_translation_units(doc)

        # Vector drawing should NOT be a translation unit
        self.assertEqual(len(units), 2)  # 1 dialogue, 1 sign (deduplicated across layers 1 and 2)

        self.assertEqual(units[0].payload, "<i>Hello</i>, world!")
        self.assertEqual(units[0].speaker, "Tohru")
        self.assertEqual(units[1].payload, "Tokyo Station")
        self.assertEqual(len(units[1].event_targets), 2)  # Targets both sign layers

        # Apply translations
        translated_map = {
            1: "¡<i>Hola</i>, mundo!",
            2: "Estación de Tokio",
        }
        updated_doc = ass_parser.apply_translation_units(doc, translated_map)

        # Check dialogue 0: HTML tag converted to ASS tag
        self.assertEqual(updated_doc.events[0].text, r"¡{\i1}Hola{\i0}, mundo!")

        # Check sign layers: prefix tags intact, both layers updated with same text
        self.assertEqual(updated_doc.events[1].text, r"{\an8\pos(960,100)\c&H0000FF&}Estación de Tokio")
        self.assertEqual(updated_doc.events[2].text, r"{\an8\pos(960,100)\c&HFFFFFF&\blur2}Estación de Tokio")

        # Check vector drawing: completely untouched
        self.assertEqual(updated_doc.events[3].text, r"{\p1}m 0 0 l 10 10{\p0}")

    def test_process_ass_end_to_end(self):
        """Test full subtrans.process execution for ASS format with mock streaming."""
        from unittest.mock import patch
        import subtrans
        import json

        raw_ass = """[Script Info]
Title: Dragon Maid Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1
Style: Signs,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,Tohru,0,0,0,,Miss Kobayashi!
Dialogue: 1,0:00:04.00,0:00:07.00,Signs,,0,0,0,,{\\an8\\pos(960,100)}Maid Cafe Open
Dialogue: 0,0:00:08.00,0:00:09.00,Default,,0,0,0,,{\\p1}m 0 0 l 10 10{\\p0}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_ass = Path(tmpdir) / "test.ass"
            output_ass = Path(tmpdir) / "test.es.ass"
            ipc_file = Path(tmpdir) / ".progress"
            input_ass.write_text(raw_ass, encoding="utf-8")

            args = subtrans.Args(
                api_key="sk-test",
                model="gpt-4o-mini",
                base_url="",
                ffmpeg_bin="ffmpeg",
                video_url="",
                sub_track_id=0,
                subtitle_url=str(input_ass),
                dest_lang="es",
                batch_size=50,
                output_path=str(output_ass),
                ipc_path=str(ipc_file),
                extra_prompt="",
                start_offset=0,
                max_duration=0,
                start_seq=0,
                format="ass",
            )

            def mock_translate_subtitle(**kwargs):
                for line in kwargs["lines"]:
                    yield subtrans.SubtitleLine(
                        seq=line.seq,
                        time_line=line.time_line,
                        text_lines=[f"ES_{line.text_lines[0]}"],
                    )

            with patch.object(subtrans.Args, "build_openai_client", return_value=(None, "mock-model")):
                with patch("subtrans.translate_subtitle", side_effect=mock_translate_subtitle):
                    with ipc_file.open("w", encoding="utf-8") as ipc:
                        subtrans.process(args, ipc)

            # 1. Output file must exist
            self.assertTrue(output_ass.exists())

            # 2. Parse output and verify translated dialogues
            out_doc = ass_parser.parse_ass_file(output_ass)
            self.assertEqual(len(out_doc.events), 3)

            # Check dialogue 1: Tohru translated
            self.assertEqual(out_doc.events[0].text, "ES_Miss Kobayashi!")
            # Check sign: prefix preserved, text translated
            self.assertEqual(out_doc.events[1].text, r"{\an8\pos(960,100)}ES_Maid Cafe Open")
            # Check vector drawing: untouched
            self.assertEqual(out_doc.events[2].text, r"{\p1}m 0 0 l 10 10{\p0}")

            # 3. Check IPC status
            ipc_data = json.loads(ipc_file.read_text(encoding="utf-8"))
            self.assertEqual(ipc_data["status"], "completed")
            self.assertEqual(ipc_data["format"], "ass")
            self.assertEqual(ipc_data["lines_done"], 2)
            self.assertTrue(ipc_data["is_eof"])

    def test_prefix_font_weight_override(self):
        """Test that layout blocks containing \\b0 or \\b1 do not emit rogue </b> or <b>."""
        fire_raw = r"{\fscx16\fscy15\an8\fnpuffmod1\b0\c&HC2AAA0&\pos(1576.5,233.5)\blur0.5}Fire"
        pfx, payload = ass_parser.prepare_translatable_payload(
            ass_parser.ASSEvent(False, 0, "0:00:00.00", "0:00:01.00", "pSigns", "", "0", "0", "0", "", fire_raw, fire_raw, 0)
        )
        self.assertEqual(payload, "Fire")
        self.assertIn(r"\b0", pfx)
        self.assertNotIn("</b>", payload)
        reconstructed = ass_parser.reconstruct_event_text(pfx, "Fuego")
        self.assertEqual(reconstructed, r"{\fscx16\fscy15\an8\fnpuffmod1\b0\c&HC2AAA0&\pos(1576.5,233.5)\blur0.5}Fuego")

        hydrant_raw = r"{\fscx11\fscy13\an8\fnpuffmod1\b0\c&HC2AAA0&\pos(1576.5,285.5)\blur0.5}Hydrant"
        pfx_h, payload_h = ass_parser.prepare_translatable_payload(
            ass_parser.ASSEvent(False, 0, "0:00:00.00", "0:00:01.00", "pSigns", "", "0", "0", "0", "", hydrant_raw, hydrant_raw, 0)
        )
        self.assertEqual(payload_h, "Hydrant")
        self.assertNotIn("</b>", payload_h)

    def test_apply_translation_units_streaming_no_drift(self):
        """Test that iterative streaming application does not cause unit drift."""
        raw_ass = """[Script Info]
Title: Drift Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Line One
Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,Line Two
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Line Three
Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,Line Four
Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,Line Five
"""
        import io
        doc = ass_parser.parse_ass_stream(io.StringIO(raw_ass))
        units = ass_parser.prepare_translation_units(doc)
        self.assertEqual(len(units), 5)

        # Simulate streaming line-by-line translation
        trans_map = {}
        for u in units:
            trans_map[u.id] = f"ES_{u.payload}"
            ass_parser.apply_translation_units(doc, units, trans_map)

        # Verify all events have exact matching lines
        self.assertEqual(doc.events[0].text, "ES_Line One")
        self.assertEqual(doc.events[1].text, "ES_Line Two")
        self.assertEqual(doc.events[2].text, "ES_Line Three")
        self.assertEqual(doc.events[3].text, "ES_Line Four")
        self.assertEqual(doc.events[4].text, "ES_Line Five")

    def test_motion_tracking_deduplication(self):
        """Test that animation frames with intermediate tags deduplicate into one unit."""
        raw_ass = """[Script Info]
Title: Motion Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Signs,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:01.10,Signs,,0,0,0,,{\\pos(100,100)}The Oborozuka Times
Dialogue: 0,0:00:01.10,0:00:01.20,Signs,,0,0,0,,{\\pos(102,100)}The Oborozuka{\\ybord11} Times
Dialogue: 0,0:00:01.20,0:00:01.30,Signs,,0,0,0,,{\\pos(104,100)}The Oborozuka{\\ybord10} Times
"""
        import io
        doc = ass_parser.parse_ass_stream(io.StringIO(raw_ass))
        units = ass_parser.prepare_translation_units(doc)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload, "The Oborozuka Times")
        self.assertEqual(len(units[0].event_targets), 3)

    def test_spatial_vs_color_and_newlines(self):
        """Test distinguishing spatial letter animation from newlines and color gradients."""
        # 1. Minami-ke book title: newline followed by font size tag must NOT be flagged
        minami_title = r"Easy-to-Make Original Recipes\N\N\N\N\N\N\N\N\N{\fs66}Cooking at Home"
        self.assertFalse(ass_parser.is_intra_word_transform(minami_title))

        # 2. Kobayashi Zodiac sign: newline followed by font scaling must NOT be flagged
        zodiac_sign = r"{\pos(647,435)}Zodiac Sign\N{\fscx70\fscy70}Leo"
        self.assertFalse(ass_parser.is_intra_word_transform(zodiac_sign))

        # 3. Degenerate Layer 19: intra-word color gradient must NOT be flagged
        degen_layer19 = r"{\pos(517,191)}D{*\c&H1EB3E3&}e{*\c&H21B4E5&}g{*\c&H23B6E7&}e{*\c&H26B7E9&}n{*\c&H28B9EB&}e{*\c&H2BBAED&}r{*\c&H2DBCEF&}a{*\c&H30BDF1&}t{\c&H32BFF3&}e"
        self.assertFalse(ass_parser.is_intra_word_transform(degen_layer19))

        # 4. TV sign 3D perspective mapping: per-character rotation and skew MUST be flagged
        tv_sign = r"N{*\frz25.889\c&HA92019&\fax-0.706}o {*\frz25.608\c&HAC241C&\fax-0.708}h{*\frz25.468\c&HAE261D&}u{*\frz25.327\c&HB0281E&\fax-0.709}m"
        self.assertTrue(ass_parser.is_intra_word_transform(tv_sign))

    def test_fx_english_lyrics_vs_particles(self):
        """Test allowing full English song lyrics while excluding kara-templater particles and drawings."""
        # 1. Valid English lyric line with Effect=fx (Kobayashi ED)
        ev_ed = ass_parser.ASSEvent(
            is_comment=False, layer=2, start="0:22:11.58", end="0:22:14.85",
            style="ED English - 1", name="", margin_l="0", margin_r="0", margin_v="0",
            effect="fx", text=r"{\an5\pos(960,1020)\bord2.3}It's perfectly all right to be small.",
            raw_line="", index=0
        )
        self.assertTrue(ass_parser.is_translatable_event(ev_ed))

        # 2. Kara-templater particle effect (vector drawing + very short duration)
        ev_particle = ass_parser.ASSEvent(
            is_comment=False, layer=2, start="0:01:28.82", end="0:01:28.87",
            style="OP TL", name="", margin_l="0", margin_r="0", margin_v="0",
            effect="fx", text=r"{\p1}m 0 0 l 0 100{\p0}\N{\alpha&HFF&}A",
            raw_line="", index=1
        )
        self.assertFalse(ass_parser.is_translatable_event(ev_particle))

        # 3. Kara-templater decomposed single letter
        ev_letter = ass_parser.ASSEvent(
            is_comment=False, layer=2, start="0:01:28.82", end="0:01:28.90",
            style="OP TL", name="", margin_l="0", margin_r="0", margin_v="0",
            effect="fx", text=r"{\blur2.4\move(284,876,254,876)}A",
            raw_line="", index=2
        )
        self.assertFalse(ass_parser.is_translatable_event(ev_letter))

        # 4. Romaji karaoke line with syllable timing \k
        ev_romaji = ass_parser.ASSEvent(
            is_comment=False, layer=0, start="0:01:07.59", end="0:01:08.51",
            style="OP Romaji", name="", margin_l="0", margin_r="0", margin_v="0",
            effect="fx", text=r"{\k11}{\k22}sing {\k23}a{\k35}long",
            raw_line="", index=3
        )
        self.assertFalse(ass_parser.is_translatable_event(ev_romaji))

    def test_programming_code_excluded(self):
        """Test that source code blocks and filenames are excluded from translation."""
        cplusplus = (
            r"{\fs15\an7\fnCourier New\b0\c&HF68D57&\blur0.9\pos(368,588)}"
            r"#include {\c&H359434&}<maidstream>\N{\c&HD35BA2&}using namespace {\c&H63A1C8&}ddy;\N\N"
            r"{\c&HD35BA2&}int {\c&HF68D57&}main{\c&H393737&}()   {\c&HD35BA2&} \N"
            r" \h\h string {\c&H393737&}dragon-name;\N\N"
            r"    \h\h {\c&H63A1C8&}cout {\c&H393737&}<< {\c&H359434&}\"Enter a Dragon Name: \"{\c&H393737&};\N"
            r"    \h\h {\c&H63A1C8&}cin {\c&H393737&}>> dragon-name;\N\N"
            r"{\c&HD35BA2&}mysql_init{\c&H393737&}(&mysql);\N\N"
            r"\h\h {\c&HF68D57&}return {\c&H63A1C8&}0{\c&H393737&};\N"
        )
        ev_code = ass_parser.ASSEvent(
            is_comment=False, layer=3, start="0:00:01.69", end="0:00:04.44",
            style="Signs", name="IDE", margin_l="0", margin_r="0", margin_v="0",
            effect="", text=cplusplus, raw_line="", index=0
        )
        self.assertTrue(ass_parser.is_programming_code(ev_code.text))
        self.assertFalse(ass_parser.is_translatable_event(ev_code))

        # Test source filename
        ev_file = ass_parser.ASSEvent(
            is_comment=False, layer=3, start="0:00:01.69", end="0:00:04.44",
            style="Signs", name="IDE", margin_l="0", margin_r="0", margin_v="0",
            effect="", text=r"{\fs16\fnCourier New\b0\pos(404,543)}DDY.cpp",
            raw_line="", index=1
        )
        self.assertTrue(ass_parser.is_programming_code(ev_file.text))
        self.assertFalse(ass_parser.is_translatable_event(ev_file))

        # Test regular dialogue with common words
        ev_dialogue = ass_parser.ASSEvent(
            is_comment=False, layer=0, start="0:10:40.94", end="0:10:44.41",
            style="Default", name="", margin_l="0", margin_r="0", margin_v="0",
            effect="", text=r"I'm slightly more experienced than you \Nwhen it comes to scouting out evil.",
            raw_line="", index=2
        )
        self.assertFalse(ass_parser.is_programming_code(ev_dialogue.text))
        self.assertTrue(ass_parser.is_translatable_event(ev_dialogue))

    def test_color_gradient_extraction_and_application(self):
        """Test extracting and proportionally distributing rainbow color gradients."""
        raw = (
            r"{\an2\pos(960,1048)\fad(120,0)\1c&HAAAAAA&}A "
            r"{\1c&HFF0000&}r{\1c&HFF7F00&}a{\1c&HFFFF00&}i{\1c&H00FF00&}n"
            r"{\1c&H0000FF&}b{\1c&H4B0082&}o{\1c&H9400D3&}w!"
        )
        pfx, raw_payload = ass_parser.extract_prefix_tags(raw)
        seq = ass_parser.extract_color_gradient(raw_payload, pfx)
        self.assertGreater(len(seq), 0)
        self.assertIn(r"{\1c&HFF0000&}", seq)
        self.assertIn(r"{\1c&H9400D3&}", seq)

        # Apply to Spanish translation
        trans = "¡Un arcoíris!"
        applied = ass_parser.apply_color_gradient(trans, seq, pfx)
        self.assertIn(r"{\1c&HFF0000&}", applied)
        self.assertIn(r"{\1c&H9400D3&}", applied)

        # Full reconstruction
        reconstructed = ass_parser.reconstruct_event_text(pfx, trans, color_gradient=seq)
        self.assertTrue(reconstructed.startswith(pfx))
        self.assertIn(r"{\1c&H00FF00&}", reconstructed)

    def test_positioned_sign_fscx_scaling(self):
        """Test auto-scaling horizontal width (\fscx) when sign text expands significantly."""
        prefix = r"{\fnpoxel font\fs22\pos(386.2,503.8)\c&HE9E9EB&}"
        orig_len = len("Toolbox")  # 7 chars
        trans_len = len("Cuadro de herramientas")  # 22 chars

        scaled = ass_parser.scale_sign_fscx(
            prefix, orig_plain_len=orig_len, trans_plain_len=trans_len, style="Signs", name="IDE"
        )
        self.assertIn(r"\fscx", scaled)
        # 7 / 22 * 100 = ~31 -> clamped to min 55
        self.assertIn(r"\fscx55", scaled)

        # Dialogue lines should NOT be scaled
        unscaled = ass_parser.scale_sign_fscx(
            r"{\pos(960,1000)}", orig_plain_len=orig_len, trans_plain_len=trans_len, style="Default"
        )
        self.assertNotIn(r"\fscx", unscaled)

    def test_templater_particles_preserved_nondestructively(self):
        """Test that single-character templater exit/entrance particles remain intact Dialogue lines."""
        raw_ass = """[Script Info]
Title: Song FX Test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: OP TL,Asul,72,&H00FFFFFF,&H64FFFFFF,&H64000000,&H00000000,-1,0,0,0,95,100,0,0,1,2.4,0,2,10,10,32,1
Style: OP Romaji,Asul,72,&H00FFFFFF,&H32F3F3F3,&H64000000,&H00000000,-1,0,0,0,95,100,0,0,1,2.4,0,8,10,10,32,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:01:38.16,0:01:42.12,OP TL,normal,0,0,0,fx,{\\an2\\pos(960,1048)}Everything is sending us this message:
Dialogue: 2,0:01:42.12,0:01:42.62,OP TL,normal,0,0,0,fx,{\\blur2.4\\move(454,876,424,876)}E
Dialogue: 2,0:01:42.12,0:01:42.62,OP TL,normal,0,0,0,fx,{\\blur2.4\\move(486,876,456,876)}v
Dialogue: 3,0:01:38.16,0:01:42.12,OP Romaji,normal,0,0,0,fx,{\\k10}ro{\\k20}ma{\\k30}ji
"""
        import io
        doc = ass_parser.parse_ass_stream(io.StringIO(raw_ass))
        units = ass_parser.prepare_translation_units(doc)

        # Only the main OP TL line is translatable (particles and romaji excluded)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].payload, "Everything is sending us this message:")

        # Apply translation
        translated_map = {1: "Todo nos está enviando este mensaje:"}
        updated_doc = ass_parser.apply_translation_units(doc, units, translated_map)

        # Verify main lyric line is translated Dialogue
        self.assertFalse(updated_doc.events[0].is_comment)
        self.assertIn("Todo nos está enviando este mensaje:", updated_doc.events[0].text)

        # Verify single-character animation particles remain non-destructively intact as Dialogue
        self.assertFalse(updated_doc.events[1].is_comment)
        self.assertFalse(updated_doc.events[2].is_comment)

        # Verify Romaji karaoke is untouched (NOT converted to Comment)
        self.assertFalse(updated_doc.events[3].is_comment)

    def test_ass_line_wrapping_no_double_newline(self):
        """Test that subtrans drain_buf with is_ass=True does not arbitrarily wrap or create \\N\\N."""
        from subtrans import drain_buf, RespBuf, SubtitleLine

        buf = RespBuf(known_seqs={1})
        orig_by_seq = {
            1: SubtitleLine(
                seq=1, time_line="00:00:00,000 --> 00:00:00,000",
                text_lines=[r"I once thought about getting \Na job at a place like this."]
            )
        }
        # Simulate LLM returning translated text with \N
        buf.put(r"[1] una vez pensé en conseguir \N un trabajo en un lugar como este.")
        buf.flush()

        results_ass = drain_buf(buf, orig_by_seq, [1], set(), is_flush=True, is_ass=True)
        self.assertEqual(len(results_ass), 1)
        # Verify exactly one text line containing single \N (no \N\N, no 3 lines)
        self.assertEqual(len(results_ass[0].text_lines), 1)
        self.assertEqual(results_ass[0].text_lines[0], r"una vez pensé en conseguir\Nun trabajo en un lugar como este.")
        self.assertNotIn(r"\N\N", results_ass[0].text_lines[0])


if __name__ == "__main__":
    unittest.main()

