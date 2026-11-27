# Benchmark Use Case Template

One file per use case, in `docs/use-cases/`, named `UC-NN-short-slug.md`.

**How to use this.** Fill sections 1-6 from the user's perspective; they should be
readable by someone who has never seen the schema. Sections 7-10 are the payoff -
they are what the schema is derived from. A use case that cannot complete section 8
is not yet understood well enough to design against.

Delete guidance text as you go. Write "n/a" rather than deleting a heading, so the
shape stays comparable across documents.

---

## UC-NN: &lt;title&gt;

| | |
|---|---|
| **Status** | draft / under review / accepted / rejected / superseded by UC-NN |
| **Owner** | |
| **Last updated** | |
| **Related** | UC-NN, UC-NN |

### 1. Summary

One or two sentences. What does someone accomplish, and why can't they do it today?

### 2. Motivation

Who has this problem and how often? What do they currently do instead, and what does
that cost them - wrong conclusions, wasted time, regressions shipped? Prefer a real
incident or a named workflow over a hypothetical.

If this use case exists mainly because a tool we might adopt happens to support it,
say so. That is a weaker justification and should be visible as such.

### 3. Actors and trigger

- **Actor:** contributor on a laptop / maintainer reviewing a PR / CI on merge /
  release manager / downstream packager / someone else
- **Trigger:** what makes them start? A command they type, a CI event, a schedule,
  an alert.
- **Frequency:** per keystroke-loop / per PR / per merge / nightly / per release

### 4. Workflow sketch

The commands or UI actions, in order, including what the actor sees back. Rough is
fine; the point is to expose where data is produced and consumed.

```
$ <command>
<what they see>
```

Note where the loop closes: what do they do with the answer? If the answer is
"nothing, they look at it," say that - some use cases really are just display, and
those have much weaker schema requirements than ones feeding an automated decision.

### 5. Study design

The core of the use case. Every record is a measurement plus coordinates:

- **Code identity** - benchmark ID, parameters
- **Code version** - commit, patch/variant label, build configuration
- **Environment** - machine, OS, CPU, BLAS vendor and threading, tuning state
- **Execution context** - session, round, interleave position, instrument

State for each:

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | varying / controlled / free | |
| Code version | varying / controlled / free | |
| Environment | varying / controlled / free | |
| Execution context | varying / controlled / free | |

- **Varying** - the independent variable; identifying fields must be present and
  *distinct* across records being compared.
- **Controlled** - must be present and *equal* across records being compared.
- **Free** - genuinely irrelevant to the comparison's validity. Justify each one;
  "free" is where invalid comparisons hide.

If more than one coordinate varies, explain why the comparison is still meaningful,
or split this into multiple use cases.

### 6. Measurements

| Measure | Instrument | Unit | Direction |
|---|---|---|---|
| e.g. wall time | pyperf | s | lower is better |
| e.g. peak RSS | memray | bytes | lower is better |

- **Raw samples retained?** yes / no / how many
- **Is the instrument available in every environment this use case spans?**
  (Cross-platform and cross-machine cases frequently fail here.)

### 7. Comparison semantics

- **What comparison is valid?** paired / unpaired / against a stored baseline /
  against a machine-relative anchor / none, absolute values only
- **Estimator:** median, trimmed mean, minimum, full distribution
- **What counts as a real difference?** A percentage, a significance criterion, or
  "human eyeballs it." Be concrete - this determines whether raw samples and
  environment strictness are load-bearing or decorative.
- **Expected noise floor** in this setting, and what it implies about the smallest
  detectable effect.

### 8. Schema implications

**The section this document exists for.**

- **Profile:** name of the validation profile this needs. Existing one, or new?
- **Fields required beyond the core:**
- **Fields required to be *absent* or explicitly null:**
- **New fields not currently in the schema:** name, type, intended Arrow type,
  who populates it, and what happens when it can't be determined.
- **Comparability key:** which environment fields must match for two records in this
  use case to belong to the same series?
- **Validation invariants:** e.g. "all records in a comparison share `env_hash`";
  "`session_id` equal and `round` distinct"; "`anchor_run_id` present and resolvable".
- **Conflicts with existing use cases:** a field this needs required that another
  needs optional, a different meaning for a shared field, an incompatible key.

### 9. Storage and lifecycle

- **Volume:** records per invocation, invocations per day, per actor
- **Retention:** ephemeral (discarded after the loop) / session-scoped / permanent
- **Location:** local working dir / shared store / both
- **Does this data ever need to join against data from another use case?** If yes,
  name it - this is the main argument for a shared core, and the main source of
  pressure on it.

### 10. Degenerate and failure modes

- What happens when a *controlled* coordinate silently isn't constant? (Machine
  thermal state drifts; BLAS changes under an unpinned environment; the working tree
  is dirty.) Is that detectable from the record alone, or does bad data enter the
  store looking valid?
- What happens with missing or partial data - one variant failed, a machine dropped
  out, an instrument is unavailable?
- What is the worst wrong conclusion someone could draw, and does the schema make it
  harder or easier to draw?

### 11. Non-goals

What this use case explicitly does *not* need to support. Especially: adjacent things
a reader would reasonably assume are included.

### 12. Open questions

Numbered, so they can be referenced in review.

---

## Worked example

### UC-03: Compare one benchmark across contributors' machines

| | |
|---|---|
| **Status** | draft |

**1. Summary.** A contributor sees a slowdown that another contributor cannot
reproduce. They want to run the same benchmark, at the same commit, on both machines
and determine whether the difference is real and environment-dependent.

**5. Study design.**

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | controlled | same benchmark ID and params |
| Code version | controlled | same commit; both trees must be clean |
| Environment | **varying** | the independent variable |
| Execution context | free | sessions are inherently separate |

**7. Comparison semantics.** Absolute wall time is not comparable across machines.
Comparison must be against a per-machine anchor workload run in the same session.
Meaningful difference: a ratio-of-ratios, not a raw delta.

**8. Schema implications.**

- Profile: new, `cross-environment`.
- Environment fields become *mandatory and maximal* - CPU model, microcode where
  available, governor, turbo state, BLAS vendor and version, thread caps, build
  flags. This is the strictest environment requirement of any use case so far,
  despite being a purely local workflow.
- New field: `anchor_run_id`, referencing an anchor benchmark record from the same
  session on the same machine. Nullable only if the profile is not claimed.
- Comparability key: inverted - records must *differ* in machine identity and
  *agree* on everything under code identity and code version.
- Conflict: contradicts the assumption that local records may omit build metadata.

**10. Degenerate modes.** If one contributor's tree is dirty, the commit field is a
lie and the comparison is meaningless while still appearing valid. The schema should
make `dirty` non-nullable in this profile so the record cannot be produced silently.
