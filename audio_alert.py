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


def generate_alert_tone(duration: float = 5.0, beep_freq: int = 880, sample_rate: int = 44100) -> bytes:
    """
    Returns WAV file bytes for a repeating alert beep pattern lasting the
    full `duration` (default 5 seconds) - a short beep followed by a
    short silence, repeated, so a 5-second alert reads as an actual
    attention signal rather than one long continuous tone.
    """
    beep_len = 0.35
    gap_len = 0.35
    cycle_len = beep_len + gap_len
    n_cycles = max(1, round(duration / cycle_len))

    frames = []
    for _ in range(n_cycles):
        beep_samples = int(sample_rate * beep_len)
        for i in range(beep_samples):
            t = i / sample_rate
            amplitude = 0.5 * math.sin(2 * math.pi * beep_freq * t)
            fade = min(1.0, i / 150, (beep_samples - i) / 150)
            sample = int(amplitude * fade * 32767)
            frames.append(struct.pack("<h", sample))
        gap_samples = int(sample_rate * gap_len)
        frames.append(struct.pack("<h", 0) * gap_samples)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"".join(frames))
    return buffer.getvalue()


def alert_audio_html(wav_bytes: bytes) -> str:
    """
    Returns a self-contained, INVISIBLE HTML5 <audio> tag that autoplays
    the alert tone - no play/pause controls shown, so it plays
    automatically on the page right after "Run compliance analysis" and
    nothing else. If the browser blocks autoplay entirely, nothing
    audible happens (no visible fallback control by design, per the
    officer's request) - the on-screen warning banner text is still
    always shown regardless, so the alert is never silent AND invisible
    at the same time.
    """
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return f"""
    <audio autoplay style="display:none;">
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
