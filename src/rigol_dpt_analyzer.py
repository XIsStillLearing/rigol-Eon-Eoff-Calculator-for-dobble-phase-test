#!/usr/bin/env python3
"""
Parse Rigol RG01 binary waveforms and estimate DPT Eon/Eoff.

The script is written for the synchronous Buck DPT data used in this workspace:
Q1 high-side is the hard-switching device, Q2 low-side is synchronous freewheel,
and the default trace mapping is trace1=CH1, trace2=CH2(Vds-like), trace3=CH4(I).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover - plotting is optional.
    plt = None


@dataclass
class TraceHeader:
    trace_index: int
    header_offset: int
    data_offset: int
    header_size: int
    points: int
    data_bytes: int
    total_span_s: float
    date: str
    time: str
    model: str


@dataclass
class LossEvent:
    kind: str
    pulse: int
    idx: int
    start_idx: int
    end_idx: int
    center_us: float
    start_us: float
    end_us: float
    tv_ns: float
    i_edge_A: float
    vbus_V: float
    e_upper_uJ: float
    e_linear_uJ: float
    vdt_Vns: float


@dataclass
class PulseMetrics:
    pulse: int
    start_us: float
    end_us: float
    duration_us: float
    vbus_V: float
    i_start_A: float
    i_end_A: float
    di_A: float
    slope_A_per_us: float


@dataclass
class FileResult:
    file: str
    timestamp: str
    points: int
    trace_count: int
    total_span_us: float
    dt_ns: float
    vds_input: str
    current_mode: str
    headers: list[TraceHeader]
    stats: list[dict[str, float]]
    current_baseline_A: float
    pulses: list[PulseMetrics]
    losses: list[LossEvent]
    standard: dict[str, float | None]
    csv: str | None
    plot: str | None
    zoom: str | None


def _read_c_string(data: bytes, start: int, size: int) -> str:
    raw = data[start : start + size].split(b"\0", 1)[0]
    return raw.decode("ascii", errors="ignore")


def parse_rg01(path: Path) -> tuple[list[TraceHeader], list[np.ndarray]]:
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RG01":
        raise ValueError(f"{path} is not a Rigol RG01 binary file")

    trace_count = struct.unpack_from("<I", data, 8)[0]
    headers: list[TraceHeader] = []
    traces: list[np.ndarray] = []
    offset = 12

    for trace_idx in range(trace_count):
        if offset + 24 > len(data):
            raise ValueError(f"{path}: truncated trace header at offset {offset}")

        header_payload_size = struct.unpack_from("<I", data, offset)[0]
        header_size = header_payload_size + 12
        points = struct.unpack_from("<I", data, offset + 12)[0]
        total_span_s = struct.unpack_from("<f", data, offset + 20)[0]
        data_offset = offset + header_size
        data_bytes = points * 4
        data_end = data_offset + data_bytes

        if data_end > len(data):
            raise ValueError(
                f"{path}: trace {trace_idx + 1} data exceeds file length "
                f"({data_end} > {len(data)})"
            )

        headers.append(
            TraceHeader(
                trace_index=trace_idx + 1,
                header_offset=offset,
                data_offset=data_offset,
                header_size=header_size,
                points=points,
                data_bytes=data_bytes,
                total_span_s=total_span_s,
                date=_read_c_string(data, offset + 0x38, 16),
                time=_read_c_string(data, offset + 0x48, 16),
                model=_read_c_string(data, offset + 0x58, 48),
            )
        )
        traces.append(np.frombuffer(data, dtype="<f4", count=points, offset=data_offset).astype(np.float64))
        offset = data_end

    return headers, traces


def estimate_levels(v: np.ndarray) -> tuple[float, float, float]:
    """Estimate the two Vds levels even when the low-state duty ratio is small."""
    sample = v
    if v.size > 200_000:
        step = max(1, v.size // 200_000)
        sample = v[::step]

    c_low = float(np.percentile(sample, 1))
    c_high = float(np.percentile(sample, 99))
    if not math.isfinite(c_low) or not math.isfinite(c_high) or abs(c_high - c_low) < 1e-12:
        c_low = float(np.min(sample))
        c_high = float(np.max(sample))

    for _ in range(32):
        threshold = (c_low + c_high) / 2.0
        low_group = sample[sample <= threshold]
        high_group = sample[sample > threshold]
        if low_group.size == 0 or high_group.size == 0:
            break
        new_low = float(np.median(low_group))
        new_high = float(np.median(high_group))
        if abs(new_low - c_low) < 1e-9 and abs(new_high - c_high) < 1e-9:
            c_low, c_high = new_low, new_high
            break
        c_low, c_high = new_low, new_high

    if c_low > c_high:
        c_low, c_high = c_high, c_low
    threshold = (c_low + c_high) / 2.0
    return c_low, c_high, threshold


def fill_short_gaps(mask: np.ndarray, max_gap_samples: int) -> np.ndarray:
    if max_gap_samples <= 0 or mask.size == 0:
        return mask
    out = mask.copy()
    runs = boolean_runs(out)
    for value, start, end in runs:
        if (not value) and start > 0 and end < out.size and (end - start) <= max_gap_samples:
            out[start:end] = True
    return out


def boolean_runs(mask: np.ndarray) -> list[tuple[bool, int, int]]:
    if mask.size == 0:
        return []
    changes = np.flatnonzero(mask[1:] != mask[:-1]) + 1
    starts = np.r_[0, changes]
    ends = np.r_[changes, mask.size]
    return [(bool(mask[s]), int(s), int(e)) for s, e in zip(starts, ends)]


def detect_on_intervals(
    vds_q1: np.ndarray,
    dt_s: float,
    min_pulse_us: float,
    debounce_ns: float,
) -> tuple[list[tuple[int, int]], tuple[float, float, float]]:
    low, high, threshold = estimate_levels(vds_q1)
    mask = vds_q1 < threshold
    gap_samples = max(1, int(round(debounce_ns * 1e-9 / dt_s)))
    mask = fill_short_gaps(mask, gap_samples)
    min_samples = max(1, int(round(min_pulse_us * 1e-6 / dt_s)))

    intervals: list[tuple[int, int]] = []
    for value, start, end in boolean_runs(mask):
        if value and (end - start) >= min_samples:
            intervals.append((start, end - 1))
    return intervals, (low, high, threshold)


def local_vbus(vds_q1: np.ndarray, idx: int, dt_s: float, side: str, window_ns: float) -> float:
    n = max(3, int(round(window_ns * 1e-9 / dt_s)))
    guard = max(1, int(round(10e-9 / dt_s)))
    if side == "before":
        seg = vds_q1[max(0, idx - n) : max(0, idx - guard)]
    else:
        seg = vds_q1[min(vds_q1.size, idx + guard) : min(vds_q1.size, idx + n)]
    if seg.size == 0:
        return float(np.percentile(vds_q1, 90))
    positive = seg[seg > 0]
    if positive.size == 0:
        return float(np.percentile(vds_q1, 90))
    return float(np.percentile(positive, 90))


def local_current(i: np.ndarray, idx: int, dt_s: float, window_ns: float, side: str = "before") -> float:
    n = max(3, int(round(window_ns * 1e-9 / dt_s)))
    if side == "after":
        seg = i[idx : min(i.size, idx + n)]
    else:
        seg = i[max(0, idx - n) : idx]
    if seg.size == 0:
        return float(i[min(max(idx, 0), i.size - 1)])
    return float(np.median(seg))


def find_loss_window(
    vds_q1: np.ndarray,
    idx: int,
    kind: str,
    vbus: float,
    low_v: float,
) -> tuple[int, int]:
    if not math.isfinite(vbus) or abs(vbus - low_v) < 1e-12:
        return idx, idx
    norm = np.clip((vds_q1 - low_v) / (vbus - low_v), 0.0, 1.0)

    if kind == "Eon":
        start = idx
        while start > 0 and norm[start] <= 0.9:
            start -= 1
        if norm[start] > 0.9 and start < idx:
            start += 1
        end = idx
        while end < norm.size - 1 and norm[end] > 0.1:
            end += 1
    elif kind == "Eoff":
        start = idx
        while start > 0 and norm[start] >= 0.1:
            start -= 1
        if norm[start] < 0.1 and start < idx:
            start += 1
        end = idx
        while end < norm.size - 1 and norm[end] < 0.9:
            end += 1
    else:
        raise ValueError(f"unsupported loss kind: {kind}")

    return int(start), int(end)


def integrate_loss(
    vds_q1: np.ndarray,
    current_A: np.ndarray,
    time_s: np.ndarray,
    idx: int,
    kind: str,
    pulse: int,
    dt_s: float,
    vbus: float,
    low_v: float,
    current_window_ns: float,
) -> LossEvent:
    start, end = find_loss_window(vds_q1, idx, kind, vbus, low_v)
    vv = np.clip(vds_q1[start : end + 1] - low_v, 0.0, vbus - low_v)
    vbus_eff = max(vbus - low_v, 1e-12)
    i_edge = local_current(current_A, idx, dt_s, current_window_ns, side="before")
    vdt_Vns = float(np.sum(vv) * dt_s * 1e9)
    e_upper_uJ = vdt_Vns * i_edge * 1e-3
    e_linear_uJ = float(np.sum(vv * (1.0 - vv / vbus_eff)) * dt_s * 1e9 * i_edge * 1e-3)

    return LossEvent(
        kind=kind,
        pulse=pulse,
        idx=int(idx),
        start_idx=int(start),
        end_idx=int(end),
        center_us=float(time_s[idx] * 1e6),
        start_us=float(time_s[start] * 1e6),
        end_us=float(time_s[end] * 1e6),
        tv_ns=float((end - start + 1) * dt_s * 1e9),
        i_edge_A=float(i_edge),
        vbus_V=float(vbus),
        e_upper_uJ=float(e_upper_uJ),
        e_linear_uJ=float(e_linear_uJ),
        vdt_Vns=float(vdt_Vns),
    )


def stats_for(trace: np.ndarray) -> dict[str, float]:
    return {
        "min": float(np.min(trace)),
        "max": float(np.max(trace)),
        "avg": float(np.mean(trace)),
        "median": float(np.median(trace)),
        "pp": float(np.ptp(trace)),
        "q01": float(np.percentile(trace, 1)),
        "q10": float(np.percentile(trace, 10)),
        "q90": float(np.percentile(trace, 90)),
        "q99": float(np.percentile(trace, 99)),
    }


def build_time(points: int, total_span_s: float) -> tuple[np.ndarray, float]:
    dt_s = total_span_s / points
    time_s = (np.arange(points, dtype=np.float64) - points / 2.0) * dt_s
    return time_s, dt_s


def convert_current(raw: np.ndarray, args: argparse.Namespace) -> tuple[np.ndarray, float]:
    if args.current_mode == "direct":
        baseline = float(args.current_baseline) if args.current_baseline is not None else 0.0
        return (raw - baseline) * args.current_scale, baseline

    current = (raw - args.ct_zero_v) / args.ct_v_per_a
    baseline = float(args.current_baseline) if args.current_baseline is not None else 0.0
    return (current - baseline) * args.current_scale, baseline


def analyze_file(path: Path, output_dir: Path, args: argparse.Namespace) -> FileResult:
    headers, traces = parse_rg01(path)
    if len(traces) < max(args.vds_trace, args.current_trace):
        raise ValueError(f"{path}: not enough traces for selected mapping")

    points = headers[0].points
    total_span_s = headers[0].total_span_s
    for header in headers:
        if header.points != points:
            raise ValueError(f"{path}: traces have different point counts")
    time_s, dt_s = build_time(points, total_span_s)

    raw_vds = traces[args.vds_trace - 1]
    current_A, current_baseline = convert_current(traces[args.current_trace - 1], args)

    if args.vds_input == "q1":
        vds_q1 = raw_vds.copy()
    else:
        _, high, _ = estimate_levels(raw_vds)
        vds_q1 = np.clip(high - raw_vds, a_min=-abs(high), a_max=None)

    intervals, (low_v, high_v, threshold_v) = detect_on_intervals(
        vds_q1,
        dt_s=dt_s,
        min_pulse_us=args.min_pulse_us,
        debounce_ns=args.debounce_ns,
    )
    if len(intervals) < 2:
        raise ValueError(
            f"{path}: detected {len(intervals)} Q1 on-pulse(s); need at least 2. "
            f"Check --vds-input, trace mapping, or --min-pulse-us."
        )

    pulses: list[PulseMetrics] = []
    losses: list[LossEvent] = []
    low_for_loss = args.vds_low_v if args.vds_low_v is not None else max(0.0, low_v)
    global_vbus = high_v

    for pulse_no, (start_idx, end_idx) in enumerate(intervals[: args.max_pulses], start=1):
        if args.vbus_mode == "local":
            vbus_on = max(
                local_vbus(vds_q1, start_idx, dt_s, "before", args.level_window_ns),
                local_vbus(vds_q1, end_idx, dt_s, "after", args.level_window_ns),
                high_v,
            )
            eon_vbus = max(local_vbus(vds_q1, start_idx, dt_s, "before", args.level_window_ns), high_v)
            eoff_vbus = max(local_vbus(vds_q1, end_idx, dt_s, "after", args.level_window_ns), high_v)
        else:
            vbus_on = global_vbus
            eon_vbus = global_vbus
            eoff_vbus = global_vbus

        i_start = local_current(current_A, start_idx, dt_s, args.current_window_ns, side="before")
        i_end = local_current(current_A, end_idx, dt_s, args.current_window_ns, side="before")
        duration_us = (end_idx - start_idx + 1) * dt_s * 1e6
        di_A = i_end - i_start
        slope = di_A / duration_us if duration_us else float("nan")

        pulses.append(
            PulseMetrics(
                pulse=pulse_no,
                start_us=float(time_s[start_idx] * 1e6),
                end_us=float(time_s[end_idx] * 1e6),
                duration_us=float(duration_us),
                vbus_V=float(vbus_on),
                i_start_A=float(i_start),
                i_end_A=float(i_end),
                di_A=float(di_A),
                slope_A_per_us=float(slope),
            )
        )

        losses.append(
            integrate_loss(
                vds_q1,
                current_A,
                time_s,
                start_idx,
                "Eon",
                pulse_no,
                dt_s,
                eon_vbus,
                low_for_loss,
                args.current_window_ns,
            )
        )
        losses.append(
            integrate_loss(
                vds_q1,
                current_A,
                time_s,
                end_idx,
                "Eoff",
                pulse_no,
                dt_s,
                eoff_vbus,
                low_for_loss,
                args.current_window_ns,
            )
        )

    standard = standard_metrics(losses, pulses)
    csv_path = None
    plot_path = None
    zoom_path = None

    if args.save_waveforms:
        csv_path = str(output_dir / f"{path.stem}.csv")
        save_waveform_csv(Path(csv_path), time_s, traces, current_A, vds_q1)

    if args.plot:
        plot_path, zoom_path = save_plots(output_dir, path.stem, time_s, traces, vds_q1, current_A, pulses, losses)

    return FileResult(
        file=path.name,
        timestamp=f"{headers[0].date} {headers[0].time}".strip(),
        points=points,
        trace_count=len(traces),
        total_span_us=float(total_span_s * 1e6),
        dt_ns=float(dt_s * 1e9),
        vds_input=args.vds_input,
        current_mode=args.current_mode,
        headers=headers,
        stats=[stats_for(t) for t in traces],
        current_baseline_A=float(current_baseline),
        pulses=pulses,
        losses=losses,
        standard=standard,
        csv=csv_path,
        plot=plot_path,
        zoom=zoom_path,
    )


def standard_metrics(losses: list[LossEvent], pulses: list[PulseMetrics]) -> dict[str, float | None]:
    p1_eoff = next((x for x in losses if x.pulse == 1 and x.kind == "Eoff"), None)
    p2_eon = next((x for x in losses if x.pulse == 2 and x.kind == "Eon"), None)
    if p1_eoff is None or p2_eon is None:
        return {
            "Eoff_Q1_first_uJ": None,
            "Eon_Q1_second_uJ": None,
            "E_sw_Q1_uJ": None,
            "E_upper_sum_uJ": None,
            "I_Eoff_A": None,
            "I_Eon_A": None,
        }
    return {
        "Eoff_Q1_first_uJ": p1_eoff.e_linear_uJ,
        "Eon_Q1_second_uJ": p2_eon.e_linear_uJ,
        "E_sw_Q1_uJ": p1_eoff.e_linear_uJ + p2_eon.e_linear_uJ,
        "E_upper_sum_uJ": p1_eoff.e_upper_uJ + p2_eon.e_upper_uJ,
        "I_Eoff_A": p1_eoff.i_edge_A,
        "I_Eon_A": p2_eon.i_edge_A,
        "T1_us": pulses[0].duration_us if len(pulses) >= 1 else None,
        "T2_gap_us": (pulses[1].start_us - pulses[0].end_us) if len(pulses) >= 2 else None,
        "T3_us": pulses[1].duration_us if len(pulses) >= 2 else None,
    }


def save_waveform_csv(
    path: Path,
    time_s: np.ndarray,
    traces: list[np.ndarray],
    current_A: np.ndarray,
    vds_q1: np.ndarray,
) -> None:
    fieldnames = ["index", "time_s"]
    fieldnames += [f"trace{i + 1}" for i in range(len(traces))]
    fieldnames += ["VDS_Q1_V", "current_A"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fieldnames)
        for idx in range(time_s.size):
            writer.writerow(
                [idx, f"{time_s[idx]:.12e}"]
                + [f"{trace[idx]:.9g}" for trace in traces]
                + [f"{vds_q1[idx]:.9g}", f"{current_A[idx]:.9g}"]
            )


def save_plots(
    output_dir: Path,
    stem: str,
    time_s: np.ndarray,
    traces: list[np.ndarray],
    vds_q1: np.ndarray,
    current_A: np.ndarray,
    pulses: list[PulseMetrics],
    losses: list[LossEvent],
) -> tuple[str | None, str | None]:
    if plt is None:
        return None, None

    t_us = time_s * 1e6
    plot_path = output_dir / f"{stem}.png"
    zoom_path = output_dir / f"{stem}_zoom.png"

    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    axes[0].plot(t_us, vds_q1, lw=1.0, label="Q1 Vds / V")
    axes[0].set_ylabel("Q1 Vds (V)")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend(loc="best")
    axes[1].plot(t_us, current_A, lw=1.0, label="Current / A", color="tab:orange")
    axes[1].set_ylabel("Current (A)")
    axes[1].set_xlabel("Time (us)")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(loc="best")
    for loss in losses:
        for ax in axes:
            ax.axvspan(loss.start_us, loss.end_us, color="tab:red", alpha=0.12)
            ax.axvline(loss.center_us, color="tab:red", alpha=0.35, lw=0.8)
    fig.suptitle(stem)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)

    if len(pulses) >= 2:
        xmin = pulses[0].start_us - 2.0
        xmax = pulses[1].end_us + 2.0
    else:
        xmin = float(np.min(t_us))
        xmax = float(np.max(t_us))

    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    mask = (t_us >= xmin) & (t_us <= xmax)
    axes[0].plot(t_us[mask], vds_q1[mask], lw=1.0, label="Q1 Vds / V")
    axes[0].set_ylabel("Q1 Vds (V)")
    axes[0].grid(True, alpha=0.25)
    axes[1].plot(t_us[mask], current_A[mask], lw=1.0, label="Current / A", color="tab:orange")
    axes[1].set_ylabel("Current (A)")
    axes[1].set_xlabel("Time (us)")
    axes[1].grid(True, alpha=0.25)
    for loss in losses:
        if xmin <= loss.center_us <= xmax:
            for ax in axes:
                ax.axvspan(loss.start_us, loss.end_us, color="tab:red", alpha=0.12)
                ax.axvline(loss.center_us, color="tab:red", alpha=0.35, lw=0.8)
    fig.suptitle(f"{stem} DPT window")
    fig.tight_layout()
    fig.savefig(zoom_path, dpi=160)
    plt.close(fig)
    return str(plot_path), str(zoom_path)


def write_summary_csv(path: Path, results: list[FileResult]) -> None:
    fields = [
        "file",
        "timestamp",
        "Vbus_P1_V",
        "T1_us",
        "T2_gap_us",
        "T3_us",
        "I_Eoff_A",
        "I_Eon_A",
        "Eoff_Q1_first_uJ",
        "Eon_Q1_second_uJ",
        "E_sw_Q1_uJ",
        "E_upper_sum_uJ",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for result in results:
            row: dict[str, object] = {
                "file": result.file,
                "timestamp": result.timestamp,
                "Vbus_P1_V": result.pulses[0].vbus_V if result.pulses else None,
            }
            row.update(result.standard)
            writer.writerow(row)


def markdown_table(headers: list[str], rows: Iterable[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(format_md_cell(x) for x in row) + " |")
    return "\n".join(lines)


def format_md_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.3f}"
    return str(value)


def write_report(path: Path, results: list[FileResult], args: argparse.Namespace) -> None:
    rows = []
    for result in results:
        rows.append(
            [
                result.file,
                result.pulses[0].vbus_V if result.pulses else None,
                result.standard.get("T1_us"),
                result.standard.get("T2_gap_us"),
                result.standard.get("T3_us"),
                result.standard.get("I_Eoff_A"),
                result.standard.get("I_Eon_A"),
                result.standard.get("Eoff_Q1_first_uJ"),
                result.standard.get("Eon_Q1_second_uJ"),
                result.standard.get("E_sw_Q1_uJ"),
            ]
        )

    text = f"""# Rigol bin 双脉冲 Eon/Eoff 自动分析报告

