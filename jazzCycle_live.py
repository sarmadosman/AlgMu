import time
import random
import threading
from dataclasses import dataclass, field
from typing import Set, List, Tuple, Optional

import mido

# -----------------------------
# User controls
# -----------------------------
seed = None
tempo = 135
complexity = 0.75  # 0..1
complexity = max(0.0, min(1.0, complexity))

PLAY_PIANO = True
PLAY_BASS = True
PLAY_DRUMS = True

# If True, piano waits for you to stop before responding.
# If False, piano plays continuously over your input.
PHRASE_GATED_PIANO = True
PHRASE_END_DELAY = 0.35  # seconds

# Use only the most recent N notes of the last/current phrase for harmony/voicing
RECENT_PHRASE_NOTES = 6

# Treat sustain pedal as "still playing" to avoid premature phrase ends
USE_SUSTAIN_PEDAL = True
SUSTAIN_CC = 64
SUSTAIN_ON_THRESHOLD = 64  # >= this means pedal down

MIDI_IN_NAME = "IAC Driver Bus 1"
PIANO_OUT_NAME = "Jazz Piano Out"
BASS_OUT_NAME = "Jazz Bass Out"
DRUM_OUT_NAME = "Jazz Drums Out"

HH_CLOSED = 42
RIDE = 51

# Fallback piano range if there is no phrase data yet
PIANO_LOW = 36   # C2
PIANO_HIGH = 72  # C5

# How tightly the generated piano follows your played register
REGISTER_HALF_SPAN = 8   # ±8 semitones around phrase center
MIN_REGISTER_WIDTH = 12  # at least one octave

CH = 0  # outgoing MIDI channel

# -----------------------------
# State
# -----------------------------
@dataclass
class ChordState:
    held: Set[int] = field(default_factory=set)
    root_pc: int = 0
    scale_pcs: List[int] = field(default_factory=lambda: [0, 2, 3, 5, 7, 9, 10])

    last_note_on_time: float = 0.0
    last_note_off_time: float = 0.0
    responding: bool = False

    sustain_down: bool = False

    current_phrase_notes: List[int] = field(default_factory=list)
    last_phrase_notes: List[int] = field(default_factory=list)
    phrase_id: int = 0

state = ChordState()
state_lock = threading.Lock()

# -----------------------------
# Utils
# -----------------------------
def pc(n: int) -> int:
    return n % 12

def clamp7(x: int) -> int:
    return max(0, min(127, x))

def wrap_to_range(note: int, low: int, high: int) -> int:
    while note < low:
        note += 12
    while note > high:
        note -= 12
    return note

def detect_root_pitch_class(notes: Set[int], fallback: int) -> int:
    return pc(min(notes)) if notes else fallback

def build_scale_from_notes(root_pc: int, notes: Set[int]) -> List[int]:
    pcs = {pc(n) for n in notes}
    has_M3 = (root_pc + 4) % 12 in pcs

    if has_M3:
        return [0, 2, 4, 5, 7, 9, 11]   # Major / Ionian
    return [0, 2, 3, 5, 7, 8, 10]       # Natural minor / Aeolian


def build_harmony_from_phrase(
    phrase_notes: List[int],
    fallback_root: int,
    fallback_scale: List[int],
    recent_n: int = RECENT_PHRASE_NOTES,
) -> Tuple[int, List[int]]:
    if not phrase_notes:
        return fallback_root, list(fallback_scale)

    recent = phrase_notes[-recent_n:] if recent_n > 0 else phrase_notes
    note_set = set(recent)
    root = detect_root_pitch_class(note_set, fallback_root)
    scale = build_scale_from_notes(root, note_set)
    return root, scale

def get_recent_source_notes(last_phrase_notes: List[int], held_now: Set[int]) -> List[int]:
    if last_phrase_notes:
        return last_phrase_notes[-RECENT_PHRASE_NOTES:] if RECENT_PHRASE_NOTES > 0 else list(last_phrase_notes)
    if held_now:
        held_sorted = sorted(held_now)
        return held_sorted[-RECENT_PHRASE_NOTES:] if RECENT_PHRASE_NOTES > 0 else held_sorted
    return []

