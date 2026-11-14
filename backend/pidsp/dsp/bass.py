"""Bass management for a 2.1 system: crossover, sub delay and polarity, levels.

This is stage A of a PiDSP calibration. Left, right and sub are measured raw,
with no crossover, one after the other in ONE recording (excitation(channels=3)).
Every sweep is deconvolved against the exact digital sweep, and the impulse
response keeps its time zero at the moment that sweep started playing. So
arrival times are comparable between channels, even though the microphone
(UMIK-1) and the sound card run on separate clocks.

From those raw responses:

  low_extension()     where the mains are 6 dB down, in the room
  align()             L/R delay and level; then, for every standard crossover
                      the speakers allow, the sub's delay, polarity and level
                      and a score for the predicted result at the seat; the
                      best scoring crossover wins (crossover_options() shows
                      them all)
  predicted_sum()     what the result should measure like, for the UI and tests

Stage B (room correction) then measures "left + sub" and "right + sub" with
this plan active and fits the FIR correction with pidsp.dsp.calibration.
"""

import math

import numpy as np
from scipy import signal

from . import calibration as cal

FS = cal.FS
GRID = cal.GRID

# Crossover frequencies offered, Hz. LR4 (24 dB/oct) throughout.
CROSSOVERS = (60, 70, 80, 90, 100, 110, 120)
XO_ORDER = 4
# How long a slice of each impulse response is kept for the bass analysis.
# Long enough for the lowest crossover's band, short enough to keep noise low.
WINDOW_SECONDS = 0.3
# Sub delay is searched over +/- this. A sub's own DSP often adds 5 to 20 ms.
MAX_DELAY_MS = 30.0
DELAY_STEP_MS = 0.05
# Solutions within this many dB of the best summation count as equally good;
# among them the one nearest the delay that lines up the bass energy of
# mains and sub is chosen (envelope_lag_ms()).
SUM_TOLERANCE_DB = 0.3
# Sub level trim limits. The sub's own volume knob covers anything beyond.
# The cut goes deep because some subs (the DB4S) can only be turned down 10 dB
# in their own app, and a sub next to the seat measures far louder than the
# mains.
MAX_SUB_BOOST_DB = 6.0
MAX_SUB_CUT_DB = -30.0
# The sub's stage A sweep: its own band only, so the time goes where it plays.
# The top leaves the fade-out clear of 2 x the highest crossover.
SUB_SWEEP_HZ = (10.0, 300.0)

# Choosing the crossover. A candidate must sit at least MAINS_MIN_RATIO above
# where the mains are 6 dB down, and SUB_MAX_RATIO below where the sub is.
MAINS_MIN_RATIO = 1.25
SUB_MAX_RATIO = 1.0
# Score, in dB (lower is better):
#   bumpiness of the predicted bass at the seat, FLAT_BAND, both sides
#   + SUMMATION_WEIGHT x the summation loss through the crossover band
#   + STRAIN_DB_PER_OCTAVE for each octave the crossover sits below
#     2 x the mains' -6 dB point (the mains play deeper, with more excursion)
#   + LOCALISATION_DB_PER_OCTAVE for each octave above LOCALISATION_HZ
#     (the higher the sub plays, the easier it is to hear where it stands,
#     which matters most for a sub away from the main speakers)
# Room correction later flattens peaks, but cannot fix a cancellation at the
# crossover, hence the extra weight on summation.
FLAT_BAND = (25.0, 250.0)
SUMMATION_WEIGHT = 2.0
STRAIN_DB_PER_OCTAVE = 1.5
LOCALISATION_HZ = 80.0
LOCALISATION_DB_PER_OCTAVE = 2.0
# Candidates within this of the best score are equally good; among them the
# one nearest the classic choice (an octave above the mains' -6 dB) wins.
SCORE_TIE_DB = 0.25

LEFT, RIGHT, SUB = 0, 1, 2


# ---------- Responses ----------

