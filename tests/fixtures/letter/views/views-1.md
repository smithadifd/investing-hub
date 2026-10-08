# Views — fixture 1 (synthetic; one confirms + one quiet)

A two-view fixture chosen so the contradicts arm does NOT trip on any
scan row and the corpus leg yields an `extends` finding.

The ticker names (BOT, BCD) are synthetic; the scan fixture
``tests/fixtures/letter/scan/scan-0.json`` carries their rotation rows
(``BOT rs20=-1`` and ``BCD rs20=-2``).

## Standing views

### Synthetic A — confirms
Id: synthetic-confirms
Weight: lead
Since: 2026-01-01
Beats: synthetic-axis-a
Watching: BOT
Claim: A synthetic view that the fixture scan confirms.
Confirms when: BOT rs20 >= -2
Contradicts when: BOT rs20 <= -100

### Synthetic B — quiet
Id: synthetic-quiet
Weight: watch
Since: 2026-01-01
Beats: synthetic-axis-b
Watching: BCD
Claim: Held watch-only on this fixture.
Confirms when: BCD rs20 >= 100
Contradicts when: BCD rs20 <= -100