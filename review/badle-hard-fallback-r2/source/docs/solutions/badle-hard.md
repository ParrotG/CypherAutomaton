# BadLE Hard (D2 ID 4)

## Bound artifacts

- Identity: `4 / BadLE Hard / healthcare / standard`
- `APKlog.zip`: `dc2cd4176c2d6ff38a7e2c8d0ae246ca0f92209a85351ec1b6526be8656982f2`
- `cgm_ble_log.txt`: `4bc0a67815216988229cc1e2183bf7d5d4de4ab8008fdc2e38f7cfcb10680630`

## Deterministic method

Require the exact two-file handout, then parse only active notifications for
the `f003` characteristic. The current decoder implements the description's
`frame[6]` clue using capture payload index 4. This is strongly selected by the
organizer's own range and smoothness oracle: index 4 is the only
nonconstant position whose first eight XOR-decoded values all remain
physiological and smooth. The log contains no visible two-byte prefix, and the
origin of the two-position discrepancy has not been established from the APK;
calling it an ATT-header adjustment would be unsupported.
XOR index 4 with `0x7c` for each of the first eight reading frames,
require the documented physiological range, convert mg/dL to mmol/L using 18,
round half-up to one decimal place and join the values in capture order inside
the required `INCYPHER{...}` wrapper.

Implementation: `deploy/arena/d2_badle.py` (`d2-badle-glucose-v1`).

## Local verification

Tests cover deprecated-frame exclusion, short captures, malformed relevant
notifications, range rejection, exact candidate hashing and identity/digest
near misses. No APK or supplied program is executed.

These checks reproduce an unconfirmed candidate, not a platform-accepted solve.
The saved organizer hint names `frame[6]`; the physiological oracle supports
the index-4 implementation, while the numbering discrepancy remains a material
uncertainty. Static inspection locates a native CGM parser in the
supplied APK bundle but has not verified the decoding routine.

The isolated, single-attempt fallback and team handoff procedure are in
[BADLE_HARD_SUBMISSION.md](../BADLE_HARD_SUBMISSION.md). Its default mode records
only; publishing the submission-enabled image is a separate team decision.