def complex_responses(recording, played, schedule, fs=FS, window=WINDOW_SECONDS):
    """Complex transfer function of each channel on GRID, from one recording.

    played/schedule come from calibration.excitation(channels=3). Each repeat
    is a coherent estimate (same position), so repeats are averaged complex.
    Time zero is the start of each sweep in the playback, so the phase carries
    the true arrival time, comparable across channels.

    Returns a list per channel of dicts: "h" (complex on GRID), "arrival" (s,
    peak of the impulse response), "snr" (dB on GRID). Raises
    calibration.MeasurementError when the recording cannot be trusted.
    """
    rec = np.asarray(recording, dtype=np.float64)
    if rec.ndim > 1:
        rec = rec.mean(axis=1)
    if np.max(np.abs(rec)) > 0.98:
        raise cal.MeasurementError(
            "The microphone was overloaded. Turn the volume down a little and measure again.")
    # Locate the playback by the mains only: their full-range sweeps give a
    # sharp match, while the sub's bass-only sweep (and the sub's own delay)
    # could pull a match over all channels tens of milliseconds late.
    offset = cal.locate(rec, played[:, :min(2, played.shape[1])].mean(axis=1))
    sweep_len = int(cal.SWEEP_SECONDS * fs)
    length = int(window * fs)
    channels = played.shape[1]
    margin = int(0.02 * fs)
    lead = int(cal.LEAD_SECONDS * fs)
    quiet = rec[offset + int(0.1 * fs):offset + lead - int(0.1 * fs)]

    def spectrum(seg, exc):
        h, n = cal.transfer(seg, exc, fs)
        ir = np.fft.irfft(h, n)[:margin + length]
        fall = length // 3
        ir[-fall:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, fall))
        nfft = 1 << int(math.ceil(math.log2(len(ir) * 4)))
        freqs = np.fft.rfftfreq(nfft, 1 / fs)
        spec = np.fft.rfft(ir, nfft)
        # Undo the early start, so time zero is the sweep's own start.
        spec *= np.exp(2j * np.pi * freqs * margin / fs)
        re = np.interp(GRID, freqs, spec.real)
        im = np.interp(GRID, freqs, spec.imag)
        return re + 1j * im, ir

    per = [{"hs": [], "arrivals": [], "exc": None} for _ in range(channels)]
    for ch, _r, start in schedule:
        a = offset + start - margin
        segment = rec[a:a + sweep_len + length + margin]
        if a < 0 or len(segment) < sweep_len + length + margin:
            raise cal.MeasurementError("The recording does not cover every sweep; measure again.")
        exc = played[start:start + sweep_len, ch].astype(np.float64)
        h, ir = spectrum(segment, exc)
        per[ch]["hs"].append(h)
        per[ch]["arrivals"].append((int(np.argmax(np.abs(ir))) - margin) / fs)
        per[ch]["exc"] = exc

    out = []
    for ch in range(channels):
        h = np.mean(per[ch]["hs"], axis=0)
        noise_seg = np.resize(quiet, sweep_len + length + margin) if len(quiet) else np.zeros(sweep_len + length + margin)
        noise, _ = spectrum(noise_seg, per[ch]["exc"])
        snr = cal.smooth(db(h), 3) - cal.smooth(db(noise), 3)
        out.append({"h": h, "arrival": float(np.median(per[ch]["arrivals"])), "snr": snr})
    _check_snr(out)
    return out


def _check_snr(responses):
    names = ["left", "right", "sub"]
    for ch, r in enumerate(responses):
        band = (GRID >= 30) & (GRID <= 80) if ch == SUB else (GRID >= 200) & (GRID <= 2000)
        if np.median(r["snr"][band]) < 15:
            raise cal.MeasurementError(
                f"The {names[ch]} speaker was barely heard over the room noise. Check it is "
                "connected and switched on, make it quiet, and measure again.")


def db(h):
    return 20 * np.log10(np.abs(h) + 1e-12)


# ---------- Crossover ----------

def biquad(kind, freq, gain=0.0, q=0.707, fs=FS):
    """RBJ cookbook coefficients (b, a): calibration.biquad plus a Lowpass."""
    if kind != "Lowpass":
        return cal.biquad(kind, freq, gain, q, fs)
    w0 = 2 * np.pi * freq / fs
    cw, alpha = np.cos(w0), np.sin(w0) / (2 * q)
    return np.array([(1 - cw) / 2, 1 - cw, (1 - cw) / 2]), np.array([1 + alpha, -2 * cw, 1 - alpha])


