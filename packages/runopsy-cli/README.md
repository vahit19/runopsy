# runopsy-cli

The terminal surface: list runs, diagnose one, and read the evidence behind a finding.

The wording here is part of the product, not decoration. A diagnosis engine earns its
place by being trusted, and trust is destroyed faster by one confident wrong answer than
by ten honest "I am not sure". So the output keeps three things apart at all times:

- **Observed failure** — what the run visibly got wrong. A fact.
- **Suspected onset** — where it probably started going wrong. A hypothesis, always
  shown with its confidence and never phrased as a cause.
- **Confirmed cause** — reserved for findings a counterfactual replay or a person has
  actually validated. Nothing the deterministic pipeline produces on its own may claim
  this.

Every unconfirmed diagnosis ends by telling the user how to upgrade it, because the
point is not to sound certain — it is to make certainty cheap to obtain.
