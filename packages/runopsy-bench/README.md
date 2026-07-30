# runopsy-bench

Labelled traces with known ground truth, and the metrics that score a diagnosis
against them.

This package is the difference between claiming the engine localizes failures and
knowing it does. Every case declares where the run actually broke, so onset accuracy,
top-3 recall and step distance are measured rather than asserted.

Two design choices keep the number honest.

**Negative controls are part of the suite.** A healthy run that produces no finding is
scored alongside the failures. An engine that flags everything would otherwise post a
perfect onset accuracy while being useless, and false positives are what actually get a
diagnosis tool switched off.

**Known blind spots are labelled, not hidden.** Some cases — a tool that succeeds while
writing the wrong value, an agent that picks a plausible but wrong tool — cannot be
found by structural analysis at all, because nothing in the recorded trace is anomalous.
They are marked `deterministically_detectable = False`, excluded from the accuracy
figure, and reported separately as the coverage gap that semantic analysis exists to
close. Quietly dropping them would inflate the headline number and hide exactly the work
that remains.