## 分析口径

- 拓扑：同步 Buck 半桥双脉冲，Q1 上管为硬开关主对象，Q2 下管为同步续流对象。
- 标准损耗：`E_sw_Q1 = Eoff_Q1(first) + Eon_Q1(second)`。
- Vds 输入：`{args.vds_input}`。若实际 CH2 接的是 Q2 Vds，应使用 `--vds-input q2` 或重新测 Q1 Vds。
- Vbus 口径：`{args.vbus_mode}`。默认 `global` 使用全局高低电平聚类后的高电平平台值，避免把边沿过冲计入母线电压。
- 电流输入：`{args.current_mode}`。`direct` 表示示波器数据已经是 A；`ct-voltage` 表示按 CT 比例从电压换算为 A。
- 能量估算：`E_upper = I_edge * integral(Vds dt)`；`E_linear = I_edge * integral(Vds * (1 - Vds/Vbus) dt)`。报告推荐使用 `E_linear` 作为工程估算，`E_upper` 作为上限参考。

## 汇总结果

{markdown_table(
        ["文件", "Vbus/V", "T1/us", "T2/us", "T3/us", "Ioff/A", "Ion/A", "Eoff/uJ", "Eon/uJ", "E_sw/uJ"],
        rows,
    )}

## 注意事项

