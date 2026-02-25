import time
import random
import threading
from dataclasses import dataclass, field
from typing import Set, List

import mido

# -----------------------------
# User controls
# -----------------------------
seed = 23
tempo = 135
complexity = 0.75  # 0..1
complexity = max(0.0, min(1.0, complexity))

PLAY_PIANO = True
PLAY_BASS = True
PLAY_DRUMS = True

PHRASE_END_DELAY = 0.35  # seconds

MIDI_IN_NAME = "IAC Driver Bus 1"
PIANO_OUT_NAME = "Jazz Piano Out"
BASS_OUT_NAME  = "Jazz Bass Out"
DRUM_OUT_NAME  = "Jazz Drums Out"

HH_CLOSED = 42
RIDE = 51

# Keep generated piano stable around 3rd–5th octaves
PIANO_LOW = 48   # C3
PIANO_HIGH = 72  # C5

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

    # Phrase memory (latest input phrase drives next generation)
    current_phrase_notes: List[int] = field(default_factory=list)
    last_phrase_notes: List[int] = field(default_factory=list)
    phrase_id: int = 0

state = ChordState()
state_lock = threading.Lock()

def pc(n: int) -> int:
    return n % 12

def clamp7(x: int) -> int:
    return max(0, min(127, x))

def wrap_to_range(note: int, low: int = PIANO_LOW, high: int = PIANO_HIGH) -> int:
    """
    Wrap note by octaves into [low, high] to prevent octave drift.
    """
    while note < low:
        note += 12
    while note > high:
        note -= 12
    return note

def detect_root_pitch_class(notes: Set[int]) -> int:
    return pc(min(notes)) if notes else state.root_pc

def build_scale_from_chord(root_pc: int, notes: Set[int]) -> List[int]:
    pcs = {pc(n) for n in notes}
    has_M3 = (root_pc + 4) % 12 in pcs
    if has_M3:
        return [0, 2, 4, 5, 7, 9, 10]   
    return [0, 2, 3, 5, 7, 9, 10]      

def build_harmony_from_phrase(phrase_notes: List[int], fallback_root: int, fallback_scale: List[int]):
    """
    Infer root/scale from latest completed input phrase.
    """
    if not phrase_notes:
        return fallback_root, list(fallback_scale)

    note_set = set(phrase_notes)
    root = detect_root_pitch_class(note_set)
    scale = build_scale_from_chord(root, note_set)
    return root, scale

def snapshot_harmony():
    with state_lock:
        return (
            set(state.held),
            state.root_pc,
            list(state.scale_pcs),
            state.last_note_on_time,
            state.last_note_off_time,
            state.responding,
            list(state.last_phrase_notes),
            state.phrase_id,
        )

def generation_enabled() -> bool:
    """
    Phrase-response latch:
    - Silent while user is playing
    - After phrase end + delay, start responding
    - Keep responding until user plays again
    """
    now = time.monotonic()
    with state_lock:
        if state.held:
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

def midi_input_thread():
    with mido.open_input(MIDI_IN_NAME) as inp:
        for msg in inp:
            changed = False

            if msg.type == "note_on" and msg.velocity > 0:
                with state_lock:
                    # New phrase starts when no notes were held
                    if len(state.held) == 0:
                        state.current_phrase_notes.clear()

                    state.held.add(msg.note)
                    state.current_phrase_notes.append(msg.note)
                    state.last_note_on_time = time.monotonic()
                    state.responding = False  # stop generator immediately when user resumes
                changed = True

            elif msg.type == "note_off":
                with state_lock:
                    state.held.discard(msg.note)
                    if not state.held:
                        state.last_note_off_time = time.monotonic()
                        if state.current_phrase_notes:
                            state.last_phrase_notes = list(state.current_phrase_notes)
                            state.phrase_id += 1
                changed = True

            elif msg.type == "note_on" and msg.velocity == 0:
                with state_lock:
                    state.held.discard(msg.note)
                    if not state.held:
                        state.last_note_off_time = time.monotonic()
                        if state.current_phrase_notes:
                            state.last_phrase_notes = list(state.current_phrase_notes)
                            state.phrase_id += 1
                changed = True

            if changed:
                with state_lock:
                    if state.held:
                        state.root_pc = detect_root_pitch_class(state.held)
                        state.scale_pcs = build_scale_from_chord(state.root_pc, state.held)

