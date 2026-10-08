# Views — fixture 0 (synthetic; no real ticker or call)

Three views in living-desk shape, chosen so the merit gate's own suite
has one of every outcome: one that the fixture scan confirms, one it
contradicts, and one watched-but-quiet that earns nothing.

The ticker names (BOT, BDU, BCD) are synthetic; the scan fixture
``tests/fixtures/letter/scan/scan-0.json`` carries their rotation rows:
``BOT rs20=-1``, ``BDU rs20=-1`` and ``BCD rs20=-2``. The thresholds
below pick one outcome each.

## Standing views

### Synthetic A — confirms
Id: synthetic-confirms
Weight: lead
Since: 2026-01-01
Beats: synthetic-axis-a
Watching: BOT
Claim: A synthetic view that the fixture scan confirms; the watched
ticker has a row in the scan fixture so the rotation finding lands.
Confirms when: BOT rs20 >= -2
Contradicts when: BOT rs20 <= -100

### Synthetic B — contradicts
Id: synthetic-contradicts
Weight: standing
Since: 2026-01-01
Beats: synthetic-axis-b
Watching: BDU
Claim: A synthetic view the fixture scan contradicts; the bound
is set above the scan's value so the verdict trips the right way.
Confirms when: BDU rs20 >= 100
Contradicts when: BDU rs20 <= -0.5

### Synthetic C — quiet
Id: synthetic-quiet
Weight: watch
Since: 2026-01-01
Beats: synthetic-axis-c
Watching: BCD
Claim: A synthetic view held watch-only; nothing in the scan
moves it and it earns no finding.
Confirms when: BCD rs20 >= 100
Contradicts when: BCD rs20 <= -100