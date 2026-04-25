"""
Generative Jazz MIDI -> 3 separate virtual MIDI outputs (Piano/Bass/Drums)
Best for GarageBand.

macOS notes:
- Uses CoreMIDI via python-rtmidi. Virtual ports should appear in GarageBand.
- If they don't, enable IAC Driver anyway (sometimes helps) and restart GarageBand.

Dependencies:
  pip install mido python-rtmidi
"""

import time
import random
from typing import List, Optional

import mido

# ---------------------------
# Controls
# ---------------------------
seed = 1
tempo = 135
complexity = 0.9  # 0..1
complexity = max(0.0, min(1.0, complexity))

# Virtual port names (these will show up as MIDI inputs in GarageBand)
PIANO_PORT_NAME = "Jazz Piano Out"
BASS_PORT_NAME  = "Jazz Bass Out"
DRUM_PORT_NAME  = "Jazz Drums Out"

PLAY_PIANO = True
PLAY_BASS = True
PLAY_DRUMS = True

# ---------------------------
# MIDI setup
# ---------------------------

def open_virtual(name: str) -> mido.ports.BaseOutput:
    # virtual=True requests a CoreMIDI virtual source on macOS (rtmidi backend)
    return mido.open_output(name, virtual=True)

piano_out = open_virtual(PIANO_PORT_NAME)
bass_out  = open_virtual(BASS_PORT_NAME)
drum_out  = open_virtual(DRUM_PORT_NAME)

# Give CoreMIDI a moment
time.sleep(0.2)

# In mido, channels are 0-based. We can just use channel 0 everywhere because
# we’re separating by PORT now (GarageBand-friendly).
CH = 0

def clamp7(x: int) -> int:
    return max(0, min(127, x))

def note_on(port: mido.ports.BaseOutput, note: int, vel: int) -> None:
    port.send(mido.Message("note_on", channel=CH, note=clamp7(note), velocity=clamp7(vel)))

def note_off(port: mido.ports.BaseOutput, note: int) -> None:
    # note_on vel=0 (same behavior as the Swift code)
    note_on(port, note, 0)

def rand_int(limit: int) -> int:
    return 0 if limit <= 0 else random.randrange(limit)

def chance(base: float) -> bool:
    p = base * (0.25 + 0.75 * complexity)
    p = max(0.0, min(1.0, p))
    return random.random() < p

def sleep_for_step(i: int) -> None:
    # Matches the Swift timing
    if i in (2, 5):
        micros = (60_000_000 * 1) / (tempo * 3)
    elif i in (0, 3):
        micros = (60_000_000 * 4) / (tempo * 15)
    else:
        micros = (60_000_000 * 2) / (tempo * 5)
    time.sleep(micros / 1_000_000.0)

# ---------------------------
# Musical data (same as Swift)
# ---------------------------

piano: List[List[Optional[int]]] = [
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

piano_note = 64
piano_chords = [[4, 10, 14], [-2, 4, 9]]

bass = [0, 5, -2, 3, -4, 1, 6, -1, 4, -3, 2, -5]
bass_idx = 0

ride: List[List[bool]] = [
    [False, False, False, False, False, False],
    [False, False, False, False, False, True],
    [False, False, False, False, True,  True],
    [False, False, True,  False, True,  True],
]


chord = piano_chords[bass_idx % 2]

# Drum note numbers (you used 54 and 52; these are not standard GM mappings,
# but we keep them to match your original behavior).
RIDE_NOTE = 51   # standard ride
HH_NOTE   = 42   # standard closed hat
KICK_NOTE = 36
# ---------------------------
# Main loop
# ---------------------------

def main() -> None:
    global piano_note, bass_idx, chord

    random.seed(seed)

    while True:
        piano_pattern = rand_int(len(piano))
        if piano_pattern == 0:
            piano_note += 5

        max_ride_index = len(ride) - 1
        biased_ride = int(round(max_ride_index * complexity))
        ride_pattern = max(0, min(max_ride_index, biased_ride + rand_int(2) - 1))

        for i in range(6):
            # Piano melody
            note_delta = piano[piano_pattern][i]
            if PLAY_PIANO and note_delta is not None and chance(0.95):
                # turn off current pitch (and octaves) before moving
                note_off(piano_out, piano_note + 12)
                note_off(piano_out, piano_note - 12)
                note_off(piano_out, piano_note)

                piano_note += note_delta
                print("piano:", piano_note)

                vel = 35 + rand_int(60)
                note_on(piano_out, piano_note, vel)

            # Piano chords
            if PLAY_PIANO and i == 1 and chance(0.25):
                for n in chord:
                    note_off(piano_out, n + bass[bass_idx] + 48)
                chord_vel = 30 + rand_int(50)
                for n in chord:
                    note_on(piano_out, n + bass[bass_idx] + 48, chord_vel)

            if i == 4:
                if bass_idx < 6:
                    chord = piano_chords[bass_idx % 2]
                else:
                    chord = piano_chords[1 - (bass_idx % 2)]
                chord_vel = 20 + rand_int(50)
                for n in chord:
                    note_on(piano_out, n + bass[bass_idx] + 48, chord_vel)

            # Drums (separate port)
            if PLAY_DRUMS and ride[ride_pattern][i]:
                note_on(drum_out, RIDE_NOTE, 20 + rand_int(40))

                note_off(drum_out, RIDE_NOTE)

            if PLAY_DRUMS and i == 2 and chance(0.9):
                note_on(drum_out, HH_NOTE, 30 + rand_int(40))
                note_off(drum_out, HH_NOTE)

            # Bass (separate port)
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

            # chord off each step (matches Swift)
            for n in chord:
                note_off(piano_out, n + bass[bass_idx] + 48)

        # keep piano in range
        if piano_note > 90:
            piano_note -= 12
        elif piano_note < 60:
            piano_note += 12
        else:
            piano_note -= 12 * rand_int(2)

def panic_off() -> None:
    # Best-effort note off sweep
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