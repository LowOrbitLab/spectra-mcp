# Fingerprint and geo behavior

`browser_start` and `start_session` default to:

```text
fingerprint_profile = auto
timezone = ""
locale = "auto"
```

## Automatic profile selection

- Linux non-persistent sessions use `linux_native`.
- Persistent profiles, reCAPTCHA preparation, Windows, and macOS use the
  upstream `windows` persona.
- Explicit `windows` or `linux_native` values override automatic selection.

The Linux-native profile keeps navigator, fonts, graphics, WebGL, timezone, and
host operating-system behavior coherent. Its renderer persona is selected
deterministically from the session seed.

`linux_native` currently does not support persistent `profile_dir` sessions or
`prep_recaptcha`.

## Timezone and locale

An empty timezone is resolved from the proxy/browser egress IP. `locale="auto"`
uses the egress country. Explicit IANA zones and locale values remain available
for reproducible tests.

## Seeds

Omitting `seed` creates a random fingerprint seed. Supplying a seed replays the
same configured persona on the same deployment environment. GPU-dependent
surfaces can differ across hosts, so validate important seeds on the machine
where they will run.

## Operational notes

- IP reputation remains important even with a coherent browser fingerprint.
- The first session may need network access for geo resolution.
- Humanized pointer movement is slower than vanilla Playwright.
- Coordinate hold-drag may require `humanize=false` with the current patched
  Firefox build.