def lr_response(kind, freq, order=XO_ORDER, fs=FS, freqs=GRID):
    """Complex response of a Linkwitz-Riley filter as CamillaDSP builds it:
    two cascaded Butterworth filters of half the order (order 4 = two Q 0.707
    RBJ biquads)."""
    if order != 4:
        raise ValueError("only LR4 is implemented")
    b, a = biquad("Highpass" if kind == "highpass" else "Lowpass", freq, q=1 / math.sqrt(2), fs=fs)
    _, h = signal.freqz(b, a, worN=freqs, fs=fs)
    return h * h


def low_extension(h, ref_band=(200.0, 2000.0), drop_db=6.0):
    """Where a speaker in the room has fallen drop_db below its midband level.

    Smoothed over half an octave so a single room-mode dip does not count as
    the end of the bass. Searches down from the bottom of ref_band; returns
    GRID[0] when the speaker never falls that far inside the measured range.
    """
    sm = cal.smooth(db(h), 2)
    band = (GRID >= ref_band[0]) & (GRID <= ref_band[1])
    ref = float(np.mean(sm[band]))
    i = int(np.argmax(GRID >= ref_band[0]))
    while i > 0 and sm[i] >= ref - drop_db:
        i -= 1
    if i == 0:
        return float(GRID[0])
    # Interpolate between the two grid points straddling the threshold.
    x0, x1 = math.log(GRID[i]), math.log(GRID[i + 1])
    y0, y1 = sm[i], sm[i + 1]
    t = (ref - drop_db - y0) / (y1 - y0) if y1 != y0 else 0.0
    return float(math.exp(x0 + t * (x1 - x0)))


def high_extension(h, ref_band=(25.0, 60.0), drop_db=6.0):
    """Where a subwoofer has fallen drop_db below its own bass level, going up.

    The bass level is the median over ref_band, so a room mode peaking in that
    band does not raise it and make the sub look as if it gave up early."""
    sm = cal.smooth(db(h), 2)
    band = (GRID >= ref_band[0]) & (GRID <= ref_band[1])
    ref = float(np.median(sm[band]))
    i = int(np.argmax(GRID > ref_band[1]))
    while i < len(GRID) - 1 and sm[i] >= ref - drop_db:
        i += 1
    return float(GRID[i])


def choose_crossover(mains_f6, sub_f6_high=None):
    """The lowest standard crossover about an octave above where the mains are
    6 dB down, so the high-pass relieves them where they struggle. It is kept
    a quarter below where the sub itself is 6 dB down, if known, so the sub
    still plays fully through the crossover."""
    want = max(CROSSOVERS[0], 2.0 * mains_f6)
    fc = next((f for f in CROSSOVERS if f >= want - 0.5), CROSSOVERS[-1])
    if sub_f6_high:
        ceiling = sub_f6_high / 1.25
        allowed = [f for f in CROSSOVERS if f <= ceiling]
        if allowed and fc > allowed[-1]:
            fc = allowed[-1]
    return fc


# ---------- Alignment ----------

def _delayed(h, delay_ms, freqs=GRID):
    return h * np.exp(-2j * np.pi * freqs * delay_ms / 1000.0)


def _xo_band(fc):
    return (GRID >= fc / 2) & (GRID <= fc * 2)


def summation_db(h_main, h_sub, fc):
    """How well filtered mains and sub add up through the crossover band:
    the mean of |main + sub| / (|main| + |sub|) in dB. 0 means perfectly in
    phase everywhere; cancellations make it strongly negative."""
    band = _xo_band(fc)
    ratio = np.abs(h_main[band] + h_sub[band]) / (np.abs(h_main[band]) + np.abs(h_sub[band]) + 1e-20)
    return float(np.mean(20 * np.log10(ratio + 1e-6)))


