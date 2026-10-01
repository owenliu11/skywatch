"""Freeze local history, identity/time split, inject 5%, and score the rules."""

import asyncio
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from random import Random

import asyncpg
import pyarrow as pa
import pyarrow.parquet as pq

from app.detect.evaluate import run
from app.detect.gaps import RecentGaps
from app.detect.inject import AnomalyInjector
from app.detect.synthetic import destination
from app.detect.windows import GAP_S, Sample

ROOT = Path(os.environ.get("PHASE4_ROOT", str(Path(__file__).resolve().parents[2])))
SEED = 20261001
SPLIT = datetime(2026, 9, 30, tzinfo=UTC)
KINDS = [
    "teleport",
    "velocity_inconsistency",
    "impossible_climb_a",
    "impossible_climb_b",
    "frozen_position",
    "duplicate_identity",
    "slow_drift",
]
MAGNITUDES = [50000, 0.1, 60, 60, 0, 50000, 10]


def heldout(icao):
    return int(hashlib.sha256(icao.encode()).hexdigest(), 16) % 2 == 1


async def main():
    (ROOT / "eval").mkdir(parents=True, exist_ok=True)
    rng = Random(SEED)
    connection = await asyncpg.connect(
        os.environ.get("DATABASE_URL", "postgresql://skywatch:skywatch@localhost:55432/skywatch")
    )
    cutoff = datetime.now(UTC)
    rows = await connection.fetch(
        """SELECT icao24,region,time,time_position,latitude,longitude,
        baro_altitude,geo_altitude,velocity,true_track,vertical_rate,on_ground,ingested_at
        FROM state_vectors WHERE region IN ('bay_area','socal') AND time >= $1 AND time < $2
        ORDER BY icao24,region,time""",
        SPLIT,
        cutoff,
    )
    earlier = await connection.fetch(
        """SELECT DISTINCT icao24 FROM state_vectors
        WHERE region IN ('bay_area','socal') AND time < $1""",
        SPLIT,
    )
    gap_rows = await connection.fetch("SELECT region,started_at,ended_at,reason FROM ingest_gaps")
    await connection.close()
    gaps = RecentGaps()
    for g in gap_rows:
        gaps.record(g["region"], g["started_at"].timestamp(), g["ended_at"].timestamp())
    (ROOT / "eval/recent_gaps.json").write_text(json.dumps(gaps.intervals, indent=2) + "\n")
    raw = []
    grouped = defaultdict(list)
    for r in rows:
        if r["latitude"] is None or r["longitude"] is None:
            continue
        s = Sample(
            r["time"].timestamp(),
            r["time_position"].timestamp() if r["time_position"] is not None else None,
            r["latitude"],
            r["longitude"],
            r["baro_altitude"],
            r["geo_altitude"],
            r["velocity"],
            r["true_track"],
            r["vertical_rate"],
            r["on_ground"],
            r["region"],
        )
        raw.append(
            {"icao24": r["icao24"], **asdict(s), "ingested_at": r["ingested_at"].timestamp()}
        )
        if heldout(r["icao24"]):
            grouped[(r["icao24"], r["region"])].append(s)
    pq.write_table(pa.Table.from_pylist(raw), ROOT / "eval/recorded_tracks.parquet")
    sessions = []
    for (icao, region), samples in sorted(grouped.items()):
        chunks = [[]]
        for sample in samples:
            if chunks[-1] and sample.last_contact - chunks[-1][-1].last_contact > GAP_S:
                chunks.append([])
            chunks[-1].append(sample)
        for chunk in chunks:
            if len(chunk) >= 30:
                sessions.append((icao, region, chunk))
    # Stable shuffle, 5% injection. No tuning against the evaluation results.
    rng.shuffle(sessions)
    n_injected = len(sessions) // 20
    if n_injected < len(KINDS):
        raise RuntimeError("Not enough held-out sessions for 5% coverage of all seven variants")
    # Injection candidates must have coherent fresh timestamps; leave imperfect controls intact.
    eligible = [
        i
        for i, (_, _, t) in enumerate(sessions)
        if all(
            s.time_position is not None
            and s.baro_alt is not None
            and s.geo_alt is not None
            and s.velocity is not None
            and s.true_track is not None
            and s.vertical_rate is not None
            and not s.on_ground
            for s in t
        )
        and all(1 <= b.time_position - a.time_position <= GAP_S for a, b in pairwise(t))
    ]
    if len(eligible) < n_injected:
        raise RuntimeError("Insufficient coherent tracks for requested injection fraction")
    selected = {index: KINDS[k % len(KINDS)] for k, index in enumerate(eligible[:n_injected])}
    result = []
    manifest_tracks = []
    injector = AnomalyInjector()
    for index, (icao, region, track) in enumerate(sessions):
        kind = selected.get(index, "clean")
        label = None
        original_count = len(track)
        if kind != "clean":
            start = max(10, len(track) // 3)
            end = min(len(track) - 3, start + max(8, len(track) // 5))
            magnitude = MAGNITUDES[KINDS.index(kind)]
            donor = None
            if kind == "duplicate_identity":
                donor = []
                for a, b in pairwise(track):
                    dt = (b.last_contact - a.last_contact) / 2
                    lat, lon, heading = destination(a.lat, a.lon, a.true_track, a.velocity * dt)
                    lat, lon, _ = destination(lat, lon, 0, magnitude)
                    donor.append(
                        replace(
                            a,
                            last_contact=a.last_contact + dt,
                            time_position=a.time_position + dt,
                            lat=lat,
                            lon=lon,
                            true_track=heading,
                            baro_alt=a.baro_alt + a.vertical_rate * dt,
                            geo_alt=a.geo_alt + a.vertical_rate * dt,
                        )
                    )
            track, label = injector.inject(track, kind, start, end, magnitude, donor)
        track_id = f"{icao}:{region}:{track[0].last_contact:.0f}"
        manifest_tracks.append(
            {
                "track_id": track_id,
                "icao24": icao,
                "region": region,
                "start": track[0].last_contact,
                "end": track[-1].last_contact,
                "original_samples": original_count,
                "label": asdict(label) if label else None,
            }
        )
        for j, s in enumerate(track):
            result.append(
                {
                    "track_id": track_id,
                    "source_icao24": icao,
                    "sample_index": j,
                    "injection_type": kind,
                    "label_start": label.start if label else -1,
                    "label_end": label.end if label else -1,
                    "magnitude": label.magnitude if label else None,
                    "seed": SEED,
                    "split": "evaluation",
                    **asdict(s),
                }
            )
    dataset = ROOT / "eval/labeled_set.parquet"
    pq.write_table(pa.Table.from_pylist(result), dataset)
    manifest = {
        "seed": SEED,
        "source": "real recorded bay_area/socal; controlled injected copies",
        "split_utc": SPLIT.isoformat(),
        "query_cutoff_utc": cutoff.isoformat(),
        "identity_split": "SHA256(icao24) modulo 2: 0 reserved earlier; 1 later evaluation",
        "reserved_earlier_ids": sorted(r["icao24"] for r in earlier if not heldout(r["icao24"])),
        "training": "none; Track B excluded",
        "n_sessions": len(sessions),
        "n_injected": n_injected,
        "injection_fraction": n_injected / len(sessions),
        "gap_s": GAP_S,
        "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "tracks": manifest_tracks,
    }
    (ROOT / "eval/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    results = run(dataset, ROOT / "eval/rules_metrics.json", gaps)
    print(
        json.dumps({k: v for k, v in results["overall"].items() if k != "event_details"}, indent=2)
    )
    print("sessions", len(sessions), "injected", n_injected)


if __name__ == "__main__":
    asyncio.run(main())
