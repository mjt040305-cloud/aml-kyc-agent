"""Security alert helpers for the AML/KYC Streamlit application.

Threshold alerts are intentionally separate from the AML risk score: a
threshold breach is an event that requires human compliance attention, not
an automatic finding of money laundering.
"""
import base64
import io
import math
import struct
import wave


def threshold_breaches(transactions, threshold_usd):
    """Return transactions whose normalized USD amount meets/exceeds threshold."""
    breaches = []
    for txn in transactions or []:
        amount = float(txn.get("amount", txn.get("usd_equivalent", 0)) or 0)
        if amount >= float(threshold_usd):
            breaches.append(txn)
    return breaches


def make_alert_wav(duration=0.55, sample_rate=22050):
    """Create a short two-tone WAV in memory; no external sound file is needed."""
    frames = bytearray()
    total = int(duration * sample_rate)
    for i in range(total):
        t = i / sample_rate
        # Two alternating tones for a recognizable compliance alert.
        freq = 880 if int(t * 4) % 2 == 0 else 660
        envelope = min(1.0, i / (0.02 * sample_rate), (total - i) / (0.04 * sample_rate))
        value = int(12000 * envelope * math.sin(2 * math.pi * freq * t))
        frames += struct.pack("<h", value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buf.getvalue()


def alert_audio_html(autoplay=False):
    """Return HTML audio player. Browser autoplay may be blocked; controls remain available."""
    encoded = base64.b64encode(make_alert_wav()).decode("ascii")
    auto = " autoplay" if autoplay else ""
    return (
        f'<audio controls{auto} preload="auto" style="width:100%;">'
        f'<source src="data:audio/wav;base64,{encoded}" type="audio/wav">'
        "Your browser does not support audio playback."
        "</audio>"
    )