def _group_delay_at(h, fc, freqs=GRID):
    """Group delay in ms around fc, power-weighted over a third of an octave."""
    sel = (freqs >= fc * 2 ** (-1 / 6)) & (freqs <= fc * 2 ** (1 / 6))
    idx = np.where(sel)[0]
    lo, hi = max(idx[0] - 1, 0), min(idx[-1] + 2, len(freqs))
    phase = np.unwrap(np.angle(h[lo:hi]))
    gd = -np.gradient(phase, 2 * np.pi * freqs[lo:hi]) * 1000
    w = np.abs(h[lo:hi]) ** 2
    return float(np.sum(gd * w) / np.sum(w))


_ENV_FS = 4000        # Hz: plenty for the bass band
_ENV_N = 8192         # about 2 s
_ENV_PRE = 400        # samples of negative time kept (100 ms)


def _bass_envelope(h):
    """Hilbert envelope of the impulse response for h (complex on GRID,
    already band-limited by the crossover filters), sampled at _ENV_FS."""
    f = np.fft.rfftfreq(_ENV_N, 1 / _ENV_FS)
    sel = (f >= GRID[0]) & (f <= 1000)
    spec = np.zeros(len(f), complex)
    spec[sel] = np.interp(f[sel], GRID, h.real) + 1j * np.interp(f[sel], GRID, h.imag)
    ir = np.roll(np.fft.irfft(spec, _ENV_N), _ENV_PRE)
    return np.abs(signal.hilbert(ir))


def envelope_lag_ms(h_main, h_sub):
    """How much later the mains' bass arrives than the sub's (ms, negative:
    the mains are earlier), from the cross-correlation of the two energy
    envelopes through the crossover. A whole envelope is matched, so it does
    not jump when a door or a mic move changes which room-mode hump is highest
    (the single impulse peak does, by a period or more)."""
    em, es = _bass_envelope(h_main), _bass_envelope(h_sub)
    xc = signal.correlate(em, es, mode="full", method="fft")
    k = int(np.argmax(xc))
    # Parabolic interpolation around the peak, for sub-sample precision.
    if 0 < k < len(xc) - 1:
        a, b, c = xc[k - 1], xc[k], xc[k + 1]
        k = k + 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else k
    return float((k - (_ENV_N - 1)) / _ENV_FS * 1000)


def _mains_alignment(responses, spread_db):
    """Left/right delay and level, independent of the crossover."""
    hl, hr = responses[LEFT]["h"], responses[RIGHT]["h"]
    # Arrival from the impulse peak (sharp for full-range speakers).
    al, ar = responses[LEFT]["arrival"], responses[RIGHT]["arrival"]
    latest = max(al, ar)
    delay = {"left": (latest - al) * 1000, "right": (latest - ar) * 1000}
    mid = (GRID >= 300) & (GRID <= 3000)
    lvl_l, lvl_r = float(np.mean(db(hl)[mid])), float(np.mean(db(hr)[mid]))
    quietest = min(lvl_l, lvl_r)
    trim = {
        "left": -(lvl_l - quietest) if lvl_l - quietest > spread_db else 0.0,
        "right": -(lvl_r - quietest) if lvl_r - quietest > spread_db else 0.0,
    }
    return delay, trim