- 当前算法默认用 Q1 Vds 的 10% 到 90% 电压过渡窗口积分，窗口内电流用边沿前 `{args.current_window_ns:g} ns` 中值近似。
- 若未直接测 Q1 漏极电流，结果仍是工程估算，不是最终计量值。
- SiC 边沿很快，正式报告前应对 Vds 与电流探头做 deskew；十几 ns 的延时会显著影响 Eon/Eoff。
- 若示波器保存的是 CT1 原始电压而不是电流，请使用 `--current-mode ct-voltage --ct-zero-v 1.65 --ct-v-per-a 0.00625`。
"""
    path.write_text(text, encoding="utf-8")


def json_default(obj: object) -> object:
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def collect_inputs(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(path.glob("*.bin")))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(item)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse Rigol RG01 .bin files and estimate synchronous Buck DPT Eon/Eoff.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="Input .bin files or directories containing .bin files.")
    parser.add_argument("-o", "--output", default="dpt_analysis_out", help="Output directory.")
    parser.add_argument("--vds-trace", type=int, default=2, help="1-based trace index used as Vds-like voltage.")
    parser.add_argument("--current-trace", type=int, default=3, help="1-based trace index used as current/current-sensor trace.")
    parser.add_argument("--vds-input", choices=["q1", "q2"], default="q1", help="Whether the selected Vds trace is Q1 Vds or Q2 Vds.")
    parser.add_argument("--vds-low-v", type=float, default=0.0, help="Low-level clamp used for Vds integration.")
    parser.add_argument("--current-mode", choices=["direct", "ct-voltage"], default="direct", help="Current conversion mode.")
    parser.add_argument("--current-scale", type=float, default=1.0, help="Additional current multiplier after conversion.")
    parser.add_argument("--current-baseline", type=float, default=None, help="Current baseline to subtract after conversion.")
    parser.add_argument("--ct-zero-v", type=float, default=1.65, help="CT sensor zero-current voltage for ct-voltage mode.")
    parser.add_argument("--ct-v-per-a", type=float, default=0.00625, help="CT sensor voltage gain in V/A for ct-voltage mode.")
    parser.add_argument("--min-pulse-us", type=float, default=0.5, help="Minimum Q1 on-pulse duration to keep.")
    parser.add_argument("--debounce-ns", type=float, default=20.0, help="Fill shorter false gaps in Q1-on mask.")
    parser.add_argument("--level-window-ns", type=float, default=500.0, help="Local window for estimating Vbus near edges.")
    parser.add_argument("--vbus-mode", choices=["global", "local"], default="global", help="Use global clustered Vbus or local edge-near Vbus.")
    parser.add_argument("--current-window-ns", type=float, default=100.0, help="Local pre-edge window for estimating edge current.")
    parser.add_argument("--max-pulses", type=int, default=2, help="Number of Q1 on-pulses to analyze.")
    parser.add_argument("--save-waveforms", action="store_true", help="Save full waveform CSV files.")
    parser.add_argument("--plot", action="store_true", help="Save PNG plots if matplotlib is available.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    inputs = collect_inputs(args.inputs)
    if not inputs:
        raise SystemExit("No .bin files found.")

    results: list[FileResult] = []
    failures: list[dict[str, str]] = []
    for path in inputs:
        try:
            result = analyze_file(path, output_dir, args)
            results.append(result)
            print(f"OK  {path.name}: E_sw={result.standard.get('E_sw_Q1_uJ'):.3f} uJ")
        except Exception as exc:
            failures.append({"file": str(path), "error": str(exc)})
            print(f"ERR {path}: {exc}")

    serializable = [asdict(result) for result in results]
    (output_dir / "analysis_refined.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    if failures:
        (output_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_csv(output_dir / "summary.csv", results)
    write_report(output_dir / "analysis_report.md", results, args)
    print(f"Output: {output_dir.resolve()}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