# -----------------------------
# MIDI output helpers
# -----------------------------
piano_out = mido.open_output(PIANO_OUT_NAME, virtual=True)
bass_out  = mido.open_output(BASS_OUT_NAME,  virtual=True)
drum_out  = mido.open_output(DRUM_OUT_NAME,  virtual=True)

CH = 0

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

# -----------------------------
# Quantize melody to current scale
# -----------------------------
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
# Main generative loop
# -----------------------------
def main():
    global bass_idx
    random.seed(seed)
    print(f"Running with tempo={tempo} BPM")

    t = threading.Thread(target=midi_input_thread, daemon=True)
    t.start()

    piano_note = 60
    active_harmony_notes: List[int] = []
    piano_gate_was_open = False
    last_seen_phrase_id = -1

    while True:
        piano_pat = rand_int(len(piano_patterns))
        max_ride = len(ride_patterns) - 1
        biased = int(round(max_ride * complexity))
        ride_pat = max(0, min(max_ride, biased + rand_int(2) - 1))

        for i in range(6):
            held_now, root_pc, scale_pcs, _, _, _, last_phrase_notes, phrase_id = snapshot_harmony()
            allow_piano = generation_enabled()

            # NEW: when a new phrase arrives, reseed melody/harmony from the latest phrase only
            if phrase_id != last_seen_phrase_id and last_phrase_notes:
                last_seen_phrase_id = phrase_id

                # Start response from the LAST note the user played
                piano_note = wrap_to_range(last_phrase_notes[-1], PIANO_LOW, PIANO_HIGH)

                # Refresh harmony from latest phrase (not stale generator state)
                root_pc, scale_pcs = build_harmony_from_phrase(last_phrase_notes, root_pc, scale_pcs)

            # If user starts playing again, immediately silence generated piano
            if not allow_piano and piano_gate_was_open:
                if PLAY_PIANO:
                    note_off(piano_out, piano_note)
                    for n in active_harmony_notes:
                        note_off(piano_out, n)
                active_harmony_notes.clear()

            piano_gate_was_open = allow_piano

            # Piano melody (phrase-end gated, latest-phrase-conditioned)
            if PLAY_PIANO and allow_piano:
                delta = piano_patterns[piano_pat][i]
                if delta is not None and chance(0.95):
                    note_off(piano_out, piano_note)
                    piano_note += delta
                    piano_note = quantize_to_scale(piano_note, root_pc, scale_pcs)
                    piano_note = wrap_to_range(piano_note, PIANO_LOW, PIANO_HIGH)  # prevent octave drift
                    note_on(piano_out, piano_note, 40 + rand_int(60))

            # Piano harmony stab (phrase-end gated)
            if PLAY_PIANO and allow_piano and i == 4 and chance(0.8):
                for n in active_harmony_notes:
                    note_off(piano_out, n)
                active_harmony_notes.clear()

                # Prefer recent notes from latest phrase for voicing source
                if last_phrase_notes:
                    chord_pcs = sorted({pc(n) for n in last_phrase_notes[-6:]})
                elif held_now:
                    chord_pcs = sorted({pc(n) for n in held_now})
                else:
                    chord_pcs = [(root_pc + s) % 12 for s in (0, 3, 7)]

                chosen = chord_pcs[:3] if len(chord_pcs) >= 3 else chord_pcs
                base = 60  # stable comping register
                voiced = []
                for p in chosen:
                    note = base + ((p - pc(base)) % 12)
                    voiced.append(note)
                    note_on(piano_out, note, 25 + rand_int(60))
                active_harmony_notes = voiced

            if PLAY_PIANO and active_harmony_notes and i == 0:
                for n in active_harmony_notes:
                    note_off(piano_out, n)
                active_harmony_notes.clear()

            # Drums (kick removed)
            if PLAY_DRUMS and ride_patterns[ride_pat][i]:
                note_on(drum_out, RIDE, 20 + rand_int(40))
                note_off(drum_out, RIDE)

            if PLAY_DRUMS and i == 2 and chance(0.9):
                note_on(drum_out, HH_CLOSED, 30 + rand_int(40))
                note_off(drum_out, HH_CLOSED)

            # Bass (old logic)
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

        # Final safeguard against drift
        piano_note = wrap_to_range(piano_note, PIANO_LOW, PIANO_HIGH)

def panic_off():
    for n in range(128):
        note_off(piano_out, n)
        note_off(bass_out, n)
        note_off(drum_out, n)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        panic_off()
        piano_out.close()
        bass_out.close()
        drum_out.close()
        print("\nStopped.")