def _plan_at(responses, fc, mains_delay, mains_trim, mains_f6, sub_f6):
    """The full plan for one crossover frequency: sub level, delay, polarity."""
    hl, hr, hs = (r["h"] for r in responses[:3])
    delay = {**mains_delay, "sub": 0.0}
    trim = dict(mains_trim)
    mid = (GRID >= 300) & (GRID <= 3000)

    # The mains as the sub will meet them: aligned, levelled, high-passed.
    hp, lp = lr_response("highpass", fc), lr_response("lowpass", fc)
    mains = 0.5 * (_delayed(hl, delay["left"]) * 10 ** (trim["left"] / 20)
                   + _delayed(hr, delay["right"]) * 10 ** (trim["right"] / 20))
    ref_level = float(np.mean(db(mains)[mid]))

    # Sub level: its own passband, below the crossover, to the mains' midband.
    sub_band = (GRID >= max(25.0, fc / 3)) & (GRID <= fc / 1.4)
    sub_level = float(np.mean(cal.smooth(db(hs), 3)[sub_band]))
    wanted = ref_level - sub_level
    trim["sub"] = float(np.clip(wanted, MAX_SUB_CUT_DB, MAX_SUB_BOOST_DB))
    warnings = []
    if wanted > MAX_SUB_BOOST_DB + 1:
        warnings.append(f"The subwoofer is {wanted:.0f} dB quieter than the speakers. "
                        "Turn its own volume up and measure again.")
    elif wanted < MAX_SUB_CUT_DB - 1:
        warnings.append(f"The subwoofer is {-wanted:.0f} dB louder than the speakers. "
                        "Turn its own volume down and measure again.")

    m = mains * hp
    s = hs * lp * 10 ** (trim["sub"] / 20)
    # The sub delay that lines up the bass energy of mains and sub at the
    # seat (positive delays the sub). The LR4 outputs are in phase, so the
    # crossover itself adds nothing; the speakers' own roll-offs and the
    # devices' latencies are all in the measured responses.
    expected = envelope_lag_ms(m, s)

    # Relative delay d (ms): positive delays the sub, negative delays the mains.
    # Every delay and both polarities at once, over the crossover band.
    band = _xo_band(fc)
    ds = np.arange(-MAX_DELAY_MS, MAX_DELAY_MS + DELAY_STEP_MS / 2, DELAY_STEP_MS)
    mb, sb = m[band], s[band]
    shifted = sb[None, :] * np.exp(-2j * np.pi * GRID[band][None, :] * ds[:, None] / 1000.0)
    denom = np.abs(mb)[None, :] + np.abs(sb)[None, :] + 1e-20
    scores = {pol: np.mean(20 * np.log10(np.abs(mb[None, :] + pol * shifted) / denom + 1e-6), axis=1)
              for pol in (1, -1)}
    best = max(float(v.max()) for v in scores.values())
    # Several delays a period apart sum about as well at the crossover; pick the
    # one nearest the expected delay, so bass notes start together.
    near = [(float(v[i]), float(ds[i]), pol) for pol, v in scores.items()
            for i in np.where(v >= best - SUM_TOLERANCE_DB)[0]]
    score, d, pol = min(near, key=lambda c: abs(c[1] - expected))

    if d >= 0:
        delay["sub"] = d
    else:
        delay["left"] -= d
        delay["right"] -= d
    # Never delay everything: take out the common part.
    common = min(delay.values())
    delay = {k: round(v - common, 3) for k, v in delay.items()}
    delay = {k: (v if v >= 0.02 else 0.0) for k, v in delay.items()}

    return {
        "crossover_hz": fc,
        "crossover_order": XO_ORDER,
        "mains_f6_hz": round(mains_f6, 1),
        "sub_f6_hz": round(sub_f6, 1),
        "summation_db": round(score, 2),
        "channels": {
            "left": {"delay_ms": delay["left"], "trim_db": round(trim["left"], 2), "inverted": False},
            "right": {"delay_ms": delay["right"], "trim_db": round(trim["right"], 2), "inverted": False},
            "sub": {"delay_ms": delay["sub"], "trim_db": round(trim["sub"], 2), "inverted": pol < 0},
        },
        "warnings": warnings,
    }


def bumpiness_db(h, band=FLAT_BAND):
    """How uneven a response is in band: third-octave smoothed, around its
    median, with dips counting cal.DIP_WEIGHT as much as peaks (RMS, dB)."""
    sel = (GRID >= band[0]) & (GRID <= band[1])
    sm = cal.smooth(db(h), 3)[sel]
    d = sm - np.median(sm)
    d = np.where(d > 0, d, cal.DIP_WEIGHT * d)
    return float(np.sqrt(np.mean(d ** 2)))


def _evaluate(responses, plan, mains_f6):
    fc = plan["crossover_hz"]
    flat = float(np.mean([bumpiness_db(predicted_sum(responses, plan, side)) for side in ("left", "right")]))
    loss = max(0.0, -plan["summation_db"])
    strain = max(0.0, math.log2(2.0 * mains_f6 / fc)) * STRAIN_DB_PER_OCTAVE
    local = max(0.0, math.log2(fc / LOCALISATION_HZ)) * LOCALISATION_DB_PER_OCTAVE
    return {"bumpiness_db": round(flat, 2), "summation_db": plan["summation_db"],
            "strain_db": round(strain, 2), "localisation_db": round(local, 2),
            "score": round(flat + SUMMATION_WEIGHT * loss + strain + local, 2)}


