---
name: whatsapp-archive
description: >-
  Archive a WhatsApp chat/group/channel history (text + images + transcribed voice notes) into an Obsidian vault, READ-ONLY, using the `wa2vault` CLI. Activate when the user says "archive/get me/pull/scrape/export the chat/group/channel with X", "the last N days of my chat with X", "X's WhatsApp", "the conversation with X", "transcribe the chat's voice notes", "update/refresh the history with X", or asks to pull fresh whatever was discussed with someone so it can be worked on. Engine: `wa2vault` (read-only, never sends). Requires wacli paired once via QR.
---

# whatsapp-archive

Archives WhatsApp conversations into an Obsidian vault so agents can read and reason over them. Engine: the **`wa2vault`** CLI (must be on PATH). It is **read-only**: it never sends messages. Engine repo: https://github.com/frizynn/wa2vault

## What it does

`wa2vault pull --chat "<name>" --days <N>` →
1. syncs the local store (wacli),
2. exports the last N days of the chat/group/channel,
3. downloads images and **transcribes voice notes** (local Whisper on CPU, cached per message),
4. writes structured Markdown to `<vault>/Chats/<chat>.md` (agent-friendly frontmatter + per-day timeline + inline transcriptions + embedded images).

## Commands (`wa2vault` is global on PATH)

- `wa2vault chats` — list chats (NAME / TYPE / JID) to find the exact name/JID.
- `wa2vault pull --chat "<name|jid>" --days <N> [--no-transcribe] [--no-media]` — the main command.
- `wa2vault sync [--idle <sec>] [--media]` — refresh the store without exporting. `--idle 180` stays connected longer and pulls more history per run (it arrives in batches).
- `wa2vault contact add "<number>" "<name>"` — save a local name for a DM. `contact list` / `contact rm <name|number>`.
- `wa2vault transcribe <audio>` — transcribe a single audio file and print the text.
- `wa2vault auth` — pair the phone (QR). **Only the user can do this.**

## Flow to follow

1. **Check pairing.** Run `wa2vault chats`. If it says "No chats found" or errors on connection → it's not paired: tell the user to run **`wa2vault auth`** and scan the QR with their phone (WhatsApp → Settings → Linked devices). **You cannot scan the QR yourself** — stop there until they do.
2. **Resolve the chat.** If the user gave a clear name, use it. If unsure of the exact name, run `wa2vault chats` and find the match. If `pull` returns an "ambiguous"/multiple-candidates error, show the candidates and ask which one (or use the JID directly).
   - **Note on DMs**: one-to-one chats often show up as a phone number (WhatsApp contact names don't always sync when you link a device). If the user wants to refer to someone by name ("the chat with Mom"), save it first with `wa2vault contact add "<number>" "Mom"` and then `pull --chat "Mom"` resolves it. You can also pull directly by number: `pull --chat "<number>"`.
3. **Days.** Default 30 if unspecified. Parse "last week"=7, "last month"=30, "last N days", "this year"=365, etc.
4. **Run.** `wa2vault pull --chat "<name>" --days <N>`. Flags: `--no-transcribe` if they want text only / want it fast; `--no-media` if they don't want images/audio.
5. **Report.** Show the path of the generated note, the counts (messages, images, transcribed voice notes) and any warnings. If the user asks, read the note and summarize what was discussed.

## Output

`<vault>/Chats/<chat-slug>.md` — that's the file you read afterward to answer questions about the conversation. Media lives in `<vault>/Chats/_media/<chat-slug>/` (embedded with vault-relative paths). The default `<vault>` is `~/Obsidian/wa2vault` and is set via `vault_dir`.

## Rules

- **Read-only / safe**: `wa2vault` never sends messages. Say so if the user is unsure.
- **Proactive capture (full trust)**: if the user starts working on "what they discussed with X" and it's worth having fresh, offer to run — or just run — the `pull` directly.
- **History caveat (mention if relevant)**: a linked device only has what the phone pushed — a full sync of ~1 year at pairing, then incremental. To reach far back, backfill is best-effort and old media may have expired on WhatsApp's CDN. Running `wa2vault sync` (or the pull) regularly keeps the archive complete.
- **Auto-update (optional)**: you can set a cron or a `/loop` running `wa2vault pull --chat X --days N` every few hours to keep a chat always fresh.

## Config

`~/.config/wa2vault/config.toml` — useful keys: `vault_dir` (default `~/Obsidian/wa2vault`), `asr_model` (`medium` default; `small` = faster, `large-v3` = better), `language` (transcription language; default `es` — set it to your own), `default_days` (30).

## If something fails

- `ffmpeg not found` → install ffmpeg (`sudo apt install ffmpeg`).
- Slow transcription → lower `asr_model` to `small` in the config.
- `wacli` not installed / empty store → see the README at https://github.com/frizynn/wa2vault
