# Digest & Telegram

_Epic: E6 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Render the durable grounded state into a **daily digest** delivered over Telegram, so the human sees only decisions, waiting items, completed-would-have actions, uncertain items, and FYIs — never a raw inbox. The digest is the primary human-facing egress surface of v1 and is therefore a **phishing / injection surface**: it is rendered by a **deterministic templater, never a model**, and every attacker-derived string is neutralized before it reaches the chat.

## Scope

**In v1**
- One scheduled digest (extensible to AM/PM) built by pure code from state queried out of Postgres.
- Deterministic message templater with escaping, control/bidi stripping, URL defanging, and per-item length caps.
- Bot locked to a single authorized `chat_id` (its only permitted output target).
- Five sections: **Decisions**, **Waiting**, **Completed-would-have**, **Uncertain** (budgeted, risk-ranked), **FYI**.
- Provenance drill-down (show source message id / span) that never auto-fetches attacker content.
- In-flow correction capture (button/reply → correction record → regression fixture).

**Deferred / out of scope**
- Scheduled AM/PM cadence, voice message, voice call (later per plan).
- Rich interactive UI beyond simple inline buttons / reply parsing.
- Any outbound email action (shadow mode: zero email actions).

## Interfaces & data contracts

Digest assembly is a pure function of state → view model → rendered text. No model call anywhere in this path.

```python
class DigestItem(BaseModel):
    work_object_id: UUID
    section: Literal["decisions", "waiting", "completed_would_have", "uncertain", "fyi"]
    # All display strings below are attacker-derived unless proven otherwise.
    title_raw: str                 # e.g. subject / summary span
    detail_raw: str | None
    sender_display_raw: str        # never trusted; rendered as external
    sender_address: str            # verified address only
    domain_authenticated: bool     # from envelope (SPF/DKIM/DMARC computed)
    identity_trusted: bool         # allowlist lookup only
    trusted_signal_risk: float     # for ranking the uncertain/fyi budgets
    provenance: list[ProvenanceRef]  # {source_event_id, source_span}

class DigestViewModel(BaseModel):
    generated_at: datetime
    account_ref: str
    low_trust: bool                # set when eval trust-bar gate not met (spec 08)
    sections: dict[str, list[DigestItem]]

def render_digest(vm: DigestViewModel) -> list[TelegramMessage]: ...   # pure, deterministic
```

**Neutralization pipeline (applied to every `*_raw` string before rendering):**
1. Unicode-normalize; strip/replace bidi and C0/C1 control characters.
2. HTML/Markdown-escape for the chosen Telegram parse mode (prefer plain text or fully-escaped MarkdownV2).
3. **Defang URLs**: rewrite `http(s)://…` so Telegram does not auto-link (e.g. `hxxp://example[.]com`); never emit clickable links from mail content.
4. Truncate to a per-item character cap with an explicit `…[truncated]` marker.
5. Wrap in a quoted-external frame (e.g. `» from «Display Name» <addr>: "…"`) — attacker text is never rendered in the assistant's own voice.

**Telegram config (from `shared/config`, pydantic-settings; secrets from the store, never git):**
```
TELEGRAM_BOT_TOKEN          # secret store only
TELEGRAM_AUTHORIZED_CHAT_ID # the single allowed output + command source
DIGEST_UNCERTAIN_MAX_ITEMS  # budget cap
DIGEST_ITEM_CHAR_CAP
```

**Correction capture contract:**
```python
class Correction(BaseModel):
    work_object_id: UUID
    field: str
    corrected_value: Any
    author_chat_id: int            # must equal TELEGRAM_AUTHORIZED_CHAT_ID
    provenance: list[ProvenanceRef]
    created_at: datetime
# Persisted as a human-authenticated, provenance-tagged regression fixture (spec 08).
```

## Invariants

1. No model/LLM call occurs in the digest render path — rendering is a pure deterministic function.
2. Every attacker-derived string passes the full neutralization pipeline before reaching Telegram.
3. The bot only ever sends to `TELEGRAM_AUTHORIZED_CHAT_ID` and only accepts commands/corrections from it.
4. No URL from mail content is ever emitted as a clickable link.
5. Provenance drill-down displays stored references only; it never fetches remote/attacker content.
6. When the eval trust-bar gate is unmet, `low_trust` is set and the digest is visibly flagged.
7. The digest performs zero email actions and mutates no state except appending correction records (via the mutation layer, spec 02).

## Failure modes

- **State store unavailable** → send a minimal deterministic "degraded: state unavailable" notice; never fabricate items.
- **Item exceeds cap** → truncate with marker; never drop the neutralization.
- **Telegram send fails** → retry with backoff; on repeated failure raise the ops heartbeat alert (spec 09); never silently drop.
- **Command from a non-authorized chat_id** → ignore and log.
- **Uncertain section over budget** → keep the top-`N` by `trusted_signal_risk`, summarize the remainder as a count.

## Acceptance criteria

- [ ] Digest is rendered entirely by deterministic code; a test asserts no `ModelClient` call occurs during render.
- [ ] Neutralization strips bidi/control chars, escapes markup, defangs URLs, and caps length — covered by unit tests with adversarial fixtures.
- [ ] Attacker-supplied subject/display-name/body renders only inside a quoted-external frame, never as assistant voice or a clickable link.
- [ ] Bot rejects sends to and commands from any chat_id other than the authorized one.
- [ ] Provenance drill-down returns stored refs/spans and issues no network fetch.
- [ ] The five sections render, with the uncertain section budgeted and ranked by trusted-signal risk.
- [ ] `low_trust` flag surfaces in the rendered digest when the trust-bar gate is unmet.
- [ ] A correction from the authorized chat writes a provenance-tagged regression fixture and triggers no unbiased-metric update.

## Test seams

- `render_digest` is pure over a `DigestViewModel` — golden-file tests over fixtures.
- `TelegramClient` (a `ToolClient`) is injectable/fakeable; assert single-chat-id targeting and payloads deterministically.
- `Clock` injected for `generated_at`.
- Adversarial fixture set: bidi overrides, zero-width chars, Markdown injection, `javascript:`/`data:` URLs, homoglyph domains, oversized payloads.

## Reviewer-finding traceability

| Finding (plan) | How this spec addresses it |
|---|---|
| `[C · Security] Digest is an egress + phishing surface` | Deterministic templater; escape + strip bidi/control; defang URLs; quoted-external framing; per-item cap; single authorized chat-id. |
| `[C · AI] Eval loop blind to misses` (corrections) | In-flow corrections captured strictly as regression fixtures, explicitly not an unbiased metric source (see spec 08). |
| `[H · AI] No abstention path` | Dedicated budgeted **Uncertain** section, ranked by trusted-signal risk to avoid alert fatigue. |
| `[H · AI] Cold-start` (trust-bar gate) | `low_trust` flag renders when actionable-recall/faithfulness gate is unmet. |
| `[C · Security] Trusted-signal boundary` | Renders `sender_address` (verified) distinctly from `sender_display_raw`; shows domain-auth vs identity-trust without conflating them. |
| `[M, folded] Metadata is an injection site` | Subject/display-name/filenames neutralized identically to bodies. |
| `[L, folded] Telegram is a third party` | Documented as an egress recipient; no secrets/PII beyond digest content sent; see Open items. |

## Open items

- Exact Telegram parse mode (plain vs escaped MarkdownV2) and inline-button vs reply-parsing UX for corrections.
- Per-item and uncertain-section budget defaults (tune against alert fatigue during backlog dry-run).
- Whether/how to redact PII within the digest itself given Telegram is a third-party recipient.