def align(responses, fc=None, spread_db=0.5, prefer=None):
    """The bass plan: crossover, and delay/level/polarity per output channel.

    responses: complex_responses() for left, right, sub.
    With fc=None every standard crossover the speakers allow is planned and
    scored (see the constants above), and the best one is returned, with all
    of them in plan["crossover_options"]. prefer: a crossover the listener
    picked from those options; it is used instead of the best one, if the
    speakers allow it. Only the earlier speakers are
    delayed, never all of them. Levels are only turned down, except that the
    sub may be raised up to MAX_SUB_BOOST_DB.
    """
    hl, hr, hs = (r["h"] for r in responses[:3])
    mains_f6 = min(low_extension(hl), low_extension(hr))
    sub_f6 = high_extension(hs)
    mains_delay, mains_trim = _mains_alignment(responses, spread_db)
    if fc is not None:
        return _plan_at(responses, fc, mains_delay, mains_trim, mains_f6, sub_f6)

    classic = choose_crossover(mains_f6, sub_f6)
    options, plans = [], {}
    for f in CROSSOVERS:
        opt = {"crossover_hz": f}
        if f < MAINS_MIN_RATIO * mains_f6 - 0.5:
            opt["excluded"] = f"too low for the main speakers (6 dB down at {mains_f6:.0f} Hz)"
        elif f > sub_f6 / SUB_MAX_RATIO + 0.5:
            opt["excluded"] = f"too high for the sub (6 dB down at {sub_f6:.0f} Hz)"
        else:
            plans[f] = _plan_at(responses, f, mains_delay, mains_trim, mains_f6, sub_f6)
            opt.update(_evaluate(responses, plans[f], mains_f6))
        options.append(opt)

    warnings = []
    if prefer in plans:
        plan = plans[prefer]
    elif plans:
        best = min(o["score"] for o in options if "score" in o)
        tied = [o["crossover_hz"] for o in options if "score" in o and o["score"] <= best + SCORE_TIE_DB]
        chosen = min(tied, key=lambda f: (abs(math.log2(f / (2.0 * mains_f6))), -f))
        plan = plans[chosen]
    else:
        plan = _plan_at(responses, classic, mains_delay, mains_trim, mains_f6, sub_f6)
        warnings.append("The main speakers and the sub barely overlap, so no crossover fits well. "
                        f"Using {classic} Hz. Check that the sub's own low-pass filter is off.")
        for o in options:
            if o["crossover_hz"] == classic:
                o.update(_evaluate(responses, plan, mains_f6))
    for o in options:
        o["chosen"] = o["crossover_hz"] == plan["crossover_hz"]
    return {**plan, "crossover_options": options, "crossover_choice": "listener" if prefer in plans else "auto",
            "warnings": warnings + plan["warnings"]}


def predicted_sum(responses, plan, side="left"):
    """Complex response of one main plus the sub at the seat, with the plan
    applied, as stage B will measure it when that input channel plays."""
    fc = plan["crossover_hz"]
    ch = plan["channels"]
    main = responses[LEFT if side == "left" else RIGHT]["h"]
    m = _delayed(main, ch[side]["delay_ms"]) * 10 ** (ch[side]["trim_db"] / 20) * lr_response("highpass", fc)
    sign = -1 if ch["sub"]["inverted"] else 1
    # In normal use the sub gets (L + R) / 2, which for mono bass (most bass)
    # is the full signal. Stage B measures one side at a time with a mixer
    # that feeds the sub that side at 0 dB (config.build(measure_side=...)),
    # so the correction is fitted for the level the sub really plays at.
    s = (sign * _delayed(responses[SUB]["h"], ch["sub"]["delay_ms"])
         * 10 ** (ch["sub"]["trim_db"] / 20) * lr_response("lowpass", fc))
    return m + s
