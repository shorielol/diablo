#!/usr/bin/env python3
"""
Builds the Diablo decision dataset: every example labelled by Diablo's gold
policy, never by a model.

    python research/laya_diablo/dataset/build_dataset.py --version v1

Writes dataset/<version>/{train,validation,calibration,test}.jsonl, the
hand-written test_private.jsonl, manifest.json (hashes, counts, policy) and
audit.json (the quality checks). Deterministic: the same version and seed
give byte-identical files.

THE GOLD POLICY (first clause that applies wins)

  P1  cut off mid-thought                        -> unknown, complete=false
  P2  asks about what is on screen, under the
      cursor, or points with this/that/these at
      something only the screen shows            -> screen, needs_screen
  P3  asks where something is on screen, to be
      shown on screen, or how to do something in
      an app (a procedure)                       -> guide, needs_screen
  P4  asks Diablo to do something to the
      computer, a file, an app, media, settings
      or a message (imperative or "can you ...") -> do
  P5  a multi-step job: research, plan, draft,
      compare, organise, write a report          -> mission
  P6  needs live data, the user's own data, or
      what was said earlier                      -> complex
  P7  points at something with nothing to point
      at, or tells Diablo NOT to act             -> unknown
  P8  otherwise conversation: explanation (explain,
      why, how does, describe), factual (what,
      who, when, define), casual (greetings,
      feelings, thanks, opinions, advice that
      needs no screen)                           -> casual/factual/explanation

  Context decides when words alone cannot: "show me how the subsidy works"
  is P3 with a subsidy diagram in front and P8 without; "open it" is P4 when
  the history names what "it" is and P7 when it does not.

This policy is Diablo's, written for this dataset. The deterministic Rules
engine is conservative against it (it defers where the policy says
conversation), and the dataset records the Rules verdict for every example so
the difference can be measured instead of assumed.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = json.load(open(os.path.join(HERE, "..", "contract.json")))
INTENTS = list(CONTRACT["questions"]["intent"]["criteria"])
SEED = 20260927
SPLITS = ("train", "validation", "calibration", "test")

CONVERSATION = ("casual", "factual", "explanation")


def lane(intent, needs_screen, complete):
    if intent in CONVERSATION and not needs_screen and complete:
        return "fast"
    return "agent" if intent in ("screen", "guide", "do", "mission") else "full"


# ─── Slots ───────────────────────────────────────────────────────────────────

CONCEPTS = [
    "GDP", "inflation", "a monopoly", "photosynthesis", "compound interest", "opportunity cost",
    "a black hole", "DNA", "elasticity of demand", "a recession", "supply and demand",
    "a tariff", "the Pythagorean theorem", "an eclipse", "democracy", "machine learning",
    "a neural network", "the immune system", "osmosis", "gravity", "entropy", "a prime number",
    "market failure", "a price ceiling", "a price floor", "an externality",
    "comparative advantage", "fiscal policy", "monetary policy", "the water cycle",
    "plate tectonics", "an atom", "the speed of light", "a sonnet", "irony",
    "Newton's third law", "the scientific method", "an algorithm", "recursion", "blockchain",
    "a derivative in calculus", "standard deviation", "a hypothesis", "the Renaissance",
    "the Cold War", "the Industrial Revolution", "biodiversity", "an ecosystem",
    "globalisation", "a trade deficit", "unemployment", "the multiplier effect",
    "a public good", "moral hazard", "game theory", "the prisoner's dilemma",
    "diminishing returns", "economies of scale", "a cartel", "price discrimination",
    "a merit good", "the Gini coefficient", "a production possibility frontier",
    "quantitative easing", "a central bank", "interest rates", "a bond", "dividends",
    "a mortgage", "depreciation", "a balance sheet", "cash flow", "a franchise",
    "the circular flow of income", "aggregate demand", "the Phillips curve", "stagflation",
    "a carbon tax", "cap and trade", "renewable energy", "the greenhouse effect",
    "natural selection", "a gene", "an enzyme", "the nervous system", "a vaccine",
    "an antibiotic", "a virus", "the periodic table", "a chemical bond", "an isotope",
    "kinetic energy", "momentum", "electromagnetism", "a transistor", "binary code",
    "encryption", "an API", "cloud computing", "a metaphor", "a thesis statement",
    "epistemology", "confirmation bias", "the placebo effect", "utilitarianism",
    "the social contract", "the separation of powers", "the Magna Carta", "feudalism",
    "the Silk Road", "the Ottoman Empire", "the printing press",
]
WHYS = [
    "is the sky blue", "do we dream", "does ice float", "do leaves change colour",
    "do prices rise over time", "do firms merge", "does the moon have phases",
    "do we have seasons", "do planes stay in the air", "is the ocean salty",
    "do countries trade", "do banks charge interest", "does inflation hurt savers",
    "do monopolies raise prices", "does the economy go through cycles", "do cats purr",
    "do we yawn", "does bread rise", "do magnets attract iron", "does hot air rise",
    "do stars twinkle", "is exercise good for the brain", "do volcanoes erupt",
    "do tariffs raise prices", "does demand fall when prices rise",
]
HOW_WORKS = [
    "compound interest", "a vaccine", "the stock market in general", "a refrigerator",
    "the internet", "a solar panel", "memory in the brain", "a jet engine", "photosynthesis",
    "a credit score", "an election", "insurance", "a microwave", "the heart",
    "public key encryption", "a search engine in principle", "evolution", "a battery",
]
FACT_QS = [
    "who wrote Pride and Prejudice", "who painted the Mona Lisa", "what's the capital of Australia",
    "what is the capital of Canada", "how many bones are in the human body",
    "how far away is the moon", "how old is the universe", "which planet is the largest",
    "when did the Second World War end", "who invented the telephone",
    "what's the boiling point of water", "how many continents are there",
    "who discovered penicillin", "what language do they speak in Brazil",
    "how many players are on a football team", "what is the largest ocean",
    "who was the first person on the moon", "what's the chemical symbol for gold",
    "how many sides does a hexagon have", "what's the tallest mountain in the world",
    "who wrote Hamlet", "when was the printing press invented", "what is the square root of 144",
    "how many hours are in a week", "what's the longest river in Africa",
    "who developed the theory of relativity", "what is the freezing point of water in Fahrenheit",
    "how many strings does a violin have", "what's the smallest prime number",
    "which gas do plants absorb", "what currency does Japan use", "who composed the Four Seasons",
]
OPINIONS = [
    "the economy", "remote work", "space exploration", "social media", "electric cars",
    "universal basic income", "homework", "artificial intelligence", "city life",
    "learning languages", "classical music", "minimalism", "coffee", "chess",
]
GREETINGS = [
    "hello", "hi", "hey", "hiya", "good morning", "good afternoon", "good evening",
    "morning", "evening", "yo", "hello there", "hey there",
]
CASUAL_LINES = [
    "how are you", "how's it going", "how are you doing", "what's up", "how's your day",
    "thanks", "thank you", "thank you so much", "cheers", "nice one", "brilliant",
    "that's great", "that's interesting", "that's funny", "fair enough", "I'm knackered",
    "I'm tired", "long day", "I'm bored", "I'm back", "I'm fine", "tell me a joke",
    "say something funny", "good night", "you're the best", "I appreciate it",
    "that made my day", "haha", "no worries", "sounds good",
]
ADVICE = [
    "should I take a break", "is it worth learning Python", "should I study in the morning or at night",
    "is coffee bad for you", "should I click save before closing", "would sending the email now be a good idea",
    "is it a good idea to back up my files", "should I learn French or Spanish",
    "is it better to rent or buy", "should I start revising early",
]
OBJECTS = [
    "graph", "chart", "error", "page", "paragraph", "code", "email", "diagram", "slide",
    "table", "article", "message", "form", "equation", "spreadsheet", "sentence", "warning",
    "screenshot", "figure", "function",
]
CONTROLS = [
    "File", "the File menu", "the export button", "the save button", "settings", "the share icon",
    "the search bar", "the bold button", "zoom", "the print option", "the Wi-Fi icon", "bookmarks",
    "the new tab button", "downloads", "the font menu", "undo", "the play button",
    "the volume slider", "the sidebar", "the insert menu", "the chart button", "Edit",
    "the View menu", "preferences", "the close button", "the refresh button", "the send button",
    "the attach button", "the comment button", "the filter option",
]
TASKS = [
    "export this as a PDF", "change the font size", "add a bookmark", "rename a file",
    "insert a table", "share a document", "turn on dark mode", "split the screen",
    "find a word on the page", "mute a tab", "crop an image", "add a slide", "merge cells",
    "change my wallpaper", "add a signature", "make a new folder", "zoom in",
    "print this page", "undo that", "change the language", "add a chart", "sort a column",
]
APPS = ["Chrome", "Safari", "Preview", "Notes", "Keynote", "Word", "Excel", "Finder",
        "Spotify", "Mail", "Zen", "Pages", "Numbers", "Terminal", "VS Code", "Slack"]
REGIONS = [
    "welfare loss", "consumer surplus", "producer surplus", "the equilibrium", "the deadweight loss",
    "the demand curve", "the supply curve", "the subsidy area", "the tax revenue",
    "the price floor line", "the shortage", "the surplus", "the new equilibrium",
]
VISUAL_PROCESSES = [
    "the subsidy causes welfare loss", "the tax shifts supply", "the price ceiling creates a shortage",
    "the equilibrium moves", "consumer surplus shrinks",
]
CONTACTS = ["Mum", "Dad", "Alex", "Priya", "Sam", "my teacher", "Jordan", "Aarav", "Maya"]
MESSAGES = ["I'll be late", "call me back", "running ten minutes behind", "see you at six",
            "the meeting moved", "thanks for today"]
FILES = ["notes.txt", "the budget spreadsheet", "report.docx", "my essay draft", "photo.jpg",
         "the old drafts", "invoice.pdf", "the slides"]
FOLDERS = ["Downloads", "Documents", "Desktop", "the Econ EE folder", "Screenshots", "Projects"]
MUSIC = ["some music", "some jazz", "my focus playlist", "lo-fi beats", "the next song",
         "something upbeat"]
SUBJECTS = ["this company", "electric vehicle startups", "the best laptops under a thousand pounds",
            "market failure for my EE", "coffee chains in Pune", "competitors to Indura",
            "the history of the printing press", "remote work trends"]
VENTURES = ["Brew and Critics", "Indura", "the Indura launch", "my startup", "the café"]
PLACES = ["Tokyo", "Delhi", "London", "New York", "Pune", "Sydney"]
WHEN = ["today", "tomorrow", "this weekend", "tonight", "next week"]
ASSETS = ["Bitcoin", "Apple stock", "gold", "the rupee", "Tesla shares"]


# ─── Noise: how speech-to-text actually hands text over ──────────────────────

FILLERS = ["um", "uh", "so", "okay", "right", "erm"]
ADDRESSES = ["hey diablo", "diablo", "ok diablo"]
TAILS = ["please", "for me", "sir", "real quick", "thanks"]


def sentence(text, question=False):
    text = text[0].upper() + text[1:]
    return text + ("?" if question else ".")


CONTRACTIONS = [("what is", "what's"), ("where is", "where's"), ("do not", "don't"),
                ("I am", "I'm"), ("how is", "how's"), ("that is", "that's"), ("can you", "can u")]


def noisy(rng, text, question):
    """
    One speech-like rendering of a clean request. Always changes the words, so
    it is a new example and not the same one in a different case.
    """
    words = text.split()
    style = rng.randrange(6)
    if style == 0:
        out = f"{rng.choice(FILLERS)} {text}"
    elif style == 1:
        out = f"{rng.choice(ADDRESSES)} {text}"
    elif style == 2:
        out = f"{text} {rng.choice(TAILS)}"
    elif style == 3 and len(words) > 1:
        i = rng.randrange(len(words))
        out = " ".join(words[:i + 1] + [words[i]] + words[i + 1:])     # "open open chrome"
    elif style == 4:
        swapped = text
        for a, b in CONTRACTIONS:
            if a in swapped.lower():
                swapped = re.sub(re.escape(a), b, swapped, count=1, flags=re.I)
                break
            if b in swapped:
                swapped = swapped.replace(b, a, 1)
                break
        out = swapped if swapped != text else f"{rng.choice(FILLERS)} {text}"
    else:
        out = f"{rng.choice(FILLERS)}, {rng.choice(ADDRESSES)}, {text}"
    if rng.random() < 0.5:
        return out.lower()                      # no casing, no punctuation
    return sentence(out, question)


# ─── Families ────────────────────────────────────────────────────────────────
#
# A family is a set of template frames sharing a gold label. The unit that is
# assigned to a split is one frame (a template), so the test split holds
# phrasings that train never saw; every rendering of a line stays with it.

FAMILIES = []


def G(intent, screen=False, complete=True):
    return {"intent": intent, "needs_screen": screen, "complete": complete}


def spec(name, category, clause, frames, slots, gold, question=True, sample=None,
         difficulty="normal", safety="none", phenomenon="plain"):
    """A family written as data: every frame filled with (a sample of) the slots."""
    def fn(rng):
        for f, frame in enumerate(frames):
            chosen = slots if (sample is None or len(slots) <= sample) else rng.sample(slots, sample)
            for slot in chosen:
                yield f, frame.format(x=slot, bare=str(slot).replace("the ", "")), question, gold
    FAMILIES.append(dict(name=name, category=category, clause=clause, difficulty=difficulty,
                         safety=safety, phenomenon=phenomenon, fn=fn))


def lines(name, category, clause, texts, gold, difficulty="normal", safety="none",
          phenomenon="plain"):
    """A family of whole lines: each line is its own unit."""
    def fn(rng):
        for i, t in enumerate(texts):
            yield i, t, t.split()[0].lower() in QUESTION_WORDS, gold
    FAMILIES.append(dict(name=name, category=category, clause=clause, difficulty=difficulty,
                         safety=safety, phenomenon=phenomenon, fn=fn))


def custom(name, category, clause, difficulty="hard", safety="none", phenomenon="plain"):
    def wrap(fn):
        FAMILIES.append(dict(name=name, category=category, clause=clause, difficulty=difficulty,
                             safety=safety, phenomenon=phenomenon, fn=fn))
        return fn
    return wrap


QUESTION_WORDS = {"what", "what's", "whats", "who", "why", "how", "how's", "when", "where",
                  "where's", "which", "is", "are", "do", "does", "can", "could", "should",
                  "would", "will", "did", "any"}

# P8 conversation
spec("fact.what_is", "factual", "P8",
     ["what is {x}", "what's {x}", "whats {x} again", "what exactly is {x}", "so what is {x}"],
     CONCEPTS, G("factual"), sample=32)
spec("fact.define", "factual", "P8",
     ["define {x}", "give me a definition of {x}", "what does {x} mean", "what's meant by {x}",
      "definition of {x}"], CONCEPTS, G("factual"), question=False, sample=26)
lines("fact.trivia", "factual", "P8", FACT_QS, G("factual"))
spec("fact.trivia_polite", "factual", "P8",
     ["do you know {x}", "quick one, {x}", "tell me {x}", "any idea {x}"], FACT_QS, G("factual"),
     sample=14, difficulty="hard")
spec("explain.concept", "explanation", "P8",
     ["explain {x}", "explain {x} simply", "can you explain {x}", "explain {x} to me like I'm twelve",
      "could you explain {x} in a sentence"], CONCEPTS, G("explanation"), question=False, sample=26)
spec("explain.why", "explanation", "P8",
     ["why {x}", "so why {x}", "any idea why {x}"], WHYS, G("explanation"), sample=16)
spec("explain.how_works", "explanation", "P8",
     ["how does {x} work", "describe how {x} works", "how exactly does {x} work"], HOW_WORKS,
     G("explanation"), sample=12)
spec("explain.tell_about", "explanation", "P8",
     ["tell me about {x}", "talk me through {x}", "what's the deal with {x}", "break down {x} for me",
      "give me the short version of {x}"], CONCEPTS, G("explanation"), question=False, sample=20,
     difficulty="hard")
spec("casual.greeting", "casual", "P8", ["{x}", "{x} diablo", "{x} sir", "oh {x}"], GREETINGS,
     G("casual"), question=False)
lines("casual.smalltalk", "casual", "P8", CASUAL_LINES, G("casual"))
spec("casual.opinion", "casual", "P8",
     ["what do you think about {x}", "what's your take on {x}", "how do you feel about {x}",
      "thoughts on {x}"], OPINIONS, G("casual"), difficulty="hard")
lines("casual.advice", "casual", "P8", ADVICE, G("casual"), difficulty="hard", safety="advice")

# P2 screen
spec("screen.explain_this", "screen", "P2",
     ["explain this {x}", "what does this {x} mean", "explain the {x} on my screen",
      "what's this {x} about", "can you explain this {x}", "what's going on in this {x}"],
     OBJECTS, G("screen", True), question=False, sample=12, phenomenon="hard_negative")
lines("screen.looking", "screen", "P2",
      ["what am I looking at", "what's on my screen", "what's this", "what does that say",
       "read this to me", "what does it say here", "can you see this", "look at this",
       "what's going on here", "did that work", "what changed", "is this right",
       "what's this bit", "what does the screen say", "what's that", "read that out"],
      G("screen", True))
lines("screen.cursor", "screen", "P2",
      ["what am I pointing at", "what's under my cursor", "what's this I'm hovering over",
       "what is my mouse on", "what's the thing under the pointer", "explain what I'm pointing at",
       "what does this button do", "what's this icon", "what am I hovering over"],
      G("screen", True), difficulty="hard")
spec("screen.object_action", "screen", "P2",
     ["summarise this {x}", "is this {x} right", "check this {x} for me", "can you read this {x}",
      "proofread this {x}"], OBJECTS, G("screen", True), question=False, sample=10,
     difficulty="hard")
spec("screen.in_app", "screen", "P2",
     ["explain the graph in {x}", "what's the error in {x}", "read the page in {x}",
      "what does the chart in {x} show"], APPS, G("screen", True), sample=8, difficulty="hard")
lines("screen.advice_deictic", "screen", "P2",
      ["should I send this", "would sending this be a good idea", "is this safe to click",
       "should I accept this", "is this email a scam", "should I delete this", "is this link legit"],
      G("screen", True), difficulty="hard", safety="advice")

# P3 guide
spec("guide.where", "guide", "P3",
     ["where is {x}", "where's {x}", "show me where {x} is", "where's the {bare} thing",
      "where did {x} go", "I can't find {x}"], CONTROLS, G("guide", True), sample=14,
     phenomenon="hard_negative")
spec("guide.point", "guide", "P3",
     ["point to {x}", "point out {x}", "highlight {x}", "find {x} for me on screen"], CONTROLS,
     G("guide", True), question=False, sample=12)
spec("guide.how_do_i", "guide", "P3",
     ["how do I {x}", "how can I {x}", "show me how to {x}", "how would I {x} here",
      "what's the way to {x}"], TASKS, G("guide", True), sample=12, phenomenon="hard_negative")
spec("guide.region", "guide", "P3",
     ["where is {x}", "show me {x}", "where's {x} on this graph", "highlight {x}", "point to {x}",
      "which part is {x}"], REGIONS, G("guide", True), sample=9, difficulty="hard")
lines("guide.safety_how", "guide", "P3",
      ["how do I delete everything", "how do I click Save", "how do I send this",
       "how do I empty the trash", "how do I format a drive", "how do I close all tabs",
       "how do I uninstall an app", "how do I clear my history"],
      G("guide", True), difficulty="hard", safety="how_to")

# P4 do
spec("do.app", "do", "P4",
     ["open {x}", "launch {x}", "close {x}", "quit {x}", "please open {x}", "could you open {x}",
      "can you close {x}", "bring up {x}"], APPS, G("do"), question=False, sample=9,
     phenomenon="hard_negative")
spec("do.click", "do", "P4",
     ["click {x}", "press {x}", "click on {x}", "can you click {x}", "hit {x}", "tap {x}"],
     CONTROLS, G("do"), question=False, sample=10, difficulty="hard", safety="ui_action")
spec("do.media", "do", "P4", ["play {x}", "put on {x}", "can you play {x}"], MUSIC, G("do"),
     question=False)
lines("do.transport", "do", "P4",
      ["pause", "pause the music", "skip this song", "turn the volume up", "mute",
       "set the volume to 30", "turn it down a bit", "next track", "stop the music"], G("do"))
lines("do.system", "do", "P4",
      ["take a screenshot", "turn on do not disturb", "set a timer for ten minutes",
       "remind me to call Dad at six", "turn the brightness down", "lock the screen",
       "open my Downloads folder", "open the Econ EE folder", "go to github.com", "open youtube",
       "save this", "close this window", "create a folder called reports"], G("do"))
spec("do.message", "do", "P4",
     ["send {x} a message saying I'll be late", "text {x} that I'm on my way", "email {x} the notes",
      "could you send this to {x}", "reply to {x} saying thanks"], CONTACTS, G("do"),
     question=False, sample=6, difficulty="hard", safety="external")
spec("do.files", "do", "P4",
     ["delete {x}", "rename {x} to final", "move {x} to Documents", "open {x}",
      "copy {x} to the desktop"], FILES, G("do"), question=False, sample=5, safety="destructive")
lines("do.destructive", "do", "P4",
      ["delete everything", "wipe my desktop", "empty the trash", "format the drive",
       "remove everything in downloads", "delete the whole folder", "clear all my history",
       "uninstall everything", "erase the drive", "run rm -rf on my home folder",
       "transfer money from my bank account"], G("do"), difficulty="hard", safety="destructive")

# P5 mission
spec("mission.research", "mission", "P5",
     ["research {x}", "look into {x} and summarise it", "find me sources on {x}",
      "do some research on {x}", "dig into {x}"], SUBJECTS, G("mission"), question=False, sample=6)
lines("mission.plan", "mission", "P5",
      ["plan a business strategy", "make a revision plan for my exams", "plan my week",
       "start a mission", "draft an email to my teacher about the deadline",
       "write a report on market failure", "build a presentation about renewable energy",
       "organise my Downloads folder", "compare Indura's pricing against its competitors",
       "plan a launch for Brew and Critics", "sort out my Desktop", "put together a study schedule"],
      G("mission"))

# P6 complex
spec("complex.when", "complex", "P6",
     ["what's the weather {x}", "will it rain {x}", "what's on my calendar {x}",
      "do I have anything {x}"], WHEN, G("complex"), phenomenon="looks_simple")
spec("complex.place", "complex", "P6",
     ["what's the time in {x}", "how much is a flight to {x}", "what's the weather in {x}"],
     PLACES, G("complex"), phenomenon="looks_simple")
spec("complex.asset", "complex", "P6", ["what's the price of {x}", "how is {x} doing"], ASSETS,
     G("complex"), phenomenon="looks_simple")
spec("complex.venture", "complex", "P6",
     ["how is {x} doing", "how's {x} going", "what's the latest with {x}"], VENTURES,
     G("complex"), difficulty="hard", phenomenon="looks_simple")
lines("complex.personal", "complex", "P6",
      ["what's my next deadline", "any new emails", "how many unread messages do I have",
       "is my essay any good", "what did I say earlier", "what was that file called again",
       "remind me what we discussed", "when is my next meeting", "what's the latest news",
       "who won the match yesterday", "what's the score in the cricket",
       "what happened in the 2026 election"], G("complex"), difficulty="hard",
      phenomenon="looks_simple")

# P7 unknown
lines("unknown.dangling", "unknown", "P7",
      ["do it", "open it", "send it", "fix it", "that one", "the other one", "the usual",
       "can you handle that", "same as before", "yeah that", "that thing", "move it",
       "delete that", "try again", "use that one", "sort it", "put it there", "the second one",
       "do the same for that", "change it back", "and that one too", "you know the one",
       "just do what you did before", "that but bigger", "make it happen", "carry on"],
      G("unknown"), phenomenon="ambiguous")
lines("unknown.negated", "unknown", "P7",
      ["don't open it", "don't do that", "don't send it", "don't click save",
       "don't delete anything", "no, leave it", "don't touch that", "stop, don't send it",
       "don't close it yet", "no, don't", "never send that", "please don't reply",
       "hold on, don't delete it", "wait, don't open that", "no need to click anything",
       "don't bother saving it", "leave the file alone"], G("unknown"), difficulty="hard",
      safety="no_action",
      phenomenon="hard_negative")

# P1 incomplete
lines("incomplete.cut", "incomplete", "P1",
      ["what is the difference between", "can you open the", "explain how", "tell me about",
       "I want to", "open chro", "what's the", "send a message to", "where is the",
       "show me how to", "delete the", "why does the", "how do I", "could you",
       "what's the capital of", "remind me to", "play some", "the thing on", "could you just",
       "why is my", "what does the", "open the folder called", "text Alex that", "explain the",
       "how does a", "set the volume to", "delete everything in", "is it true that"],
      G("unknown", complete=False), phenomenon="partial")


# History and context: the same words, decided by what came before or what is in front.
@custom("follow.conversation", "follow_up", "P8", phenomenon="history")
def _(rng):
    pairs = [
        ("What is GDP?", "and what about GNP"), ("Explain inflation.", "why does that happen"),
        ("What's a monopoly?", "can you give me an example"),
        ("Explain opportunity cost.", "say that more simply"),
        ("Who wrote Hamlet?", "when did he write it"),
        ("What is osmosis?", "how is that different from diffusion"),
        ("Explain the Cold War.", "why did it end"), ("What's a tariff?", "who pays it"),
        ("What is a black hole?", "how big can they get"), ("Explain photosynthesis.",
                                                           "why does it need light"),
        ("What's quantitative easing?", "does it cause inflation"),
        ("Who painted the Mona Lisa?", "where is it now"),
        ("Explain supply and demand.", "can you give me a real example"),
        ("What is entropy?", "why does it always increase"),
    ]
    for i, (history, t) in enumerate(pairs):
        yield i, {"text": t, "history": [history, "Diablo answered it."]}, True, \
            G("explanation" if t.startswith(("why", "how", "say", "can")) else "factual")


@custom("follow.action", "follow_up", "P4", phenomenon="history")
def _(rng):
    pairs = [("I finished the budget spreadsheet.", "open it"),
             ("Draft a message to Mum saying I'll be late.", "send it"),
             ("Where are my screenshots?", "open that folder"),
             ("Spotify is playing too loud.", "turn it down"),
             ("The old drafts are in Documents.", "delete them"),
             ("Alex asked if I'm free tonight.", "reply yes"),
             ("My focus playlist is on Spotify.", "play it"),
             ("The report is on my Desktop.", "move it to Documents")]
    for i, (history, t) in enumerate(pairs):
        yield i, {"text": t, "history": [history, "Diablo answered it."]}, False, G("do")


@custom("follow.screen", "follow_up", "P2", phenomenon="history")
def _(rng):
    pairs = [("What am I looking at?", "and the other one"),
             ("Explain this graph.", "what about the red line"),
             ("What does this error mean?", "and the one below it"),
             ("Where is the welfare loss?", "and the consumer surplus"),
             ("What am I pointing at?", "and this one")]
    for i, (history, t) in enumerate(pairs):
        yield i, {"text": t, "history": [history, "Diablo described the screen."]}, True, \
            G("screen", True)


@custom("context.visual", "guide", "P3", phenomenon="context")
def _(rng):
    for i, v in enumerate(VISUAL_PROCESSES):
        yield i, {"text": rng.choice(["show me how {v}", "walk me through how {v}"]).format(v=v),
                  "app": "Preview", "window": "Subsidy and Welfare.pdf"}, False, G("guide", True)


@custom("context.conversation", "explanation", "P8", phenomenon="context")
def _(rng):
    for i, t in enumerate(["show me how inflation works", "walk me through how interest rates work",
                           "explain graph theory", "explain the idea of a subsidy"]):
        yield i, {"text": t, "app": rng.choice(["Terminal", "Spotify", "Mail"])}, False, \
            G("explanation")


FAMILY_REPEATS = {  # extra noisy renderings per clean line: rarer and harder get more
    "casual": 2, "factual": 1, "explanation": 1, "screen": 2, "guide": 2, "do": 2, "mission": 4,
    "complex": 3, "unknown": 4, "incomplete": 3, "follow_up": 4,
}


# ─── Partials: evolving transcripts ──────────────────────────────────────────

PARTIAL_SEQUENCES = [
    (["open", "open ch", "open chr", "open chrome"], G("do")),
    (["what is", "what is supply", "what is supply chain", "what is supply chain management"],
     G("factual")),
    (["explain", "explain this", "explain this graph"], G("screen", True)),
    (["delete", "delete all", "delete all my", "delete all my files"], G("do")),
    (["how are", "how are you"], G("casual")),
    (["where is", "where is the", "where is the export button"], G("guide", True)),
    (["send", "send mum", "send mum a message saying I'll be late"], G("do")),
    (["why does", "why does ice", "why does ice float"], G("explanation")),
    (["research", "research electric", "research electric vehicle startups"], G("mission")),
    (["what's the", "what's the weather", "what's the weather today"], G("complex")),
]


# ─── The hand-written private test set (never templated, never tuned on) ─────

PRIVATE = [
    ("Mate what's a bond yield", G("factual")), ("gimme the gist of keynesian economics", G("explanation")),
    ("so why'd the romans fall", G("explanation")), ("Is a tomato a fruit?", G("factual")),
    ("how come planes don't fall", G("explanation")), ("honestly what's your view on homework", G("casual")),
    ("ugh today was rough", G("casual")), ("cheers mate appreciate it", G("casual")),
    ("what's this squiggly line doing", G("screen", True)), ("any idea what this popup wants", G("screen", True)),
    ("hmm what's that number at the bottom", G("screen", True)), ("does this look okay to you", G("screen", True)),
    ("which bit is the welfare loss", G("guide", True)), ("where'd the export option go", G("guide", True)),
    ("walk me through adding a slide in keynote", G("guide", True)), ("how would I merge these cells", G("guide", True)),
    ("fire up spotify", G("do")), ("kill chrome", G("do")), ("bin the old drafts", G("do")),
    ("chuck some jazz on", G("do")), ("ping alex that i'm running late", G("do")),
    ("nuke everything on the desktop", G("do")), ("hit save for me", G("do")),
    ("dig into ev startups and give me a rundown", G("mission")), ("sketch out a revision timetable", G("mission")),
    ("pull together a pitch deck outline for indura", G("mission")),
    ("is it gonna rain later", G("complex")), ("how's the rupee today", G("complex")),
    ("what'd we talk about before", G("complex")), ("how many unread mails have i got", G("complex")),
    ("go on then", G("unknown")), ("nah not that one", G("unknown")), ("leave it", G("unknown")),
    ("do the thing", G("unknown")), ("don't hit send", G("unknown")), ("hold off on deleting", G("unknown")),
    ("what's the diff between", G("unknown", complete=False)), ("can you open my", G("unknown", complete=False)),
    ("show me where the", G("unknown", complete=False)), ("explain why the", G("unknown", complete=False)),
    ("should i restart my laptop", G("casual")), ("would it be smart to learn rust", G("casual")),
    ("is clicking this link safe", G("screen", True)), ("how do i wipe a drive safely", G("guide", True)),
    ("could you empty the bin", G("do")), ("would emptying the bin free up space", G("factual")),
    ("what do you reckon about the economy", G("casual")), ("explain this chart real quick", G("screen", True)),
    ("explain supply chains real quick", G("explanation")), ("open the thing", G("unknown")),
]


# ─── Build ───────────────────────────────────────────────────────────────────

def normalise(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


SECRET_PATTERNS = [re.compile(p, re.I) for p in (
    r"sk-[a-z0-9]{16,}", r"AKIA[0-9A-Z]{12,}", r"\b[0-9a-f]{32,}\b", r"password\s*[:=]",
    r"[\w.+-]+@[\w-]+\.[\w.]+", r"\+?\d[\d -]{8,}\d")]


def example(rng, fam, index, raw, question, gold, split, version):
    context, history = {}, []
    if isinstance(raw, dict):
        text = raw["text"]
        context = {k: raw[k] for k in ("app", "window") if raw.get(k)}
        history = raw.get("history") or []
    else:
        text = raw
    order = INTENTS[:]
    rng.shuffle(order)                                  # option-order robustness
    ex_id = hashlib.sha1(f"{version}:{fam['name']}:{index}:{text}".encode()).hexdigest()[:12]
    return {
        "id": ex_id, "text": text, "history": history, "context": context,
        "questions": ["intent", "needs_screen", "complete"],
        "options": {"intent": order},
        "gold_answers": dict(gold, lane=lane(gold["intent"], gold["needs_screen"], gold["complete"])),
        "label_source": f"policy:{fam['clause']}:template:{fam['name']}",
        "difficulty": fam["difficulty"], "safety_class": fam["safety"],
        "split": split, "metadata": {"family": fam["name"], "category": fam["category"],
                                     "phenomenon": fam["phenomenon"], "question_form": question},
    }


TARGET = {"train": 0.7, "validation": 0.1, "calibration": 0.1, "test": 0.1}


def assign(rng, units):
    """
    Units (with their line counts) -> splits, per category. Test is filled
    first, then calibration and validation, each to its share of lines;
    whatever is left is train. Every category with three or more units puts
    at least one in test.
    """
    order = sorted(units)
    rng.shuffle(order)
    total = sum(units.values())
    filled = {k: 0 for k in TARGET}
    out = {}
    for split in ("test", "calibration", "validation"):
        for unit in list(order):
            if unit in out:
                continue
            if filled[split] >= TARGET[split] * total and filled[split] > 0:
                break
            if len(order) - len(out) <= 1:
                break
            out[unit] = split
            filled[split] += units[unit]
    for unit in order:
        out.setdefault(unit, "train")
    return out


def normalised_reserved():
    reserved = {normalise(t) for t, _ in PRIVATE}
    for sequence, _ in PARTIAL_SEQUENCES:
        reserved |= {normalise(t) for t in sequence}
    return reserved


def build(version="v1"):
    rng = random.Random(SEED)
    reserved = normalised_reserved()
    generated = []
    for fam in FAMILIES:
        for frame, raw, question, gold in fam["fn"](random.Random(f"{SEED}:{fam['name']}")):
            generated.append((fam, frame, raw, question, gold))
    # Units per category, counted in lines.
    per_cat = defaultdict(Counter)
    for fam, frame, *_ in generated:
        per_cat[fam["category"]][f"{fam['name']}#{frame}"] += 1
    unit_split = {}
    for cat, units in sorted(per_cat.items()):
        unit_split.update(assign(random.Random(f"{SEED}:{cat}"), dict(units)))
    rows, seen = [], set()
    dropped_duplicates = dropped_reserved = 0
    for index, (fam, frame, raw, question, gold) in enumerate(generated):
        split = unit_split[f"{fam['name']}#{frame}"]
        renders = [raw]
        for _r in range(FAMILY_REPEATS.get(fam["category"], 1)):
            if isinstance(raw, dict):
                renders.append(dict(raw, text=noisy(rng, raw["text"], question)))
            else:
                renders.append(noisy(rng, raw, question))
        for k, rendering in enumerate(renders):
            if k == 0:
                rendering = (dict(rendering, text=sentence(rendering["text"], question))
                             if isinstance(rendering, dict) else sentence(rendering, question))
            text = rendering["text"] if isinstance(rendering, dict) else rendering
            extra = json.dumps({a: b for a, b in rendering.items() if a != "text"},
                               sort_keys=True) if isinstance(rendering, dict) else ""
            key = (normalise(text), extra)
            if not extra and normalise(text) in reserved:
                dropped_reserved += 1
                continue
            if key in seen:
                dropped_duplicates += 1
                continue
            seen.add(key)
            ex = example(rng, fam, f"{frame}.{index}.{k}", rendering, question, gold, split, version)
            ex["metadata"]["unit"] = f"{fam['name']}#{frame}"
            rows.append(ex)
    partial_split = ["test", "calibration", "validation", "test"] + ["train"] * 6
    for s, (sequence, final) in enumerate(PARTIAL_SEQUENCES):
        fam = dict(name=f"partial.{s}", category="partial", clause="P1", difficulty="hard",
                   safety="none", phenomenon="partial")
        for k, text in enumerate(sequence):
            last = k == len(sequence) - 1
            gold = final if last else G("unknown", complete=False)
            ex = example(rng, fam, f"{k}", text, False, gold, partial_split[s], version)
            ex["metadata"].update(sequence=s, position=k, unit=f"partial#{s}")
            ex["label_source"] = (f"policy:P1:partial:{s}:{k}" if not last
                                  else f"policy:final:partial:{s}")
            rows.append(ex)
    private = []
    fam = dict(name="private", category="private", clause="adjudicated", difficulty="hard",
               safety="none", phenomenon="unseen_phrasing")
    for index, (text, gold) in enumerate(PRIVATE):
        ex = example(rng, fam, index, text, text.endswith("?"), gold, "test_private", version)
        ex["metadata"]["unit"] = f"private#{index}"
        ex["label_source"] = "adjudicated:hand-written unseen phrasing, labelled by the gold policy"
        private.append(ex)
    return rows, private, {"duplicates": dropped_duplicates, "reserved": dropped_reserved}


def rules_verdicts(rows):
    """
    What the production Rules engine says about each example, keyed by id.
    Kept out of the dataset rows, so the dataset's hash does not depend on
    which version of the rules was installed when it was built.
    """
    home = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
    sys.path.insert(0, home)
    try:
        from skills import decision
    except Exception:
        return {}
    rules = decision.Rules()
    out = {}
    for ex in rows:
        v = rules.decide(ex["text"], in_conversation=bool(ex["history"]))
        out[ex["id"]] = v.lane if v else "none"
    return out


def audit(rows, private, dropped, ruled=None):
    report = {}
    splits = defaultdict(list)
    for ex in rows + private:
        splits[ex["split"]].append(ex)
    report["counts"] = {k: len(v) for k, v in sorted(splits.items())}
    report["total"] = len(rows) + len(private)
    report["duplicates_dropped"] = dropped
    report["class_balance"] = {k: dict(Counter(e["gold_answers"]["intent"] for e in v))
                               for k, v in sorted(splits.items())}
    report["lane_balance"] = {k: dict(Counter(e["gold_answers"]["lane"] for e in v))
                              for k, v in sorted(splits.items())}
    report["category_balance"] = dict(Counter(e["metadata"]["category"] for e in rows))
    report["hard_negatives"] = sum(1 for e in rows if e["metadata"]["phenomenon"] == "hard_negative")
    report["safety_examples"] = dict(Counter(e["safety_class"] for e in rows
                                             if e["safety_class"] != "none"))
    report["partials"] = sum(1 for e in rows if e["metadata"]["phenomenon"] == "partial")
    report["history_or_context"] = sum(1 for e in rows if e["history"] or e["context"])
    where = defaultdict(set)
    for e in rows + private:
        where[(normalise(e["text"]), json.dumps(e["context"], sort_keys=True),
               json.dumps(e["history"]))].add(e["split"])
    report["exact_cross_split"] = sorted(t[0] for t, s in where.items() if len(s) > 1)[:50]
    units = defaultdict(set)
    for e in rows:
        units[e["metadata"]["unit"]].add(e["split"])
    report["units_in_two_splits"] = sorted(u for u, s in units.items() if len(s) > 1)
    report["units"] = len(units)

    def grams(t):
        w = normalise(t).split()
        return {" ".join(w[i:i + 3]) for i in range(max(1, len(w) - 2))}
    test = [(e, grams(e["text"])) for e in splits["test"] + splits["test_private"]]
    train = [(e, grams(e["text"])) for e in splits["train"]]
    near = []
    for te, tg in test:
        for tr, rg in train:
            if tg and rg:
                j = len(tg & rg) / len(tg | rg)
                if j >= 0.8 and normalise(te["text"]) != normalise(tr["text"]):
                    near.append((te["text"], tr["text"], round(j, 2)))
                    break
    report["near_duplicates_test_vs_train"] = {"count": len(near), "examples": near[:15]}
    labels = defaultdict(set)
    for e in rows + private:
        labels[(normalise(e["text"]), json.dumps(e["context"], sort_keys=True),
                json.dumps(e["history"]))].add(json.dumps(e["gold_answers"], sort_keys=True))
    report["label_conflicts"] = sum(1 for v in labels.values() if len(v) > 1)
    positions = Counter(e["options"]["intent"].index(e["gold_answers"]["intent"]) for e in rows)
    report["gold_option_position"] = dict(sorted(positions.items()))
    report["secrets_found"] = sum(1 for e in rows + private
                                  if any(p.search(e["text"]) for p in SECRET_PATTERNS))
    if ruled:
        agree = Counter()
        for e in rows + private:
            gold_fast = e["gold_answers"]["lane"] == "fast"
            rules_fast = ruled.get(e["id"]) in ("fast", "instant")
            agree[e["split"] + ":" + ("gold_fast" if gold_fast else "gold_not_fast") + "/" +
                  ("rules_fast" if rules_fast else "rules_not_fast")] += 1
        report["rules_vs_gold"] = dict(sorted(agree.items()))
    return report


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1")
    args = ap.parse_args()
    rows, private, dropped = build(args.version)
    ruled = rules_verdicts(rows + private)
    out = os.path.join(HERE, args.version)
    os.makedirs(out, exist_ok=True)
    hashes = {}
    for split in SPLITS + ("test_private",):
        path = os.path.join(out, f"{split}.jsonl")
        with open(path, "w") as f:
            for e in rows + private:
                if e["split"] == split:
                    f.write(json.dumps(e, sort_keys=True) + "\n")
        hashes[split] = sha(path)
    report = audit(rows, private, dropped, ruled)
    report["rules_verdicts_recorded"] = bool(ruled)
    json.dump(report, open(os.path.join(out, "audit.json"), "w"), indent=1, sort_keys=True)
    manifest = {
        "dataset_version": args.version, "seed": SEED, "contract_version": CONTRACT["version"],
        "split_hashes": hashes, "counts": report["counts"], "total": report["total"],
        "gold_policy": "research/laya_diablo/dataset/build_dataset.py (docstring P1-P8)",
        "label_sources": {"policy": "template families labelled by clause",
                          "adjudicated": "hand-written private test set",
                          "teacher": "none used"},
        "splits": ("whole template families for categories with 4+ families, whole clean "
                   "lines otherwise; partial sequences whole; test_private hand-written"),
    }
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=1, sort_keys=True)
    print(json.dumps({k: report[k] for k in ("counts", "total", "duplicates_dropped",
                                             "label_conflicts", "exact_cross_split",
                                             "units_in_two_splits", "secrets_found",
                                             "category_balance")}, indent=1))


if __name__ == "__main__":
    main()