def get_phrase_range(notes: List[int]) -> Tuple[int, int]:
    if not notes:
        return PIANO_LOW, PIANO_HIGH

    center = round(sum(notes) / len(notes))
    low = max(0, center - REGISTER_HALF_SPAN)
    high = min(127, center + REGISTER_HALF_SPAN)

    if high - low < MIN_REGISTER_WIDTH:
        high = min(127, low + MIN_REGISTER_WIDTH)
        low = max(0, high - MIN_REGISTER_WIDTH)

    return low, high

def choose_chord_base(notes: List[int], low: int, high: int) -> int:
    if notes:
        center = round(sum(notes) / len(notes))
        return wrap_to_range(center, low, high)
    return wrap_to_range(60, low, high)

def snapshot_state():
    with state_lock:
        return (
            set(state.held),
            state.root_pc,
            list(state.scale_pcs),
            state.last_note_on_time,
            state.last_note_off_time,
            state.responding,
            state.sustain_down,
            list(state.last_phrase_notes),
            list(state.current_phrase_notes),
            state.phrase_id,
        )

# -----------------------------
# Interaction / gating (PIANO ONLY)
# -----------------------------
def piano_enabled() -> bool:
    if not PHRASE_GATED_PIANO:
        return True

    now = time.monotonic()
    with state_lock:
        user_active = bool(state.held) or (USE_SUSTAIN_PEDAL and state.sustain_down)
        if user_active:
            state.responding = False
            return False

        if state.responding:
            return True

        if state.last_note_on_time <= 0.0 or state.last_note_off_time <= 0.0:
            return False

        if (now - state.last_note_off_time) < PHRASE_END_DELAY:
            return False

        state.responding = True
        return True

# -----------------------------
# MIDI I/O
# -----------------------------
def note_on(port, note: int, vel: int):
    port.send(mido.Message("note_on", channel=CH, note=clamp7(note), velocity=clamp7(vel)))

def note_off(port, note: int):
    note_on(port, note, 0)

def rand_int(limit: int) -> int:
    return 0 if limit <= 0 else random.randrange(limit)

def chance(base: float) -> bool:
    p = base * (0.25 + 0.75 * complexity)
    return random.random() < max(0.0, min(1.0, p))

def sleep_for_step(i: int):
    if i in (0, 3):
        seconds = 16.0 / tempo
    elif i in (2, 5):
        seconds = 20.0 / tempo
    else:
        seconds = 24.0 / tempo
    time.sleep(seconds)

def quantize_to_scale(note: int, root_pc: int, scale_pcs: List[int]) -> int:
    target_pcs = {(root_pc + s) % 12 for s in scale_pcs}
    best = note
    best_dist = 999
    for d in range(-6, 7):
        cand = note + d
        if pc(cand) in target_pcs:
            dist = abs(d)
            if dist < best_dist:
                best_dist = dist
                best = cand
    return best

# -----------------------------
# MIDI input thread
# -----------------------------
def midi_input_thread():
    with mido.open_input(MIDI_IN_NAME) as inp:
        print(f"Listening on MIDI input: {MIDI_IN_NAME}")
        for msg in inp:
            changed = False

            if msg.type == "control_change" and msg.control == SUSTAIN_CC and USE_SUSTAIN_PEDAL:
                with state_lock:
                    state.sustain_down = (msg.value >= SUSTAIN_ON_THRESHOLD)
                    if state.sustain_down:
                        state.responding = False
                continue

            if msg.type == "note_on" and msg.velocity > 0:
                with state_lock:
                    starting_new_phrase = (len(state.held) == 0) and (not (USE_SUSTAIN_PEDAL and state.sustain_down))
                    if starting_new_phrase:
                        state.current_phrase_notes.clear()

                    state.held.add(msg.note)
                    state.current_phrase_notes.append(msg.note)
                    state.last_note_on_time = time.monotonic()
                    state.responding = False
                changed = True

            elif msg.type == "note_off":
                with state_lock:
                    state.held.discard(msg.note)
                    if not state.held and not (USE_SUSTAIN_PEDAL and state.sustain_down):
                        state.last_note_off_time = time.monotonic()
                        if state.current_phrase_notes:
                            state.last_phrase_notes = list(state.current_phrase_notes)
                            state.phrase_id += 1
                changed = True

            elif msg.type == "note_on" and msg.velocity == 0:
                with state_lock:
                    state.held.discard(msg.note)
                    if not state.held and not (USE_SUSTAIN_PEDAL and state.sustain_down):
                        state.last_note_off_time = time.monotonic()
                        if state.current_phrase_notes:
                            state.last_phrase_notes = list(state.current_phrase_notes)
                            state.phrase_id += 1
                changed = True

            if changed:
                with state_lock:
                    if state.held:
                        state.root_pc = detect_root_pitch_class(state.held, state.root_pc)
                        state.scale_pcs = build_scale_from_notes(state.root_pc, state.held)

