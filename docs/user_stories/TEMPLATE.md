# User story template

Copy this file to `docs/user_stories/<your-project>.md` and fill it in.

Keep it non-technical: describe who does what and why, not how your current
tooling implements it. These stories feed requirements gathering for later
technical design, so the most valuable content is what your users try to
accomplish, where today's tools fail them, and what they could not live
without. One file per project/organization is fine; add as many stories as
you need.

---

# &lt;Project name&gt; user stories

**Project:** &lt;name, link&gt;
**Current benchmarking setup:** &lt;one or two sentences: what you run, how often,
where results go — e.g. "nightly ASV runs on two dedicated machines, results
published as a static site"&gt;
**Contact:** &lt;name/handle for follow-up questions&gt;

## Who uses benchmarks here

List the kinds of people who interact with benchmark results, in your
project's own words (contributor, reviewer, release manager, perf engineer,
infra operator, …). One line each on what they care about.

## Stories

Use as many as apply. Format:

### &lt;Short title&gt;

> As a &lt;role&gt;, I want &lt;goal&gt;, so that &lt;why it matters&gt;.

**Today:** How this works right now, in a sentence or two.
**Pain:** Where the current experience falls short, if it does.

## What must not break

Capabilities your project depends on today that any future system must keep.

## Wishes

Things nobody supports today but your users keep asking for.

## Scale and constraints

Rough numbers only: benchmarks per run, runs per day, history length you
care about, hardware you run on, anything unusual (clusters, GPUs, cross-repo
benchmarks, …).
