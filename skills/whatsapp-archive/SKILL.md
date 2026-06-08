---
name: whatsapp-archive
description: >-
  Archiva el historial de un chat/grupo/canal de WhatsApp (texto + imágenes + audios transcriptos) a un vault Obsidian, READ-ONLY, usando la CLI `wa2vault`. Activar cuando el usuario diga "archivá/traeme/bajá/scrapeá/exportá el chat/grupo/canal de X", "los últimos N días del chat con X", "el WhatsApp de X", "la conversación con X", "transcribí los audios del chat", "actualizá/refrescá el historial de X", o pida traer fresco lo que se habló con alguien para trabajarlo. Motor: `wa2vault` (read-only, nunca envía). Requiere wacli pareado una vez por QR.
---

# whatsapp-archive

Archiva conversaciones de WhatsApp a un vault Obsidian para que los agentes las lean y razonen sobre ellas. Motor: la CLI **`wa2vault`** (debe estar en el PATH). Es **read-only**: nunca envía mensajes. Repo del motor: https://github.com/frizynn/wa2vault

## Qué hace

`wa2vault pull --chat "<nombre>" --days <N>` →
1. sincroniza el store local (wacli),
2. exporta los últimos N días del chat/grupo/canal,
3. baja imágenes y **transcribe las notas de voz** (Whisper local en CPU, con cache por mensaje),
4. escribe un Markdown estructurado en `<vault>/Chats/<chat>.md` (frontmatter para agentes + timeline por día + transcripciones inline + imágenes embebidas).

## Comandos (`wa2vault` está global en PATH)

- `wa2vault chats` — lista chats (NAME / TYPE / JID) para encontrar el nombre/JID exacto.
- `wa2vault pull --chat "<nombre|jid>" --days <N> [--no-transcribe] [--no-media]` — el comando principal.
- `wa2vault sync [--idle <seg>] [--media]` — refresca el store sin exportar. `--idle 180` se queda conectado más tiempo y baja más historial por corrida (llega por tandas).
- `wa2vault contact add "<número>" "<nombre>"` — guarda un nombre local para un DM. `contact list` / `contact rm <nombre|número>`.
- `wa2vault transcribe <audio>` — transcribe un audio suelto e imprime el texto.
- `wa2vault auth` — emparejar el teléfono (QR). **Solo lo puede hacer el usuario.**

## Flujo que seguís

1. **Chequear pairing.** Corré `wa2vault chats`. Si dice "No chats found" o da error de conexión → no está pareado: decile al usuario que corra **`wa2vault auth`** y escanee el QR con el celu (WhatsApp → Ajustes → Dispositivos vinculados). **No podés escanear el QR vos** — frená ahí hasta que lo haga.
2. **Resolver el chat.** Si el usuario dio un nombre claro, usalo. Si no estás seguro del nombre exacto, corré `wa2vault chats` y buscá el match. Si `pull` devuelve error de "ambiguo"/varios candidatos, mostrale los candidatos y preguntá cuál (o usá el JID directo).
   - **Nota sobre DMs**: los chats 1-a-1 suelen aparecer con el número de teléfono (los nombres de contacto de WhatsApp no siempre sincronizan al vincular). Si el usuario quiere referirse a alguien por nombre ("el chat con Mamá"), guardalo primero con `wa2vault contact add "<número>" "Mamá"` y después `pull --chat "Mamá"` lo resuelve. También se puede pullear directo por número: `pull --chat "<número>"`.
3. **Días.** Default 30 si no especifica. Parseá "última semana"=7, "último mes"=30, "últimos N días", "este año"=365, etc.
4. **Correr.** `wa2vault pull --chat "<nombre>" --days <N>`. Flags: `--no-transcribe` si pide solo texto / lo quiere rápido; `--no-media` si no quiere imágenes/audios.
5. **Reportar.** Mostrá el path de la nota generada, los conteos (mensajes, imágenes, audios transcriptos) y cualquier warning. Si el usuario lo pide, leé la nota y resumí lo conversado.

## Salida

`<vault>/Chats/<chat-slug>.md` — ese es el archivo que después leés para responder preguntas sobre la conversación. La media vive en `<vault>/Chats/_media/<chat-slug>/` (embebida con paths relativos al vault). El `<vault>` por defecto es `~/Obsidian/wa2vault` y se configura con `vault_dir`.

## Reglas

- **Read-only / seguro**: `wa2vault` nunca manda mensajes. Aclarálo si el usuario duda.
- **Captura proactiva (full trust)**: si el usuario arranca a trabajar sobre "lo que habló con X" y conviene tenerlo fresco, ofrecé o corré el `pull` directo.
- **Caveat de historial (decílo si es relevante)**: el dispositivo vinculado solo tiene lo que el teléfono empujó — full-sync de ~1 año al emparejar, después incremental. Para ir muy atrás, el backfill es best-effort y la media vieja puede haber expirado en el CDN de WhatsApp. Correr `wa2vault sync` (o el pull) seguido mantiene el archivo completo.
- **Auto-actualizar (opcional)**: se puede dejar un cron o un `/loop` que corra `wa2vault pull --chat X --days N` cada X horas para mantener un chat siempre fresco.

## Config

`~/.config/wa2vault/config.toml` — claves útiles: `vault_dir` (default `~/Obsidian/wa2vault`), `asr_model` (`medium` default; `small` = más rápido, `large-v3` = mejor), `language` (`es`), `default_days` (30).

## Si algo falla

- `ffmpeg not found` → instalar ffmpeg (`sudo apt install ffmpeg`).
- Transcripción lenta → bajar `asr_model` a `small` en el config.
- `wacli` no instalado / store vacío → ver el README en https://github.com/frizynn/wa2vault
