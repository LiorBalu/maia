"""Server-side audio preprocessing for telephony streams.

Applies WebRTC Audio Processing Module (APM) — the same module that powers
Google Meet / Chrome WebRTC — for noise suppression, automatic gain control,
and high-pass filtering. Optionally gates non-speech frames based on WebRTC's
built-in VAD, preventing the voice model (OpenAI Realtime) from
misinterpreting background noise as speech and interrupting the agent.

Operates natively on Twilio's audio format (μ-law 8kHz, 20ms / 160-byte frames).
Per-frame overhead is ~2-5 ms on a modern CPU core.

This module is import-safe: if the optional `webrtc-noise-gain` package is not
installed, `build_preprocessor_from_settings` will return None and callers pass
audio through unchanged.
"""

from __future__ import annotations

import audioop
import base64
import logging
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


# μ-law digital silence byte per G.711 spec (Twilio uses 0xFF for silence).
_MULAW_SILENCE_BYTE = b"\xff"

# Twilio frame: 20ms @ 8kHz = 160 samples * 1 byte/sample = 160 bytes.
# After decoding to PCM16: 160 samples * 2 bytes = 320 bytes.
# After upsampling to 16kHz: 320 samples * 2 bytes = 640 bytes = 2 APM 10ms frames.
_APM_FRAME_BYTES_16K = 320  # 10ms @ 16kHz, int16 mono


try:
    from webrtc_noise_gain import AudioProcessor as _WebRtcApm  # type: ignore

    _APM_AVAILABLE = True
except Exception as _apm_err:  # ImportError or missing wheel
    _WebRtcApm = None  # type: ignore
    _APM_AVAILABLE = False
    logger.info(
        "webrtc-noise-gain not available (%s); call-level noise suppression will be disabled "
        "even if toggled on in settings.",
        _apm_err,
    )


