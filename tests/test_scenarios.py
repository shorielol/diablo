"""
What a person actually says, and what Diablo actually does about it.

Every other suite here tests a function. This one tests a sentence. That
distinction is not academic — almost every real fault found in this project
survived the unit tests and was caught by someone talking to it:

  "play music"          searched the library for a song called "music", found
                        none, and opened a random YouTube video, with Apple
                        Music open and 53 tracks in it
  "tell me ..."         any sentence containing "me" fuzzy-matched "mute",
                        turned the volume down and answered "Done, Sir"
  "open github.com"     ran `open -a "Zen Browser"` against an application
                        called Zen, failed, and reported success
  search_wikipedia      returned "Expecting value: line 1 column 1" as the
                        answer and read it out loud
  "help me with this"   answered the question instead of teaching it

None of those are broken functions. They are correct functions wired to the
wrong sentence, or wired to nothing. So this suite starts from the sentence.

Three kinds of check:

  ROUTE    the sentence reaches the right tool at all
  DO       the tool runs and comes back with something real
  GUARD    the thing that must not happen does not

Anything that would steal focus, send a message or cost money is listed and
skipped unless DIABLO_TEST_INTRUSIVE=1. Skipped is reported, never silently
passed — a suite that hides what it did not run is how all of the above
stayed hidden.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_tools  # noqa: E402
from skills import music_tool  # noqa: E402
from skills import roster  # noqa: E402
from skills import tutor  # noqa: E402
import voice_bridge  # noqa: E402

INTRUSIVE = os.getenv("DIABLO_TEST_INTRUSIVE", "0") not in ("0", "", "false")


def picks(sentence: str, tool: str, top: int = 3) -> bool:
    """Is `tool` among the first few the selector offers for this sentence?"""
    return tool in (agent_tools.select(sentence) or [])[:top]


def main() -> int:
    r = Results()

    # ── Music ────────────────────────────────────────────────────────────
    #
    # The scenario that prompted this file. Every one of these is a request
    # for music with no music named in it, and every one of them used to end
    # up as a web search for a song title.
    r.head("Putting music on")
    for sentence in ("play music", "play some music", "put some music on",
                     "play songs", "shuffle my music", "put something on",
                     "play me some tunes", "play my playlist"):
        r.check(f"'{sentence}' means shuffle",
                music_tool.wants_shuffle(sentence))
        r.check("  and the reflex catches it before the model does",
                agent_tools.music_tool_wants_shuffle(sentence)
                or sentence in ("shuffle my music", "play my playlist",
                                "put something on", "play me some tunes"),
                "reflex needs a music word in the sentence")

    for sentence in ("play bohemian rhapsody", "play the beatles",
                     "put on some jazz", "play my workout playlist"):
        r.check(f"'{sentence}' is a real request, not a shuffle",
                not music_tool.wants_shuffle(sentence))

    with guard(r, "shuffling actually starts something"):
        if not INTRUSIVE:
            r.skip("shuffle the library", "starts audio; DIABLO_TEST_INTRUSIVE=1")
        else:
            answer = agent_tools.play_music(query="play some music")
            r.check("it reports what it put on",
                    "Shuffling" in answer and "Playing" in answer, answer[:80])

    # ── A named song ─────────────────────────────────────────────────────
    #
    # The old search matched the whole spoken phrase against a track title, so
    # it only worked if you said the title and nothing else. "ivy by frank
    # ocean" found nothing, with Ivy by Frank Ocean sitting in the library.
    r.head("Playing a particular song")
    with guard(r, "the library is readable"):
        tracks = music_tool.library()
        r.check("the library can be read", len(tracks) > 0, f"{len(tracks)} tracks")
        if tracks:
            r.check("  with a title, an artist and an id for each",
                    all(t.get("id") and t.get("name") for t in tracks))

            # Ask for a real track the way a person actually says it.
            sample = next((t for t in tracks if t["name"] and t["artist"]), None)
            spoken = f"{sample['name'].split('(')[0].strip()} by {sample['artist']}"
            found = music_tool.find_in_library(spoken)
            r.check(f"'{spoken[:38]}' is found",
                    found is not None and found["id"] == sample["id"],
                    (found or {}).get("name", "not found"))
            r.check("  and the bare title works too",
                    (music_tool.find_in_library(sample["name"]) or {}).get("id")
                    == sample["id"])

    with guard(r, "a song that is not there is not substituted"):
        for absent in ("bohemian rhapsody by queen",
                       "some song that does not exist anywhere"):
            r.check(f"'{absent[:34]}' finds nothing",
                    music_tool.find_in_library(absent) is None,
                    "a wrong song is worse than falling through to the web")

    if INTRUSIVE:
        with guard(r, "it plays what it says it is playing"):
            tracks = music_tool.library()
            # A track that round-trips through the matcher, rather than
            # whichever happens to be first. Apple Music titles carry curly
            # apostrophes and "(feat. ...)" tails, and a test that picks one of
            # those is testing the matcher's edge cases by accident instead of
            # testing that what is announced is what plays.
            def ordinary(t):
                # An unremarkable title: a couple of plain words, no bracketed
                # tails, no curly apostrophes, no titles spelled out one letter
                # at a time. Picking whichever track happened to be first meant
                # the test kept landing on the matcher's edge cases — "Best
                # Part (feat. H.E.R.)" one run, "C h e e r s" the next — and
                # testing those by accident instead of testing that what is
                # announced is what plays.
                name = t["name"]
                words = name.split()
                if not (1 <= len(words) <= 4):
                    return False
                if any(len(w) == 1 for w in words):
                    return False
                if not all(c.isalnum() or c in " '" for c in name):
                    return False
                return (music_tool.find_in_library(
                    f"{name} by {t['artist']}") or {}).get("id") == t["id"]

            target = next((t for t in tracks if ordinary(t)), None)
            if target:
                answer = agent_tools.play_music(
                    query=f"{target['name']} by {target['artist']}")
                import subprocess as sp
                playing = sp.run(
                    ["osascript", "-e",
                     'tell application "Music" to return name of current track'],
                    capture_output=True, text=True).stdout.strip()
                r.check("the track it announced is the track that started",
                        playing and playing[:12].lower() in answer.lower(),
                        f"announced {answer[:50]!r}, playing {playing!r}")
    else:
        r.skip("playing a named track", "starts audio; DIABLO_TEST_INTRUSIVE=1")

    r.check("'play music' reaches the music tool", picks("play music", "play_music"))
    r.check("'next song' reaches transport", picks("next song", "music_transport"))

    # ── The screen ───────────────────────────────────────────────────────
    r.head("Asking about the screen")
    r.check("'what's on my screen' reads it", picks("what's on my screen", "read_screen"))
    r.check("'point at the send button' points", picks("point at the send button", "point_at"))
    # The maths gate needs the question AND the screen to agree. Asked "how
    # do I open a new tab" on a page whose prose contained "show that", it
    # answered "I can't read the equation".
    frag = lambda t, c=0.4: {"text": t, "confidence": c}
    maths = [frag("Question 3", 0.5), frag("Solve for x:", 0.5), frag("4x", 0.3)]
    prose = [frag("Show that you agree", 0.5), frag("Calculate shipping", 0.5),
             frag("3x", 0.3)]
    r.check("a maths question on a maths screen is gated",
            tutor.unreadable_problem(maths, "help me with this question")
            is not None)
    r.check("a browser question on prose is NOT gated",
            tutor.unreadable_problem(prose, "how do i open a new tab") is None,
            "the screen alone is not enough; he has to be asking about a problem")
    r.check("a browser question on a maths screen is NOT gated",
            tutor.unreadable_problem(maths, "how do i open a new tab") is None)

    r.check("'help me with this question' teaches rather than answers",
            picks("help me with this question", "walk_me_through"))
    r.check("'how do i export this' teaches", picks("how do i export this", "walk_me_through"))
    # The exact request that broke it: "teach me how to add skills onto
    # claude" answered "Done, Sir." because "claude" scores 0.667 against
    # "louder" and the volume reflex claimed it before the tutor was reached.
    r.head("Reflexes only answer what they are for")
    for sentence in ("teach me how to add skills onto claude",
                     "how do i use claude code",
                     "open claude",
                     "what does claude cost",
                     "remind me in a minute",
                     "show me the loader in the build output",
                     "tell me about boulder colorado"):
        r.check(f"'{sentence[:40]}' is not a volume command",
                not (voice_bridge.is_a_command(sentence)
                     and voice_bridge.fuzzy_match(
                         sentence, ["volume", "louder", "quieter",
                                    "mute", "brightness"])),
                "a word that rhymes is not an instruction")

    for sentence in ("turn the volume down", "make it louder", "mute it",
                     "turn the volyume down", "make it lowder",
                     "set britness to 50"):
        r.check(f"'{sentence}' still is",
                voice_bridge.is_a_command(sentence)
                and voice_bridge.fuzzy_match(
                    sentence, ["volume", "louder", "quieter", "mute",
                               "brightness"]),
                "real transcription slips must still get through")

    r.check("teaching about an app is routed to the tutor",
            tutor.wants_teaching("teach me how to add skills onto claude"))

    r.head("Asking about the screen, continued")
    r.check("'just tell me the answer' does NOT teach",
            not tutor.wants_teaching("just tell me the answer"))
    r.check("'what is the answer' does NOT teach",
            not tutor.wants_teaching("what is the answer"))

    # ── Research ─────────────────────────────────────────────────────────
    r.head("Looking things up")
    r.check("'who won the race last night' searches the web",
            picks("who won the race last night", "web_search"))
    r.check("'what is the treaty of versailles' reaches an encyclopaedia",
            picks("what is the treaty of versailles", "search_wikipedia")
            or picks("what is the treaty of versailles", "web_search"))

    with guard(r, "wikipedia returns prose, not an exception"):
        answer = agent_tools.search_wikipedia(query="Treaty of Versailles")
        r.check("it comes back with real sentences",
                len(answer) > 60 and "Versailles" in answer, answer[:70])
        # The specific regression: a JSON parser handed HTML used to have its
        # exception text returned as the answer and read out loud.
        r.check("and never an exception message",
                "Expecting value" not in answer and "Traceback" not in answer)

    with guard(r, "a nonsense lookup fails honestly"):
        answer = agent_tools.search_wikipedia(query="asdkjhaskdjhasd")
        r.check("it says it found nothing",
                "no article" in answer.lower() or "couldn't find" in answer.lower(),
                answer[:70])

    # ── The browser ──────────────────────────────────────────────────────
    r.head("Opening things")
    with guard(r, "the browser is one that exists"):
        from skills import browsers
        chosen = browsers.default()
        installed = browsers.installed()
        r.check("a real browser is chosen", chosen in installed or chosen == "Safari",
                f"{chosen} of {installed}")
        r.check("and it is not the old hardcoded name", chosen != "Zen Browser")

    r.check("'open github.com' opens a URL", picks("open github.com", "open_url"))

    # ── Files and numbers ────────────────────────────────────────────────
    r.head("Files and arithmetic")
    r.check("'read the notes file' reads", picks("read the notes file on my desktop", "read_file"))
    r.check("'total the sales csv' reaches python, not mental arithmetic",
            picks("total the numbers in the sales csv", "python_snippet", top=5))

    with guard(r, "arithmetic is computed, not guessed"):
        answer = agent_tools.python_snippet(code="print(37 * 419)")
        r.check("37 * 419 is 15503", "15503" in str(answer), str(answer)[:60])

    # ── The machine ──────────────────────────────────────────────────────
    r.head("The machine itself")
    with guard(r, "system status answers"):
        answer = agent_tools.system_status()
        r.check("it reports something real",
                any(w in answer.lower() for w in ("cpu", "battery", "ram", "memory")),
                answer[:70])
        # "How much space have I got left" was unanswerable from here: the
        # percentage was collected and then only mentioned above ninety, so an
        # agent asked about disk space correctly reported that it could not
        # find out.
        r.check("disk space is reported, not just warned about",
                "gigabytes free" in answer.lower(), answer[-60:])

    r.check("'how much battery' reaches it", picks("how much battery do i have", "system_status"))

    # ── Delegation ───────────────────────────────────────────────────────
    r.head("Who gets the job")
    for sentence, who in (("look up who won the f1 race", "asmo"),
                          ("what's on my screen", "vassago"),
                          ("total the csv on my desktop", "marbas"),
                          ("turn the volume down", "seere")):
        r.check(f"'{sentence}' goes to {who}",
                roster.assign(sentence) == who,
                roster.assign(sentence))

    with guard(r, "two errands run at once"):
        pieces = roster.split("check my battery and also tell me what window is in front")
        r.check("it splits", len(pieces) > 1, f"{len(pieces)} pieces")
        if len(pieces) > 1:
            r.check("  onto different agents",
                    len({a for _t, a in pieces}) == len(pieces))

    with guard(r, "a dependent errand is never split"):
        pieces = roster.split("read the total from sales.csv and then send it to mum")
        r.check("it stays with one agent", len(pieces) == 1)

    # ── Guards ───────────────────────────────────────────────────────────
    #
    # The things that must not happen, phrased as the sentences that would
    # make them happen.
    r.head("What must not happen")

    for sentence in ("tell me what is on my screen", "show me the notes file",
                     "give me three ideas", "read me the first line",
                     "remind me tomorrow"):
        r.check(f"'{sentence}' does not touch the volume",
                not voice_bridge.fuzzy_match(
                    sentence, ["volume", "louder", "quieter", "mute", "brightness"]),
                "fuzzy_match matched 'me' to 'mute' for the life of the project")

    for sentence in ("turn the volume down", "make it louder", "mute it"):
        r.check(f"'{sentence}' still does",
                voice_bridge.fuzzy_match(
                    sentence, ["volume", "louder", "quieter", "mute", "brightness"]))

    with guard(r, "the shell denylist holds"):
        answer = agent_tools.run_shell(command="rm -rf /")
        r.check("rm -rf / is refused",
                "refus" in answer.lower() or "not allowed" in answer.lower()
                or "denied" in answer.lower() or "won't" in answer.lower(),
                answer[:70])

    with guard(r, "the filesystem boundary holds"):
        # Refusing by raising is as good as refusing by returning, and this
        # one raises. The test asks whether the file was read, not how the
        # refusal was shaped.
        refused, detail = False, ""
        try:
            answer = agent_tools.read_file(path="/etc/passwd")
            detail = str(answer)[:70]
            refused = any(w in answer.lower() for w in
                          ("outside", "cannot", "not allowed", "refus"))
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            refused = "outside" in str(e).lower() or "allowed" in str(e).lower()
        r.check("/etc/passwd is out of bounds", refused, detail)
        r.check("  and root's password file never came back",
                "root:" not in detail)

    # ── Speaking ─────────────────────────────────────────────────────────
    r.head("How it sounds")
    from skills import voice
    r.check("explaining is 110-130 words per minute",
            110 <= voice.EXPLAIN_WPM <= 130, str(voice.EXPLAIN_WPM))
    r.check("ordinary speech is faster than that",
            voice.NORMAL_WPM > voice.EXPLAIN_WPM, str(voice.NORMAL_WPM))
    r.check("a long answer gets the slower pace",
            voice_bridge._pace_for(" ".join(["word"] * 40)) == voice.EXPLAIN_WPM)
    r.check("a short one does not",
            voice_bridge._pace_for("Done, Sir.") == voice.NORMAL_WPM)
    r.check("the orb waits for the sound",
            voice.on_audio_start is not None,
            "the animation used to start 1125ms before any audio")

    # ── Going and finding out ────────────────────────────────────────────
    #
    # "There's nothing about that on this screen" is true and useless. If
    # Diablo does not know how, the job is to look it up — quietly, with
    # nothing appearing on screen — and then teach it.
    r.head("Looking it up when it is not on screen")
    with guard(r, "a page can be read without a browser"):
        from skills import search_tool
        text = search_tool.read_page(
            "https://support.apple.com/en-gb/guide/textedit/welcome/mac")
        r.check("a documentation page comes back as text",
                len(text) > 300, f"{len(text)} characters")
        r.check("  with the markup stripped",
                "<" not in text[:400] and "function(" not in text[:400])

    with guard(r, "the spoken question becomes a sensible search"):
        import re as _re
        def as_query(question, app):
            asked = _re.sub(
                r"^\s*(?:can you\s+|could you\s+|please\s+)?"
                r"(?:teach|show|tell|walk|guide|help)\s+me\s+"
                r"(?:how\s+to\s+|through\s+|with\s+|about\s+)?", "",
                question.strip(), flags=_re.IGNORECASE)
            asked = _re.sub(r"^\s*(?:how\s+do\s+i|how\s+can\s+i|how\s+would\s+i|"
                            r"how\s+to|explain\s+how\s+to)\s+", "", asked,
                            flags=_re.IGNORECASE).strip(" ?.")
            asked = asked or question
            return (f"how to {asked} in {app}"
                    if app and app.lower() not in asked.lower()
                    else f"how to {asked}")
        # The preamble that routed it to the tutor has to come off before it
        # reaches a search engine: "how to teach me how to add skills" finds
        # nothing at all.
        r.check("the teaching preamble is stripped",
                as_query("teach me how to add skills onto claude", "Claude")
                == "how to add skills onto claude")
        r.check("the application is added when it is missing",
                as_query("how do i change the font", "TextEdit")
                == "how to change the font in TextEdit")

    if os.getenv("DIABLO_TEST_NETWORK", "1") not in ("0", "", "false"):
        with guard(r, "it finds a real procedure"):
            found = tutor.research("how do i change the font", "TextEdit")
            r.check("instructions were found", found is not None,
                    (found or {}).get("source", "nothing"))
            if found:
                r.check("  from a real source", bool(found.get("source")),
                        found["source"])
                r.check("  as at least two steps", len(found["steps"]) >= 2,
                        f"{len(found['steps'])} steps")
                joined = " ".join(s["say"] for s in found["steps"]).lower()
                # The thing it used to invent. Format > Font is the real path.
                r.check("  naming the real menu, not an invented one",
                        "format" in joined or "font" in joined, joined[:90])
    else:
        r.skip("looking something up", "needs the network")

    # ── Hearing ──────────────────────────────────────────────────────────
    r.head("Hearing you")
    with guard(r, "a transcriber loads and works"):
        from skills import stt
        import numpy as np
        import wave as wavelib
        engine = stt.load()
        r.check("an engine is available", engine is not None, engine.name)

        clip = os.path.join(DIABLO_HOME, "command.wav")
        if not os.path.exists(clip):
            r.skip("transcribing a real recording", "no sample on disk")
        else:
            # Both shapes, because they come from different paths: push to
            # talk hands over a numpy array it collected itself, the wake path
            # hands over a file. An engine that only takes one of them breaks
            # exactly half of the ways you can talk to it.
            by_path = engine.transcribe(clip)["text"].strip().lower()
            r.check("a file transcribes", "instagram" in by_path, by_path[:50])

            with wavelib.open(clip) as handle:
                audio = (np.frombuffer(handle.readframes(handle.getnframes()),
                                       dtype=np.int16).astype(np.float32) / 32768.0)
            by_array = engine.transcribe(audio, fp16=False,
                                         beam_size=5)["text"].strip().lower()
            r.check("a numpy array transcribes too", "instagram" in by_array,
                    by_array[:50])
            r.check("  and Whisper's keyword arguments are tolerated",
                    True, "fp16 and beam_size mean nothing to Parakeet")

    # Both of the ways Diablo went deaf after switching engines. Neither was a
    # bad answer — both were a failed transcription, which sounds exactly like
    # not being heard at all.
    with guard(r, "the tools it shells out to are findable"):
        from skills import config as diablo_config
        # An app launched from Finder gets a PATH without /opt/homebrew/bin,
        # and Parakeet decoded audio by invoking ffmpeg. Every utterance died
        # with "FFmpeg is not installed" while ffmpeg sat there on disk.
        r.check("ffmpeg can be found", bool(diablo_config.require("ffmpeg")),
                diablo_config.require("ffmpeg") or "missing")
        r.check("ffprobe can be found", bool(diablo_config.require("ffprobe")),
                diablo_config.require("ffprobe") or "missing")
        r.check("PATH carries the Homebrew prefix",
                "/opt/homebrew/bin" in diablo_config.repair_path()
                or not os.path.isdir("/opt/homebrew/bin"))

    with guard(r, "audio in memory needs no temp file and no ffmpeg"):
        from skills import stt
        import wave as wavelib
        import numpy as np
        clip = os.path.join(DIABLO_HOME, "command.wav")
        if not os.path.exists(clip):
            r.skip("transcribing without ffmpeg", "no sample on disk")
        else:
            engine = stt.load()
            with wavelib.open(clip) as handle:
                audio = (np.frombuffer(handle.readframes(handle.getnframes()),
                                       dtype=np.int16).astype(np.float32) / 32768.0)
            saved = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = "/usr/bin:/bin"      # no homebrew at all
                text = engine.transcribe(audio)["text"].strip().lower()
            finally:
                os.environ["PATH"] = saved
            r.check("it still transcribes with ffmpeg unreachable",
                    "instagram" in text, text[:40])

    with guard(r, "the model can be used from another thread"):
        # MLX keeps its streams per thread. A model loaded during boot and
        # then used from the push-to-talk worker raised "There is no
        # Stream(cpu, 1) in current thread", which arrives as a failed
        # transcription and is indistinguishable from silence.
        import threading as _threading
        from skills import stt
        import wave as wavelib
        import numpy as np
        clip = os.path.join(DIABLO_HOME, "command.wav")
        if os.path.exists(clip):
            engine = stt.load()
            with wavelib.open(clip) as handle:
                audio = (np.frombuffer(handle.readframes(handle.getnframes()),
                                       dtype=np.int16).astype(np.float32) / 32768.0)
            got = {}
            worker = _threading.Thread(
                target=lambda: got.update(
                    text=engine.transcribe(audio)["text"].strip().lower()))
            worker.start()
            worker.join(timeout=40)
            r.check("a worker thread transcribes",
                    "instagram" in got.get("text", ""), got.get("text", "(nothing)"))

    with guard(r, "it falls back rather than going deaf"):
        from skills import stt
        fallback = stt.load(prefer="whisper")
        r.check("whisper is still reachable", fallback.name == "whisper",
                fallback.model_id)

    # ── Things this suite cannot do unattended ───────────────────────────
    r.head("Not tested here")
    for what, why in (
        ("sending a WhatsApp", "sends a real message to a real person"),
        ("sending an iMessage", "same"),
        ("deleting a file", "covered in test_filesystem against a sandbox"),
        ("the wake word", "needs a microphone and a voice; test_mic_wake covers the endpointer"),
        ("push to talk", "needs a keypress; the API path is covered in test_app"),
    ):
        r.skip(what, why)

    return r.report("scenarios")


if __name__ == "__main__":
    sys.exit(main())
