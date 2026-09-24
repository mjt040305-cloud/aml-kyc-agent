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
import base64


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


def alert_audio_html(wav_bytes: bytes) -> str:
    """
    Returns a self-contained HTML5 <audio> tag for the alert tone, with
    BOTH autoplay attempted AND visible native controls shown.

    Some browsers silently block autoplay-with-sound even after a genuine
    user gesture (e.g. clicking "Run compliance analysis"), with no error
    surfaced anywhere - st.audio(..., autoplay=True) can fail exactly
    this way with no visible sign why. Showing native controls alongside
    the autoplay attempt means the officer always has a guaranteed,
    one-click way to hear the alert even when autoplay itself is blocked,
    rather than a silent alert that may or may not have actually played.
    """
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return f"""
    <audio autoplay controls style="width: 100%; height: 32px;">
        <source src="data:audio/wav;base64,{b64}" type="audio/wav">
    </audio>
    """


def transaction_triggered_any_rule(txn: dict) -> bool:
    """
    True if this transaction triggered ANY AML rule at all (Customer,
    Transaction, Geographic, or Behavioural) - i.e. it was flagged for
    review, regardless of which specific rule or how severe. This is the
    broad "something in this batch needs a look" alert; see
    transaction_breaches_threshold() for the narrower, threshold-specific
    check if that's what's wanted instead.
    """
    return bool(txn.get("triggered_rules"))


def transaction_breaches_threshold(txn: dict, threshold: float) -> bool:
    """
    True if this transaction's USD-equivalent amount is at or above the
    institution's configured structuring alert threshold. Compares the
    amount directly against the threshold rather than matching an exact
    rule-label string, so this stays correct even if the rules engine's
    wording for that rule ever changes.
    """
    amount = txn.get("amount", txn.get("usd_equivalent", 0))
    try:
        return float(amount) >= float(threshold)
    except (TypeError, ValueError):
        return False