class CallAudioPreprocessor:
    """Per-call audio preprocessor for Twilio μ-law 8 kHz streams.

    Pipeline per 20 ms Twilio frame (each stage individually toggleable):

        μ-law 8k → PCM16 8k → upsample 16k → WebRTC APM
                    (noise suppression + AGC + HPF + built-in VAD)
                 → downsample 8k → VAD gate (mute non-speech + hangover)
                 → PCM16 8k → μ-law 8k

    If every stage is disabled the instance returns the input payload unchanged
    (zero-copy fast path).

    Not thread-safe; construct one instance per call.
    """

    def __init__(
        self,
        *,
        noise_suppression: bool = False,
        noise_suppression_level: int = 2,
        auto_gain: bool = False,
        auto_gain_dbfs: int = -3,
        vad_gate: bool = False,
        vad_hangover_ms: int = 400,
        rms_floor_dbfs: int = 0,
        speech_frames_to_open: int = 2,
        speech_subframes_to_open: int = 2,
    ) -> None:
        # Clamp to WebRTC APM's supported ranges.
        """Clamp the settings to the APM ranges, prepare the gate counters and the resampler state, and create the
        WebRTC APM when any stage is on.
        """
        self.noise_suppression = bool(noise_suppression)
        self.noise_suppression_level = max(0, min(4, int(noise_suppression_level)))
        self.auto_gain = bool(auto_gain)
        self.auto_gain_dbfs = max(-31, min(31, int(auto_gain_dbfs)))
        self.vad_gate = bool(vad_gate)
        self._hangover_frames_total = max(1, int(vad_hangover_ms) // 20)
        self._hangover_frames_remaining = 0
        # Require a short run of consecutive speech-positive frames before we
        # open the gate. This blocks brief false-positive VAD blips caused by
        # clicks / bumps / short noise bursts from releasing 400-500ms of noise.
        self.speech_frames_to_open = max(1, int(speech_frames_to_open))
        self._speech_run_frames = 0
        # Twilio frames are 20ms, but the APM runs on 10ms chunks. Requiring
        # both 10ms subframes inside the current 20ms frame to look like speech
        # is much better at rejecting transient background bursts while adding
        # no extra frame latency.
        self.speech_subframes_to_open = max(1, int(speech_subframes_to_open))
        # RMS floor gate: if the frame's RMS is below this dBFS level, treat as
        # silence regardless of what WebRTC VAD says. Catches steady low-level
        # noise (hum, fan, traffic) that APM sometimes lets through. 0 = off.
        self.rms_floor_dbfs = int(rms_floor_dbfs)
        # int16 full scale RMS. dBFS = 20*log10(rms/32768).
        # Cache the linear threshold to avoid per-frame log math.
        if self.rms_floor_dbfs < 0:
            self._rms_floor_linear = int(32768 * (10 ** (self.rms_floor_dbfs / 20.0)))
        else:
            self._rms_floor_linear = 0

        # audioop.ratecv state (kept across frames so the resampler doesn't
        # produce clicks at frame boundaries).
        self._upsample_state = None
        self._downsample_state = None

        # Stats (for optional logging at call end).
        self.frames_processed = 0
        self.frames_gated = 0
        # Per-frame processing time in microseconds, for latency profiling.
        # 20ms frames → 3000 samples per minute — cheap to keep in memory.
        self._frame_latencies_us: List[int] = []

        self._apm = None
        need_apm = self.noise_suppression or self.auto_gain or self.vad_gate
        if need_apm and _APM_AVAILABLE:
            try:
                self._apm = _WebRtcApm(
                    self.auto_gain_dbfs if self.auto_gain else 0,
                    self.noise_suppression_level if self.noise_suppression else 0,
                )
            except Exception as e:
                logger.error("Failed to initialise WebRTC APM: %s — disabling preprocessing", e)
                self._apm = None

        # If caller asked for a feature but APM init failed, treat as disabled
        # rather than silently dropping audio.
        self.enabled = self._apm is not None

    # ------------------------------------------------------------------ public API

    def process(self, ulaw_b64: str) -> str:
        """Process one Twilio 20ms μ-law base64 frame.

        Always returns a base64 μ-law payload of the same length as the input.
        When gated, returns μ-law silence; Twilio-side framing is preserved so
        the downstream voice AI continues to see a continuous stream.
        """
        if not self.enabled or self._apm is None:
            return ulaw_b64

        _t0 = time.perf_counter_ns()
        try:
            return self._process_inner(ulaw_b64)
        finally:
            # Cheap — monotonic counter, single append.
            self._frame_latencies_us.append((time.perf_counter_ns() - _t0) // 1000)

    def _process_inner(self, ulaw_b64: str) -> str:
        """The pipeline for one frame: decode mu-law, upsample, run the APM per 10 ms chunk (collecting the VAD
        verdict), downsample, apply the VAD / RMS gate, re-encode mu-law and keep the frame size.
        """
        try:
            ulaw_in = base64.b64decode(ulaw_b64)
        except Exception:
            return ulaw_b64  # malformed input — passthrough

        original_len = len(ulaw_in)
        if original_len == 0:
            return ulaw_b64

        # 1. μ-law → PCM16 @ 8 kHz
        pcm_8k = audioop.ulaw2lin(ulaw_in, 2)

        # 2. Upsample 8k → 16k (APM requires 16 kHz)
        pcm_16k, self._upsample_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, self._upsample_state)

        # 3. Run APM over each 10 ms (320-byte) sub-frame. Collect VAD verdict.
        processed = bytearray()
        speech_subframes = 0
        for offset in range(0, len(pcm_16k), _APM_FRAME_BYTES_16K):
            chunk = bytes(pcm_16k[offset : offset + _APM_FRAME_BYTES_16K])
            if len(chunk) != _APM_FRAME_BYTES_16K:
                # Trailing remainder from resampler — skip APM and keep as-is.
                processed.extend(chunk)
                continue
            try:
                result = self._apm.Process10ms(chunk)
            except Exception as e:
                logger.warning("APM Process10ms failed: %s", e)
                processed.extend(chunk)
                continue
            processed.extend(result.audio)
            if getattr(result, "is_speech", False):
                speech_subframes += 1

        # 4. Downsample 16k → 8k
        pcm_out_8k, self._downsample_state = audioop.ratecv(bytes(processed), 2, 1, 16000, 8000, self._downsample_state)

        self.frames_processed += 1

        # 5. VAD gate: silence non-speech frames (with hangover so we don't
        #    clip the tail of a word). Optionally combined with an RMS floor
        #    that overrides APM's VAD for quiet frames.
        if self.vad_gate:
            below_floor = False
            if self._rms_floor_linear > 0:
                try:
                    frame_rms = audioop.rms(pcm_out_8k, 2)
                    if frame_rms < self._rms_floor_linear:
                        below_floor = True
                except Exception:
                    pass
            raw_speech = speech_subframes >= self.speech_subframes_to_open and not below_floor
            if raw_speech:
                self._speech_run_frames += 1
            else:
                self._speech_run_frames = 0

            gate_open_speech = self._speech_run_frames >= self.speech_frames_to_open
            if gate_open_speech:
                self._hangover_frames_remaining = self._hangover_frames_total
            elif self._hangover_frames_remaining > 0:
                self._hangover_frames_remaining -= 1
            else:
                self.frames_gated += 1
                silent = _MULAW_SILENCE_BYTE * original_len
                return base64.b64encode(silent).decode("ascii")

        # 6. PCM16 → μ-law
        ulaw_out = audioop.lin2ulaw(pcm_out_8k, 2)

        # Twilio expects fixed-size frames; the resampler may
        # occasionally produce ±1 sample. Pad or truncate to preserve framing.
        if len(ulaw_out) != original_len:
            if len(ulaw_out) < original_len:
                ulaw_out = ulaw_out + _MULAW_SILENCE_BYTE * (original_len - len(ulaw_out))
            else:
                ulaw_out = ulaw_out[:original_len]

        return base64.b64encode(ulaw_out).decode("ascii")

    def stats_summary(self) -> str:
        """One-line summary of the call: frames processed / gated, latency percentiles and the active settings."""
        if self.frames_processed == 0:
            return "audio-preproc: no frames processed"
        gated_pct = 100.0 * self.frames_gated / self.frames_processed
        latency = self._latency_summary()
        return (
            f"audio-preproc: frames={self.frames_processed} "
            f"gated={self.frames_gated} ({gated_pct:.1f}%) "
            f"{latency} "
            f"ns={self.noise_suppression}(lvl{self.noise_suppression_level}) "
            f"agc={self.auto_gain} vad_gate={self.vad_gate} "
            f"open_after_frames={self.speech_frames_to_open} "
            f"open_after_subframes={self.speech_subframes_to_open}"
        )

    def _latency_summary(self) -> str:
        """Per-frame processing latency: mean, p50, p95, p99 and max, in microseconds."""
        samples = self._frame_latencies_us
        if not samples:
            return "latency=n/a"
        ordered = sorted(samples)
        n = len(ordered)

        def _pct(p: float) -> int:
            # Nearest-rank percentile — fine for call telemetry.
            """Nearest-rank percentile of the sorted latencies."""
            idx = min(n - 1, max(0, int(round(p * (n - 1)))))
            return ordered[idx]

        mean_us = sum(ordered) // n
        return (
            f"latency_us(mean={mean_us} " f"p50={_pct(0.50)} p95={_pct(0.95)} " f"p99={_pct(0.99)} max={ordered[-1]})"
        )


# ---------------------------------------------------------------------- factory


def build_preprocessor_from_settings(call_settings) -> Optional[CallAudioPreprocessor]:
    """Construct a CallAudioPreprocessor from a CallSettingsDocument.

    Controlled by the single ``noise_suppression`` feature toggle. All tuning
    lives in ``feature.meta_data`` and can be edited per-tenant from the UI
    (same JSON-editing pattern as chat features). Missing keys fall back to
    strong defaults tuned for Twilio μ-law 8 kHz + the OpenAI Realtime VAD:

      * WebRTC APM noise suppression at level 3 (high).
      * VAD gate ON with 500 ms hangover.
      * RMS floor gate at -45 dBFS (steady low-level background noise →
        forced silence even if APM VAD would let it through).
      * AGC OFF by default.

    Returns None when the feature is off, the call has no settings, or the
    underlying APM wheel is unavailable.
    """
    if call_settings is None:
        return None

    feature = None
    try:
        feature = call_settings.get_feature("noise_suppression")
    except Exception as e:
        logger.debug("get_feature('noise_suppression') failed: %s", e)
        return None

    if feature is None or not bool(getattr(feature, "enabled", False)):
        return None

    if not _APM_AVAILABLE:
        logger.warning(
            "Feature noise_suppression=ON for tenant=%s but webrtc-noise-gain is not " "installed; skipping.",
            getattr(call_settings, "tenant_name", "?"),
        )
        return None

    # Read overrides from feature.meta_data (editable per-tenant in UI).
    meta = getattr(feature, "meta_data", None) or {}

    def _get(key, default):
        """Value of a meta_data key, or the default when it is missing or None."""
        val = meta.get(key, default)
        return default if val is None else val

    ns_level = int(_get("noise_suppression_level", 3))
    vad_gate_enabled = bool(_get("vad_gate_enabled", True))
    vad_hangover_ms = int(_get("vad_hangover_ms", 500))
    rms_floor_dbfs = int(_get("rms_floor_dbfs", -45))
    speech_frames_to_open = int(_get("speech_frames_to_open", 1))
    speech_subframes_to_open = int(_get("speech_subframes_to_open", 2))
    auto_gain_enabled = bool(_get("auto_gain_enabled", False))
    auto_gain_dbfs = int(_get("auto_gain_dbfs", -3))

    try:
        pre = CallAudioPreprocessor(
            noise_suppression=ns_level > 0,
            noise_suppression_level=ns_level,
            auto_gain=auto_gain_enabled,
            auto_gain_dbfs=auto_gain_dbfs,
            vad_gate=vad_gate_enabled,
            vad_hangover_ms=vad_hangover_ms,
            rms_floor_dbfs=rms_floor_dbfs,
            speech_frames_to_open=speech_frames_to_open,
            speech_subframes_to_open=speech_subframes_to_open,
        )
    except Exception as e:
        logger.error("Failed to build CallAudioPreprocessor: %s", e)
        return None

    if not pre.enabled:
        return None

    logger.info(
        "Noise suppression active for tenant=%s (APM lvl%d, vad_gate=%s, hangover=%dms, "
        "rms_floor=%ddBFS, open_after_frames=%d, open_after_subframes=%d, agc=%s)",
        getattr(call_settings, "tenant_name", "?"),
        ns_level,
        vad_gate_enabled,
        vad_hangover_ms,
        rms_floor_dbfs,
        speech_frames_to_open,
        speech_subframes_to_open,
        auto_gain_enabled,
    )
    logger.info(f"NOISE_SUPPRESSION_ACTIVE tenant={getattr(call_settings, 'tenant_name', '?')} "
        f"mode=webrtc_apm ns_level={ns_level} vad_gate={vad_gate_enabled} "
        f"hangover_ms={vad_hangover_ms} rms_floor_dbfs={rms_floor_dbfs} "
        f"open_after_frames={speech_frames_to_open} "
        f"open_after_subframes={speech_subframes_to_open} "
        f"agc={auto_gain_enabled}")
    return pre
