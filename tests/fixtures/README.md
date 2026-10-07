# The calibration record

This is the evidence behind `DEFAULT_THRESHOLD` (0.364) in `uvc_ptz_mcp/verify.py`. It is kept as
**measurements, not pictures**.

## What is here

| File | Contents |
|---|---|
| `verify_cases.json` | 10 labelled pairs: which two frames, what the label is, the value the metric produced, and how each pair came to be labelled |
| `README.md` | This file |

**No frames.** They were removed, deliberately and permanently. The captures from that session show
a room somebody lives in, two of them showed a person, and a photograph of a private space is not
test data to publish — a small greyscale still of somebody's home is still their home. The numbers
are what the threshold rests on; the pixels were never needed to test it.

## Why the labels can be trusted

Every label comes from *what was observed at the time*, never from the metric:

| Label | How it was established |
|---|---|
| `same` | Frames taken during a 96-second run with **no commands sent at all**, and frames taken either side of a write that demonstrably did not reach the hardware (the read-back said it had) |
| `changed` | Frames taken either side of a command that moved the camera — confirmed by looking at the two frames, because a number cannot separate "the optics moved" from "the exposure re-converged" |

That distinction is the whole point of the record: the highest scoring `same` pair (0.1038) is a
write the camera ignored while reporting success, and the lowest scoring `changed` pair (0.6242) is
a real 30° tilt. The threshold sits between them with a 6× margin.

## Where it came from

Frames captured from a **DJI Osmo Pocket 4P** in webcam mode on **2026-09-26**, over its standard
UVC camera controls. The device was returned to the store afterwards, so there are no more
measurements to take. The full writeup — device identity, the advertised control surface, why the
read-back is unusable, the quiet baseline, the measured motion model, the pan aim map, the
Extension Unit dead end — is in [`docs/measurements.md`](../../docs/measurements.md).

## How it is used

`tests/test_verify.py` asserts that this record and `verify.reference_cases()` in the code describe
the same pairs, with the same labels and the same values, and that the shipped threshold still sits
between the two clusters. Edit one copy and the test fails. A second test,
`test_no_captures_are_committed`, fails if any image or video appears anywhere in the repository.

## Rebuilding it from fresh captures

The method is preserved even though the pictures are not:

```bash
python scripts/make_fixtures.py --frames <directory of captures> --check   # measure, write nothing
```

`--check` prints the table and fails if the shipped threshold no longer separates the pairs,
without writing anything. Running it without `--check` writes a frame pack, which is gitignored on
purpose: measure with it, then keep the numbers.
