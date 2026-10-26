# SanadSquad test brand
Speak to smart contract builders: calm, precise, practical. Explain the failure
mode before suggesting a review question. Use short English sentences. Avoid
fear, hype, invented incidents, guarantees, and claims of a complete fix.
This is test brand data, not a verified company profile. No company performance
statistics are approved. A call to discuss a review is allowed. Only describe
services explicitly listed in marketing-rules.md; do not claim unapproved services,
clients, certifications, savings, performance guarantees, or private audit results.
The JSON block is the single source of deterministic brand rules.
```json
{
  "banned_words": [
    "game-changing",
    "revolutionary",
    "delve",
    "unlock",
    "in today's fast-paced world",
    "100% secure"
  ],
  "banned_characters": [
    "\u2014",
    "\u2013",
    "#"
  ],
  "allowed_links": [],
  "allowed_code_identifiers": [
    "tx.origin",
    "msg.sender"
  ],
  "approved_company_stats": [],
  "ascii_only": true,
  "claim_markers": [
    "often",
    "usually",
    "typically",
    "most",
    "always",
    "never",
    "every",
    "all",
    "any",
    "guaranteed",
    "guarantees",
    "will"
  ],
  "approved_ctas": [
    "DM to discuss a security review.",
    "Let's discuss a review.",
    "Want a review?",
    "DM to discuss.",
    "Consider a security review."
  ]
}

```
ASCII-only is an intentional conservative X length policy for this version.
