"""
audio_alert.py
---------------
Generates a short audible alert tone entirely in Python (stdlib `wave` +
`struct` only - no external audio file, no extra dependency, nothing
fetched from the internet). Used to sound an alert in the officer's
browser when a newly-analysed batch contains a transaction that breaches
the institution's configured structuring alert threshold.

This is a UX convenience only - it never substitutes for the actual
human review step. The transaction still goes through the normal
Step 4 checkpoint exactly as before; the sound is purely an attention
signal so the officer notices sooner.
"""

import io
import math
import struct
import wave


def generate_alert_tone(duration: float = 0.6, freq: int = 880, sample_rate: int = 44100) -> bytes:
    """
    Returns WAV file bytes for a short, clear two-tone alert beep
    (a common "attention" pattern: high-low-high) suitable for
    st.audio(..., autoplay=True).
    """
    frames = []
    n_samples = int(sample_rate * duration)
    for i in range(n_samples):
        t = i / sample_rate
        # Two-tone alternating pattern (like a simple alarm chirp) rather
        # than one flat tone, so it reads as an "alert" rather than a
        # generic notification ping.
        segment = int(t / (duration / 3))  # 3 segments: high, low, high
        this_freq = freq if segment != 1 else freq * 0.75
        amplitude = 0.5 * math.sin(2 * math.pi * this_freq * t)
        # Fade in/out slightly at the very start/end to avoid a harsh click
        fade = min(1.0, i / 200, (n_samples - i) / 200)
        sample = int(amplitude * fade * 32767)
        frames.append(struct.pack("<h", sample))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"".join(frames))
    return buffer.getvalue()


def transaction_breaches_threshold(txn: dict) -> bool:
    """
    True if this transaction's own triggered_rules include the
    institutional structuring/threshold rule - i.e. its USD-equivalent
    amount is at or above the institution's configured structuring alert
    threshold. Kept as its own function so app.py and any future caller
    checks this the same way everywhere.
    """
    return any(
        r.get("label") == "Large / structured transaction"
        for r in txn.get("triggered_rules", [])
    )
