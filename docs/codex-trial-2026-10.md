# codex trial and A/B against the default backend (October 2026)

One paid run of the codex backend, kept as a reference for choosing a model. Input: the crawl of
"Speaker & Headphone Test" (`com.speaker.clean.ejectwater`, 13 screens), stage `analyze`.

## The paid run (2026-10-14)

- Model `gpt-5.6-luna`, reasoning effort `low` (the cheapest listed codex model; official API
  price per 1M tokens: $0.20 input, $0.02 cached input, $1.20 output).
- Ran through the real pipeline path (`analyze` → `run_agent` → `codex_command`) inside an isolated
  container, before that isolation became part of the backend (see [agent-backend.md](agent-backend.md)).
- 91 s, codex exit 0: 5 shell commands, 1 web search, 1 file write (`app_spec.json`).
- Usage: 453,496 input tokens (377,856 cached), 10,165 output tokens. API-equivalent cost ≈ $0.035
  (75,640 × $0.20 + 377,856 × $0.02 + 10,165 × $1.20 per 1M). With a ChatGPT login it is counted
  against the plan's codex limits, not billed per token.
- The pipeline rejected the spec: `navigation.tabs lists screen '0011' more than once`. That was the
  only contract violation; with the duplicate tab removed the spec validates.

## A/B on the same input (2026-10-15)

`analyze` was re-run on the default backend with the current prompt and contract (371 s, valid on the
first try), so both specs come from the same prompt. The earlier comparison used a default-backend
spec generated on 2026-07-13 with a much older prompt. Both default-backend specs carry the same
`provenance.source_crawl_sha256` (`f7434052…`). The `gpt-5.6-luna` spec has no provenance, because the
pipeline rejected it before adding one, but it ran on the same crawl copy (the same 13 screen ids).

| | default backend, current prompt | `gpt-5.6-luna`, current prompt | default backend, 2026-07-13 prompt |
|---|---|---|---|
| Contract | valid | invalid (duplicate tab `0011`) | valid |
| App name | Speaker Cleaner | Speaker Cleaner | Speaker & Headphone Test |
| Screens | 16: all 13 crawled + 3 inferred (`dbmeter`, `cleaning_session`, `tips_test`) | 13: all crawled | 13: missed `0009`, invented `0013` |
| Tabs | Cleaner 0011, Mode 0012, dB Meter `dbmeter`, Stereo 0002 | same titles and order, but dB Meter points at 0011 | none |
| Requirements | 22 | 6 | 15 |
| Capabilities | 9 (cleaning playback, vibration, 7-day plan, stereo test, dB meter, test sound, subscription, language, share/feedback) | 7 (no language, share/feedback or test sound) | 0 |

## Conclusion

- The large gap in the first comparison (no capabilities, no tabs, a different app name) came from
  the prompt version, not the model: the 2026-07-13 spec predates capabilities and `navigation.tabs`.
- With the same prompt, the remaining gap is the model. Both read the app the same way (name, four
  tabs in the same order, all crawled screens covered). `gpt-5.6-luna` at `low` effort is much
  shallower (6 vs 22 requirements) and broke the contract where a screen was not captured: it pointed
  the dB Meter tab at the home screen instead of adding a screen for it.
- `gpt-5.6-luna` proves the codex path works end to end at about $0.035 per `analyze`, but it is not
  good enough for `analyze`. A stronger codex model needs its own paid run before it is used.
