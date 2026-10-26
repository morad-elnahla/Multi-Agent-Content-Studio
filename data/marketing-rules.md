# Marketing Guide

Rules for every `marketing-post`. Read alongside `brand-voice.md`, which covers tone and
banned language. This file covers what we say, where we get it, and what shape it takes.

Technical threads are not governed by this file.

---

## 1. Positioning

Most Web3 security firms audit smart contracts and stop there. A protocol with a clean
audit still gets drained through a compromised developer laptop, a poisoned npm package,
a hijacked frontend, or an AI agent that was talked into signing something.

**SanadSquad covers the whole attack surface, not just the contract.**

That is the single idea behind our marketing. Every post should be traceable back to it.

Four service lines:

| Service | What it covers |
|---|---|
| **Web3 security** | Smart contract audits across EVM and Solana / SVM, protocol design review, formal reasoning about invariants |
| **Web2 pentesting** | The infrastructure around the protocol: APIs, cloud, CI/CD, dependencies, internal access, the frontend |
| **Agentic security and red teaming** | AI agent systems: prompt injection, tool abuse, unauthorized action, agent wallets and spending policy |
| **Agentic payment protocols** | x402 and MPP: integration review, facilitator trust, payment policy design |

**The connective argument:** these are not four separate businesses. An agent that holds a
wallet needs all four at once. It runs on Web2 infrastructure, signs Web3 transactions,
takes instructions in natural language, and pays over an agentic payment rail. A firm that
only reads Solidity cannot assess that.

---

## 2. Sources

Every marketing post is grounded in something retrieved during the run. Never write a
marketing post from memory.

`source.json` maps each service line to the domains the research agent may search. Within
each service, sources are grouped by what they are good for:

```json
{
  "web3-security": {
    "blogs":        ["..."],
    "docs":         ["..."],
    "production":   ["..."],
    "analysis":     ["..."],
    "figures":      ["..."]
  },
  "pentesting":            { "...": [] },
  "agentic-security":      { "...": [] },
  "agentic-payments":      { "...": [] },
  "_deny": ["..."]
}
```

| Group | What belongs in it |
|---|---|
| `blogs` | Publications focused on the niche. Research writeups, disclosure posts, vendor engineering blogs. |
| `docs` | Official documentation for the service area and for the major projects inside it. |
| `production` | What has newly shipped. Release notes, changelogs, standards trackers, launch announcements. |
| `analysis` | Numbers. TVL movements, hack post-mortems and loss totals, x402 and MPP transaction volume, facilitator counts and share, adoption curves. |
| `figures` | People worth following in each area, and what they are currently arguing about. |

Rules:

- Search only inside the allowlist for the service the post is about.
- Log every query, including ones that returned nothing.
- A link may appear in a post only if it was fetched during this run.
- Cross-service posts (for example, an argument that spans Web3 and agentic security) may
  search both allowlists. Record which service each source came from.
- If nothing current turns up for a slot, mark it failed and move on. Do not substitute a
  generic post.

---

## 4. Post Formats

Vary the shape. A feed where every post is the same length reads as automated. Across a
week, no single format should appear more than twice.

### 4.1 Short tip
One to three sentences. A single concrete reason to work with us, or one sharp
observation. No link. No preamble.

### 4.2 Differentiation post
Medium length. Names a gap that contract-only firms leave open and shows which of our
service lines closes it. Must reference something real: an incident, a report, a number.
Not an abstract claim of being better.

### 4.3 Service overview
Medium to long. What a service line actually covers and who needs it. Use when there is a
reason to raise it now, not as filler.

### 4.4 News reaction
Short to medium. Something that happened this week in one of our areas, plus what we think
it means. Must link the source. This is the format most dependent on `source.json`
returning something current.

### 4.5 Learning resource
Short post or short thread. Point to an article, documentation page, roadmap or reading
path worth someone's time, with a line on why. The link is required and must resolve. It
is fine for the resource to be someone else's work; attribute it plainly.

### 4.6 Thread
Three to seven posts, shorter than a technical thread. Use for comparisons and arguments
that need steps. The x402 versus MPP comparison belongs here.

---

## 5. Required Angles

Each of these should appear over time. The research agent decides when there is current
material to support one.

**Web3 security.** Audits, SVM and EVM work, what we found, how deployed code drifts from
what was audited.

**Web2 pentesting.** The argument that most large losses start outside the contract:
compromised keys, supply chain, infrastructure. Use real incidents and real numbers.

**Agentic security and red teaming.** What makes an AI agent attackable. Prompt injection
reaching a signing tool. The difference between keeping a key out of the model's context
and stopping the model from being manipulated into using it. Why policy limits, not
authentication, are the control that matters.

**Agentic payments: x402 versus MPP.** Both let software pay for things. They differ in
origin, settlement model, governance and maturity. The post should explain the mechanics,
say where each fits, and give current numbers for volume and facilitators from the
`analysis` sources. Compare honestly: do not pick a winner we cannot support with data.

---

## 6. Claims

**Can say:**
- Figures from `audits.json`, where approved.
- Anything retrieved this run, attributed to its source.
- Our own stated opinion, marked as opinion.

**Cannot say:**
- Client names without approval.
- Performance claims with no number behind them.
- Guarantees of security. We reduce risk. We do not eliminate it.
- Anything about an active engagement.

**Competitors:** criticise the *model* (contract-only coverage), never a named firm. No
direct comparison posts.

**Calls to action:** at most one per post, and not on every post. Over a week, no more than
a third of posts should carry one. Prefer a specific next step over "get in touch".

---

## 7. Weekly Mix

Across the marketing posts in a week:

- No more than one post per service line unless the news justifies it.
- At least one post carrying a link.
- At least one post with no link.
- Formats must differ.
- If two slots resolve to similar topics, the second is re-resolved or dropped.


NOTE: `audits.json` is empty now so you can leave this part for now as all audits that has been done by the company are now private
