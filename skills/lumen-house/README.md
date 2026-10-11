# Lumen House discovery skill

[SKILL.md](SKILL.md) reads current public offers and prepares a reviewable proposal. It performs no payments, document uploads, contacts, code execution or credential entry.

## Install sources

- Website document: <https://lumenhouse.xyz/skills/lumen-house/SKILL.md>. Verify availability before installing.
- Repository document: <https://github.com/hotsyrup/lumen-house/blob/main/skills/lumen-house/SKILL.md>.
- Raw repository URL: <https://raw.githubusercontent.com/hotsyrup/lumen-house/main/skills/lumen-house/SKILL.md>.

The official skills CLI documents this repository layout and Hermes agent selection. This CLI route requires Node.js 22.20 or later and Git.

Install into Hermes for the current project:

```sh
npx skills add hotsyrup/lumen-house --skill lumen-house --agent hermes-agent
```

Use `--global` only when a user-wide installation is intended. See the [skills FAQ](https://www.skills.sh/docs/faq) and [official CLI documentation](https://github.com/vercel-labs/skills).

## Paid-service limits

Recheck each offer and its terms before proposing a purchase. The Manifest Kit costs 0.10 USDC on Base or USD 1.00 through Stripe MPP for one MIT-licensed ZIP; MIT does not make the paid download free. It checks listed local file sizes and SHA-256 digests, without security or authorship certification.

The checker costs USD 1.00 through Stripe MPP or 0.10 USDC on Base per supplied public-document snapshot. It compares supplied documents rather than testing live websites. Live funded checker fulfillment remains unverified for both methods. Human assistance requires an individually agreed written quote. Discovery and installation confer no purchasing, contact or posting authority.

## Test results

- Exact mirror: 5,737 bytes; SHA-256 `68cfc15d20bc11b7f2e890436085f5ee583c973e7fb3f127177a7728ada4a817`.
- Official Hermes installer: **passed October 11, 2026, at 22:31 UTC** in fresh Docker with Python 3.14.8. Tested official v0.21.6 source commit `818c13be1dc4fd28987e1e881a9408224afd4535`; the source archive had no `.git` metadata, so the CLI displayed `vunknown`.

The tested command was:

```sh
python hermes skills install https://lumenhouse.xyz/skills/lumen-house/SKILL.md --category discovery
```

Normal confirmation was used, without force or a custom User-Agent. Fresh `skills-guard-v8` returned **SAFE / community** with no findings. Only the exact `SKILL.md` above was installed; native listing showed it enabled, and the native audit returned SAFE with exit code 0. This proves discovery-skill installation, not paid-service fulfillment.

- skills.sh / skills CLI installer test: **pending; not run**. Repository layout and command were checked against official documentation only.

## License

The [MIT license](LICENSE) applies only to `SKILL.md`, `README.md` and `LICENSE` in this directory. The root README link adds no license to the other website or repository files. The paid Manifest Kit has its own separate MIT license and sale terms.
