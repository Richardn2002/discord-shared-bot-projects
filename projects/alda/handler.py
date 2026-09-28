# Alda Music — turn Alda text into an audio file.
#
# Commands:
#   !alda <code>          compose with the Alda language, reply with a .wav
#   !alda ```<code>```    the code may be in a code block
#   !alda help            show usage and examples
#
# Examples:
#   !alda piano: c d e f g
#   !alda (tempo 140) piano: o4 c8 d e f g a b > c2
#   !alda piano: c/e/g violin: e f g


import io
import re
import wave

from aldakit import Score
from aldakit.errors import AldaParseError
from aldakit.midi.render import render_pcm
from aldakit.midi.soundfont import ensure_soundfont, find_soundfont


# Refuse anything longer than this: the WAV would be too big to upload.
MAX_WAV_SECONDS = 45

_COMMAND = re.compile(r"^!alda(?:\s|$|`)", re.IGNORECASE)

HELP_FIELDS = [
    {"name": "Usage", "value": "`!alda <code>`  ·  `!alda help`", "inline": False},
    {"name": "Notes", "value": "`c d e f g`", "inline": True},
    {"name": "Octaves", "value": "`o4 c c+ c- < c > c`", "inline": True},
    {"name": "Durations", "value": "`c1 c2 c4 c8 c16`", "inline": True},
    {"name": "Chords", "value": "`c/e/g`", "inline": True},
    {"name": "Parts", "value": "`piano: c d e`\n`violin: e f g`", "inline": True},
    {"name": "Tempo", "value": "`(tempo 140)`", "inline": True},
    {"name": "Ties & bars", "value": "`c~c`  ·  `c4 d | e f`", "inline": True},
    {"name": "Repeat", "value": "`[c d e]*3`", "inline": True},
    {
        "name": "Example",
        "value": "`!alda (tempo 140) piano: o4 c8 d e f g a b > c2`",
        "inline": False,
    },
    {"name": "Full tutorial", "value": "[alda.io/tutorial](https://alda.io/tutorial/)", "inline": False},
]


def _extract_source(text):
    """Pull Alda code out of the command, unwrapping an optional code block."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]  # drop the opening ``` or ```alda line
    out = []
    for line in lines:
        if line.strip().startswith("```"):
            break
        out.append(line)
    return "\n".join(out).strip()


def _get_soundfont():
    """Return a usable SoundFont path, downloading one once if needed."""
    found = find_soundfont()
    if found is not None:
        return found
    log("no soundfont on this machine — downloading TimGM6mb once (~6 MB)")
    return ensure_soundfont("TimGM6mb")


def _wav_bytes(score, soundfont):
    """Synthesize a Score to WAV bytes entirely in memory."""
    pcm, sample_rate, _peak = render_pcm(score.midi, soundfont)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buf.getvalue()


def _clip(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def on_message(message):
    content = message["content"]
    if not _COMMAND.match(content):
        return

    author = message["author"]["name"]
    source = content[5:].strip()

    if not source or source.lower() in ("help", "?", "h"):
        send_embed(
            title="🎵 Alda Music",
            description="I turn **Alda** text into an audio file and attach it here.",
            color=0x9B59B6,
            fields=HELP_FIELDS,
        )
        return

    source = _extract_source(source)
    if not source:
        reply("give me some Alda notes — try `!alda help`.")
        return

    try:
        score = Score(source)
        duration = score.duration          # Score() is lazy; this forces the parse
        note_count = len(score.midi.notes)
    except AldaParseError as exc:
        log(f"parse error: {exc}")
        reply(f"⚠️ I couldn't parse that:\n```\n{_clip(str(exc), 1400)}\n```")
        return
    except Exception as exc:  # noqa: BLE001 - report anything to the user
        log(f"generation failed: {exc!r}")
        reply(f"⚠️ generation failed: `{type(exc).__name__}: {_clip(str(exc), 300)}`")
        return

    if duration > MAX_WAV_SECONDS:
        reply(f"🎼 that piece is **{duration:.0f}s** long; the audio limit is {MAX_WAV_SECONDS}s.")
        return

    wav = _wav_bytes(score, _get_soundfont())
    send_file(f"alda-{author}.wav", wav)
    send_embed(
        title="🎵 done",
        color=0x9B59B6,
        fields=[
            {"name": "Duration", "value": f"{duration:.2f}s", "inline": True},
            {"name": "Notes", "value": str(note_count), "inline": True},
            {"name": "Size", "value": f"{len(wav) / 1024:.0f} KiB", "inline": True},
        ],
    )
    log(f"generated {len(wav)} bytes of WAV for {author}")


def on_failure(event_name, event_data, error):
    log(f"alda tripped while handling {event_name}: {error}")