# -----------------------------
# Pattern tables
# -----------------------------
piano_patterns = [
    [None, None, None, None, None, None],
    [None, None, None, None, 5,    None],
    [None, None, None, None, 6,    -1],
    [None, 4,    None, None, 1,    None],
    [None, None, None, None, 4,    1],
    [None, None, 4,    None, 1,    None],
    [None, None, 4,    None, None, 1],
    [None, 8,    None, None, -2,   -1],
    [None, None, None, None, None, None],
    [None, 3,    5,    None, -2,   -1],
    [None, 9,    -1,   None, -2,   -1],
    [None, 6,    -3,   None, 1,    1],
    [None, None, None, None, None, None],
    [None, 3,    3,    None, -2,   1],
    [None, 1,    2,    None, 1,    1],
    [None, 10,   -2,   None, -2,   -1],
    [None, 7,    -1,   None, -2,   1],
    [None, 4,    -1,   None, 3,    -1],
    [None, None, None, None, None, None],
    [None, 3,    None, 3,    -2,   1],
    [1,    1,    1,    None, 3,    -1],
    [1,    1,    1,    3,    -2,   1],
]

ride_patterns = [
    [False, False, False, False, False, False],
    [False, False, False, False, False, True],
    [False, False, False, False, True,  True],
    [False, False, True,  False, True,  True],
]

bass = [0, 5, -2, 3, -4, 1, 6, -1, 4, -3, 2, -5]
bass_idx = 0

# -----------------------------
# Main
# -----------------------------
def main():
    global bass_idx
    random.seed(seed)
    print(f"Running with tempo={tempo} BPM")
    print(f"Piano gated mode: {PHRASE_GATED_PIANO}")
    print(f"Input:  {MIDI_IN_NAME}")
    print(f"Piano:  {PIANO_OUT_NAME}")
    print(f"Bass:   {BASS_OUT_NAME}")
    print(f"Drums:  {DRUM_OUT_NAME}")

    piano_out = mido.open_output(PIANO_OUT_NAME, virtual=True)
    bass_out = mido.open_output(BASS_OUT_NAME, virtual=True)
    drum_out = mido.open_output(DRUM_OUT_NAME, virtual=True)

    t = threading.Thread(target=midi_input_thread, daemon=True)
    t.start()

    active_melody_note: Optional[int] = None
    active_harmony_notes: List[int] = []

    piano_note = 60
    last_seen_phrase_id = -1
    piano_was_on = False

    try:
        while True:
            piano_pat = rand_int(len(piano_patterns))
            max_ride = len(ride_patterns) - 1
            biased = int(round(max_ride * complexity))
            ride_pat = max(0, min(max_ride, biased + rand_int(2) - 1))

            for i in range(6):
                (
                    held_now,
                    root_pc,
                    scale_pcs,
                    _,
                    _,
                    _,
                    _,
                    last_phrase_notes,
                    current_phrase_notes,
                    phrase_id,
                ) = snapshot_state()

                allow_piano = piano_enabled()

                source_notes = get_recent_source_notes(
                    last_phrase_notes if last_phrase_notes else current_phrase_notes,
                    held_now,
                )
                dyn_low, dyn_high = get_phrase_range(source_notes)

                if phrase_id != last_seen_phrase_id and last_phrase_notes:
                    last_seen_phrase_id = phrase_id
                    piano_note = wrap_to_range(last_phrase_notes[-1], dyn_low, dyn_high)
                    root_pc, scale_pcs = build_harmony_from_phrase(last_phrase_notes, root_pc, scale_pcs)

                if not allow_piano and piano_was_on:
                    if PLAY_PIANO:
                        if active_melody_note is not None:
                            note_off(piano_out, active_melody_note)
                            active_melody_note = None
                        for n in active_harmony_notes:
                            note_off(piano_out, n)
                        active_harmony_notes.clear()

                piano_was_on = allow_piano

                # -------------------------
                # Piano
                # -------------------------
                if PLAY_PIANO and allow_piano:
                    delta = piano_patterns[piano_pat][i]
                    if delta is not None:
                        if active_melody_note is not None:
                            note_off(piano_out, active_melody_note)

                        piano_note += delta
                        piano_note = quantize_to_scale(piano_note, root_pc, scale_pcs)
                        piano_note = wrap_to_range(piano_note, dyn_low, dyn_high)

                        note_on(piano_out, piano_note, 40 + rand_int(60))
                        active_melody_note = piano_note

                if PLAY_PIANO and allow_piano and i == 4 and chance(0.8):
                    for n in active_harmony_notes:
                        note_off(piano_out, n)
                    active_harmony_notes.clear()

                    if source_notes:
                        chord_pcs = sorted({pc(n) for n in source_notes})
                    elif held_now:
                        chord_pcs = sorted({pc(n) for n in held_now})
                    else:
                        chord_pcs = [(root_pc + s) % 12 for s in (0, 3, 7)]

                    chosen = chord_pcs[:3] if len(chord_pcs) >= 3 else chord_pcs
                    base = choose_chord_base(source_notes, dyn_low, dyn_high)

                    for p in chosen:
                        n = base + ((p - pc(base)) % 12)
                        n = wrap_to_range(n, dyn_low, dyn_high)
                        active_harmony_notes.append(n)
                        note_on(piano_out, n, 25 + rand_int(60))

                if PLAY_PIANO and active_harmony_notes and i == 0:
                    for n in active_harmony_notes:
                        note_off(piano_out, n)
                    active_harmony_notes.clear()

                # -------------------------
                # Drums
                # -------------------------
                if PLAY_DRUMS and ride_patterns[ride_pat][i]:
                    note_on(drum_out, RIDE, 20 + rand_int(40))
                    note_off(drum_out, RIDE)

                if PLAY_DRUMS and i == 2 and chance(0.9):
                    note_on(drum_out, HH_CLOSED, 30 + rand_int(40))
                    note_off(drum_out, HH_CLOSED)

                # -------------------------
                # Bass
                # -------------------------
                if PLAY_BASS and i == 2:
                    note_off(bass_out, bass[bass_idx] + 48)
                    bass_idx = (bass_idx + 1) % 12

                    if chance(0.35):
                        r = rand_int(3)
                        if r == 0:
                            note_on(bass_out, bass[bass_idx] + 48 - 1, 50)
                        elif r == 1:
                            note_on(bass_out, bass[bass_idx] + 48 + 1, 50)

                if PLAY_BASS and i == 4 and chance(0.30):
                    r = rand_int(4)
                    if r == 0:
                        note_on(bass_out, bass[bass_idx] + 48 - 1, 50)
                    elif r == 1:
                        note_on(bass_out, bass[bass_idx] + 48 + 1, 50)

                if PLAY_BASS and i == 5:
                    note_off(bass_out, bass[bass_idx] + 48 - 1)
                    note_off(bass_out, bass[bass_idx] + 48 + 1)
                    note_on(bass_out, bass[bass_idx] + 48, 50)

                sleep_for_step(i)

    except KeyboardInterrupt:
        pass
    finally:
        for n in range(128):
            note_off(piano_out, n)
            note_off(bass_out, n)
            note_off(drum_out, n)
        piano_out.close()
        bass_out.close()
        drum_out.close()
        print("\nStopped.")

if __name__ == "__main__":
    main()